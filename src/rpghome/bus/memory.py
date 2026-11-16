"""InMemoryBus: the Bus inside one process, for tests.

Same behaviour as a broker where it matters: retained messages (a late subscriber gets the
last one at once), `+` / `#` in filters, a last will that `kill()` publishes, and a payload
that is not a JSON object handed to the subscriber's `on_invalid` (or logged and dropped).
Messages are delivered synchronously, in the publishing thread, so a test needs no waiting.
"""

import json
import logging

from rpghome.bus.base import Handler, InvalidHandler, topic_matches

log = logging.getLogger("rpghome.bus")


def as_raw(payload: object) -> bytes:
    """The payload as bytes, the way a broker would hand it to `on_invalid`."""
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode()
    try:
        return json.dumps(payload).encode()
    except (TypeError, ValueError):
        return repr(payload).encode()


class InMemoryBus:
    def __init__(self, will: tuple[str, dict] | None = None):
        #: every publish so far, as (topic, payload, retain) - a test's log of what was sent
        self.published: list[tuple[str, dict, bool]] = []
        #: topic -> retained payload
        self.retained: dict[str, dict] = {}
        self.will: tuple[str, dict] | None = will
        self.dead = False  # kill(): the client is gone, only the will got out
        self.closed = False
        self._subs: list[tuple[str, Handler, InvalidHandler | None]] = []

    # -- the will -----------------------------------------------------------------

    def set_will(self, topic: str, payload: dict) -> None:
        """What the broker would publish if this client died without saying goodbye."""
        self.will = (topic, payload)

    def kill(self) -> None:
        """Die without closing: the bus publishes the last will, then delivers nothing."""
        self.dead = True
        if self.will is None:
            log.warning("InMemoryBus killed with no will set")
            return
        topic, payload = self.will
        self._deliver(topic, payload, retain=True)

    # -- Bus ----------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return not self.dead and not self.closed

    def publish(self, topic: str, payload: dict, retain: bool = False) -> None:
        if self.dead or self.closed:
            log.warning("publish on %s ignored: the bus is gone", topic)
            return
        self._deliver(topic, payload, retain)

    def subscribe(
        self, topic_filter: str, handler: Handler, on_invalid: InvalidHandler | None = None
    ) -> None:
        if self.closed:
            return
        self._subs.append((topic_filter, handler, on_invalid))
        for topic, payload in list(self.retained.items()):  # retained messages arrive at once
            if topic_matches(topic_filter, topic):
                self._call(handler, on_invalid, topic, payload)

    def close(self) -> None:
        self.closed = True
        self._subs.clear()

    # -- internals ------------------------------------------------------------------

    def _deliver(self, topic: str, payload: dict, retain: bool) -> None:
        if retain:
            self.retained[topic] = payload
        self.published.append((topic, payload, retain))
        for topic_filter, handler, on_invalid in list(self._subs):
            if topic_matches(topic_filter, topic):
                self._call(handler, on_invalid, topic, payload)

    @staticmethod
    def _call(
        handler: Handler, on_invalid: InvalidHandler | None, topic: str, payload: object
    ) -> None:
        if not isinstance(payload, dict):
            reason = f"not a JSON object, got a {type(payload).__name__}"
            if on_invalid is None:
                log.warning("dropped a message on %s that is %s", topic, reason)
                return
            try:
                on_invalid(topic, as_raw(payload), reason)
            except Exception:  # a handler that raises must not break the bus
                log.exception("invalid-message handler for %s failed", topic)
            return
        try:
            handler(topic, payload)
        except Exception:  # a handler that raises must not break the bus
            log.exception("bus handler for %s failed", topic)
