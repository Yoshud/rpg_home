"""MqttBus: the Bus over a real broker (paho-mqtt 2.x).

A background network loop (`loop_start`) owns the socket: `publish` and `subscribe` never
block on the network, and the loop reconnects by itself (exponential backoff, subscriptions
renewed and `connect_hooks` run after every (re)connect).

The broker may be down: `connect_async` + `loop_start` keep trying in the background, so a
service starts anyway. `wait_connected()` is there for the callers that want to know. While
the connection is down a publish is dropped, not queued behind it - a state message is only
worth its latest version, and the connect hooks re-publish it when the broker is back. An
outage costs one warning, its end one line of info, however many attempts it took; before the
broker was ever reached, publishes lost are the normal start-up state and say nothing but a
debug line.

Payloads are JSON objects. A message that is not one goes to the subscriber's `on_invalid`
(or is logged and dropped), never raised into the network thread. Handlers are guarded the
same way. QoS 1 can deliver the same message twice, and the bus hands over what the broker
gives it: only the service that answers a command can say what a repeated `(topic, id)`
means, because its own answer is part of the answer (see `bus/recent.py`).
"""

import json
import logging
import threading
import uuid
from collections.abc import Callable

import paho.mqtt.client as mqtt

from rpghome.bus.base import Handler, InvalidHandler, topic_matches

log = logging.getLogger("rpghome.bus")

QOS = 1  # commands, events and state: at least once, on a local broker that is cheap
KEEPALIVE_S = 20
RECONNECT_MAX_DELAY_S = 10


class MqttBus:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        client_id: str | None = None,
        will: tuple[str, dict] | None = None,
        keepalive_s: int = KEEPALIVE_S,
        connect_hooks: list[Callable[[], None]] | None = None,
    ):
        self.host, self.port = host, port
        self._client_id = client_id or f"rpghome-{uuid.uuid4().hex[:8]}"
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=self._client_id, protocol=mqtt.MQTTv311
        )
        self._client.reconnect_delay_set(min_delay=1, max_delay=RECONNECT_MAX_DELAY_S)
        self._client.on_connect = self._on_connect
        self._client.on_connect_fail = self._on_connect_fail
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._filters: dict[str, tuple[Handler, InvalidHandler | None]] = {}
        self._connect_hooks: list[Callable[[], None]] = list(connect_hooks or [])
        self._connected = threading.Event()
        self._ever_connected = False  # only for the log: "connected" vs "reconnected"
        self._warned = False  # one warning per outage, not one per attempt
        self._dropped = False  # and one about the publishes that outage cost
        self._closed = False
        if will is not None:  # a last will must be set before connecting
            self._client.will_set(will[0], json.dumps(will[1]), qos=QOS, retain=True)
        self._client.connect_async(host, port, keepalive=keepalive_s)
        self._client.loop_start()

    # -- extra to what the Bus protocol needs ----------------------------------------

    def add_connect_hook(self, hook: Callable[[], None]) -> None:
        """Run `hook()` after every (re)connect - e.g. re-publishing a retained flag."""
        self._connect_hooks.append(hook)

    def wait_connected(self, timeout_s: float) -> bool:
        """True when the broker is reached within timeout_s."""
        return self._connected.wait(timeout_s)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # -- Bus --------------------------------------------------------------------------

    def publish(self, topic: str, payload: dict, retain: bool = False) -> None:
        if self._closed:
            return
        if not self._connected.is_set():
            self._say_dropped(
                "publish on %s dropped: not connected to the broker (the bus retries by itself)",
                topic,
            )
            return
        try:
            data = json.dumps(payload)
        except (TypeError, ValueError) as exc:
            log.error("cannot encode the payload for %s: %s", topic, exc)
            return
        info = self._client.publish(topic, data, qos=QOS, retain=retain)
        if info.rc != mqtt.MQTTErrorCode.MQTT_ERR_SUCCESS:  # e.g. NO_CONN on a QoS 0 send
            log.warning("publish on %s failed: %s", topic, info.rc)

    def subscribe(
        self, topic_filter: str, handler: Handler, on_invalid: InvalidHandler | None = None
    ) -> None:
        if self._closed:
            return
        self._filters[topic_filter] = (handler, on_invalid)
        if self.connected:
            rc, _mid = self._client.subscribe(topic_filter, qos=QOS)
            if rc != mqtt.MQTTErrorCode.MQTT_ERR_SUCCESS:
                log.warning("subscribe to %s failed: %s", topic_filter, rc)

    def close(self) -> None:
        """A clean goodbye: the broker drops our last will instead of publishing it."""
        if self._closed:
            return
        self._closed = True
        self._connected.clear()
        self._filters.clear()
        try:
            self._client.disconnect()  # queued, and the network loop flushes it before exiting
        except Exception as exc:  # closing must not raise
            log.warning("disconnecting from the broker failed: %s", exc)
        try:
            self._client.loop_stop()
        except Exception as exc:
            log.warning("stopping the network loop failed: %s", exc)

    # -- paho callbacks (network thread) ------------------------------------------------

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties) -> None:
        if reason_code.is_failure:
            self._say("broker %s:%d refused the connection: %s", self.host, self.port, reason_code)
            return
        first, was_down = not self._ever_connected, self._warned or self._dropped
        self._ever_connected = True
        self._connected.set()
        self._warned = self._dropped = False  # the next outage gets its own warning
        log.info(
            "%s to broker %s:%d as %s",
            "connected" if first else ("back" if was_down else "reconnected"),
            self.host,
            self.port,
            self._client_id,
        )
        for topic_filter in list(self._filters):  # subscriptions are per connection
            client.subscribe(topic_filter, qos=QOS)
        for hook in list(self._connect_hooks):
            try:
                hook()
            except Exception:  # a hook must not break the network loop
                log.exception("connect hook failed")

    def _on_connect_fail(self, _client, _userdata) -> None:
        self._connected.clear()
        self._say(
            "cannot reach the broker at %s:%d, retrying in the background", self.host, self.port
        )

    def _on_disconnect(self, _client, _userdata, _flags, reason_code, _properties) -> None:
        self._connected.clear()
        if self._closed:
            return
        self._say("lost the broker (%s); the bus reconnects by itself", reason_code)

    def _say(self, message: str, *args) -> None:
        """One warning for an outage, then silence until the broker is back."""
        if self._warned:
            log.debug(message, *args)
            return
        self._warned = True
        log.warning(message, *args)

    def _say_dropped(self, message: str, *args) -> None:
        """Once per outage: what that outage cost a service that kept publishing.

        Before the broker was ever reached this is the start-up state a service is meant to
        sit in - the engine plays silence and retries in the background - so it is a debug
        line. Once a connection has been up, publishes lost are worth the warning.
        """
        if self._dropped:
            log.debug(message, *args)
            return
        self._dropped = True
        if self._ever_connected:
            log.warning(message, *args)
        else:
            log.debug(message, *args)

    # -- received messages ---------------------------------------------------------------

    def _on_message(self, _client, _userdata, message) -> None:
        try:
            payload = json.loads(message.payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._invalid(message.topic, message.payload, str(exc))  # the reason, phrased by json
            return
        if not isinstance(payload, dict):
            self._invalid(
                message.topic, message.payload, f"not a JSON object, got a {type(payload).__name__}"
            )
            return
        for topic_filter, (handler, _on_invalid) in list(self._filters.items()):
            if topic_matches(topic_filter, message.topic):
                try:
                    handler(message.topic, payload)
                except Exception:  # never raise into the network thread
                    log.exception("bus handler for %s failed", message.topic)

    def _invalid(self, topic: str, raw: bytes, reason: str) -> None:
        """A message no handler can use: hand it to the subscribers that asked for it."""
        asked = False
        for topic_filter, (_handler, on_invalid) in list(self._filters.items()):
            if on_invalid is not None and topic_matches(topic_filter, topic):
                asked = True
                try:
                    on_invalid(topic, raw, reason)
                except Exception:  # never raise into the network thread
                    log.exception("invalid-message handler for %s failed", topic)
        if not asked:
            log.warning(
                "dropped a message on %s that is not a JSON object (%s): %r",
                topic,
                reason,
                raw[:80],
            )
