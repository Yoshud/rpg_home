"""MqttBus away from a broker: the JSON rules and the reconnect bookkeeping.

The bus is pointed at a socket that accepts the connection and never answers, so it is never
connected and its network thread does nothing that could disturb the test - which is close to
the state an engine starts in when the broker is down. What paho calls back is called the way
paho calls it. The network itself is covered in test_mqtt_broker.py (`-m broker`).
"""

import logging
import socket

import paho.mqtt.client as mqtt
import pytest
from paho.mqtt.reasoncodes import ReasonCode

from rpghome.bus import topics
from rpghome.bus.base import Bus
from rpghome.bus.mqtt import MqttBus


class FakeClient:
    """What paho passes as `client`: only subscribe() is used."""

    def __init__(self):
        self.subscribed: list[tuple[str, int]] = []

    def subscribe(self, topic_filter, qos=0):
        self.subscribed.append((topic_filter, qos))
        return 0, 1


class FakeMessage:
    def __init__(self, topic, payload):
        self.topic, self.payload = topic, payload


def success():
    return ReasonCode(mqtt.PacketTypes.CONNACK, "Success")


def failure():
    return ReasonCode(mqtt.PacketTypes.CONNACK, "Not authorized")


@pytest.fixture
def bus():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)  # accepts the connection, never answers: no CONNACK, ever
    try:
        quiet_bus = MqttBus("127.0.0.1", listener.getsockname()[1], client_id="rpghome-test-unit")
        try:
            yield quiet_bus
        finally:
            quiet_bus.close()
    finally:
        listener.close()


def test_an_mqtt_bus_is_a_bus(bus):
    assert isinstance(bus, Bus)


def test_a_bus_starts_disconnected_and_close_is_safe(bus):
    assert bus.wait_connected(0.2) is False
    assert bus.connected is False
    bus.close()  # never connected: no raise, and a second close does nothing
    bus.close()
    bus.publish(topics.STATE, {"scene": "x"})  # after close: ignored
    bus.subscribe(topics.STATE, lambda _t, _p: None)


def test_a_reconnect_renews_the_subscriptions_and_runs_the_connect_hooks(bus):
    bus.subscribe(topics.CMD_FILTER, lambda _t, _p: None)
    announced: list[int] = []
    bus.add_connect_hook(lambda: announced.append(1))

    first = FakeClient()
    bus._on_connect(first, None, None, success(), None)
    assert bus.connected is True
    assert first.subscribed == [(topics.CMD_FILTER, 1)]
    assert announced == [1]

    second = FakeClient()  # the broker restarted: subscribe again, announce again
    bus._on_connect(second, None, None, success(), None)
    assert second.subscribed == [(topics.CMD_FILTER, 1)]
    assert announced == [1, 1]


def test_a_refused_connection_leaves_the_bus_disconnected(bus, caplog):
    with caplog.at_level(logging.WARNING, logger="rpghome.bus"):
        bus._on_connect(FakeClient(), None, None, failure(), None)
    assert bus.connected is False
    assert "refused the connection" in caplog.text


def test_a_connect_hook_that_raises_does_not_break_the_connection(bus, caplog):
    def boom():
        raise RuntimeError("hook blew up")

    bus.add_connect_hook(boom)
    with caplog.at_level(logging.ERROR, logger="rpghome.bus"):
        client = FakeClient()
        bus._on_connect(client, None, None, success(), None)
    assert bus.connected is True
    assert client.subscribed == []  # nothing subscribed yet in this test
    assert "hook blew up" in caplog.text


def test_a_message_is_delivered_only_when_it_is_a_json_object(bus, caplog):
    seen: list[dict] = []
    bus.subscribe(topics.CMD_FILTER, lambda _t, payload: seen.append(payload))
    client = FakeClient()
    bus._on_connect(client, None, None, success(), None)
    with caplog.at_level(logging.WARNING, logger="rpghome.bus"):
        bus._on_message(None, None, FakeMessage(topics.cmd("scene"), b'{"scene": {"name": "a"}}'))
        bus._on_message(None, None, FakeMessage(topics.cmd("scene"), b"{not json"))
        bus._on_message(None, None, FakeMessage(topics.cmd("scene"), b'["scene"]'))
        bus._on_message(None, None, FakeMessage(topics.cmd("scene"), b"\xff\xfe"))
    assert seen == [{"scene": {"name": "a"}}]  # the rest was logged and dropped, not raised
    assert caplog.text.count("dropped a message") == 3


def test_a_handler_that_raises_does_not_reach_the_network_thread(bus, caplog):
    def boom(_topic, _payload):
        raise RuntimeError("handler blew up")

    bus.subscribe(topics.CMD_FILTER, boom)
    bus._on_connect(FakeClient(), None, None, success(), None)
    with caplog.at_level(logging.ERROR, logger="rpghome.bus"):
        bus._on_message(None, None, FakeMessage(topics.cmd("stop"), b"{}"))  # nothing raises here
    assert "handler blew up" in caplog.text


def test_a_payload_that_cannot_be_json_is_reported_not_raised(bus, caplog):
    bus._on_connect(FakeClient(), None, None, success(), None)  # so the publish is really tried
    with caplog.at_level(logging.ERROR, logger="rpghome.bus"):
        bus.publish(topics.STATE, {"scene": {1, 2}})  # a set is not JSON
    assert "cannot encode the payload" in caplog.text


def test_a_publish_while_disconnected_is_dropped_not_queued(bus, caplog):
    """Before the broker was ever reached, that is the start-up state, not an incident."""
    assert bus.connected is False
    with caplog.at_level(logging.WARNING, logger="rpghome.bus"):
        for _ in range(5):
            bus.publish(topics.STATE, {"scene": "x"})  # a state behind an outage is not queued
    assert "dropped: not connected" not in caplog.text  # no warning for a normal start

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="rpghome.bus"):
        bus.publish(topics.STATE, {"scene": "x"})
    assert caplog.text.count("dropped: not connected") == 1  # it is said, at debug level


def test_publishes_lost_after_the_broker_went_down_warn_once(bus, caplog):
    """Once a connection has been up, publishes it lost are worth saying out loud."""
    bus._on_connect(FakeClient(), None, None, success(), None)
    assert bus.connected is True
    bus._on_disconnect(None, None, None, failure(), None)  # a real outage
    with caplog.at_level(logging.WARNING, logger="rpghome.bus"):
        for _ in range(5):
            bus.publish(topics.STATE, {"scene": "x"})
    assert caplog.text.count("dropped: not connected") == 1  # one warning, then quiet


def test_a_message_that_is_not_a_json_object_reaches_on_invalid(bus, caplog):
    seen: list[dict] = []
    bad: list[tuple[str, bytes, str]] = []
    bus.subscribe(
        topics.CMD_FILTER, lambda _t, payload: seen.append(payload), lambda *b: bad.append(b)
    )
    bus._on_connect(FakeClient(), None, None, success(), None)
    with caplog.at_level(logging.WARNING, logger="rpghome.bus"):
        bus._on_message(None, None, FakeMessage(topics.cmd("scene"), b'{"scene": {"name": "a"}}'))
        bus._on_message(None, None, FakeMessage(topics.cmd("scene"), b"{not json"))
        bus._on_message(None, None, FakeMessage(topics.cmd("set"), b'["name"]'))
        bus._on_message(None, None, FakeMessage(topics.cmd("stop"), b"\xff\xfe"))
    assert seen == [{"scene": {"name": "a"}}]
    assert [t for t, _raw, _why in bad] == [
        topics.cmd("scene"),
        topics.cmd("set"),
        topics.cmd("stop"),
    ]
    assert "Expecting property name" in bad[0][2], bad  # what json says about `{not json`
    assert bad[1][2] == "not a JSON object, got a list"
    assert bad[2][1] == b"\xff\xfe" and bad[2][2].startswith("Expecting value"), bad
    assert b'["name"]' in bad[1][1]  # the raw bytes, as the broker sent them
    assert "dropped a message" not in caplog.text  # the subscriber took it, so nothing was dropped

    bus.subscribe(topics.EVENTS, lambda _t, _p: None)  # asks for nothing: the old behaviour
    with caplog.at_level(logging.WARNING, logger="rpghome.bus"):
        bus._on_message(None, None, FakeMessage(topics.EVENTS, b"nope"))
    assert "dropped a message" in caplog.text


def test_an_on_invalid_that_raises_does_not_reach_the_network_thread(bus, caplog):
    def boom(_topic, _raw, _reason):
        raise RuntimeError("invalid handler blew up")

    bus.subscribe(topics.CMD_FILTER, lambda _t, _p: None, boom)
    bus._on_connect(FakeClient(), None, None, success(), None)
    with caplog.at_level(logging.ERROR, logger="rpghome.bus"):
        bus._on_message(None, None, FakeMessage(topics.cmd("stop"), b"nope"))  # nothing raises
    assert "invalid handler blew up" in caplog.text


def test_the_bus_hands_over_every_message_the_broker_gives_it(bus):
    """QoS 1 is "at least once", and a repeated `(topic, id)` is not second-guessed here.

    Saying what a repeated id means needs the answer that was already given to it, which only
    the service that answers knows - so the bus delivers and `rpghome.bus.recent` decides
    (tests/engine/test_link.py is the engine doing exactly that).
    """
    seen: list[dict] = []
    bus.subscribe(topics.CMD_FILTER, lambda _t, payload: seen.append(payload))
    bus._on_connect(FakeClient(), None, None, success(), None)
    message = b'{"name": "master_gain_db", "value": -6, "id": "c1"}'
    for _ in range(3):  # the broker promises "at least once"
        bus._on_message(None, None, FakeMessage(topics.cmd("set"), message))
    assert seen == [{"name": "master_gain_db", "value": -6, "id": "c1"}] * 3

    bus._on_message(None, None, FakeMessage(topics.cmd("set"), b'{"name": "x", "id": "c2"}'))
    assert len(seen) == 4  # a new id is a new command

    for _ in range(2):  # no id: nothing to recognise it by, so it is applied as it comes
        bus._on_message(None, None, FakeMessage(topics.cmd("stop"), b"{}"))
    assert len(seen) == 6

    scene = b'{"scene": {"name": "a"}, "id": "c1"}'
    bus._on_message(None, None, FakeMessage(topics.cmd("scene"), scene))
    assert len(seen) == 7  # the same id on another topic is another command


def test_an_outage_costs_one_warning_and_its_end_one_line(bus, caplog):
    with caplog.at_level(logging.INFO, logger="rpghome.bus"):
        bus._on_connect_fail(None, None)  # the broker was never up: the first attempt warns
        for _ in range(4):
            bus._on_connect_fail(None, None)  # retrying every second, saying it once
        assert caplog.text.count("retrying in the background") == 1

        bus._on_connect(FakeClient(), None, None, success(), None)
        assert caplog.text.count("connected to broker") == 1  # the first connection, no outage
        assert "back to broker" not in caplog.text

        bus._on_disconnect(None, None, None, failure(), None)  # a real outage now
        for _ in range(4):
            bus._on_connect_fail(None, None)
        assert caplog.text.count("lost the broker") == 1
        assert caplog.text.count("retrying in the background") == 1  # the same outage, still once
        bus._on_connect(FakeClient(), None, None, success(), None)
        assert caplog.text.count("back to broker") == 1  # one line for its end, not one per try
