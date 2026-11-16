"""InMemoryBus: retained messages, MQTT wildcards, the last will, and what it never does."""

import logging

import pytest

from rpghome.bus import topics
from rpghome.bus.base import Bus, topic_matches
from rpghome.bus.memory import InMemoryBus


@pytest.mark.parametrize(
    ("topic_filter", "topic", "want"),
    [
        ("rpghome/cmd/#", "rpghome/cmd/scene", True),
        ("rpghome/cmd/#", "rpghome/cmd/a/b", True),
        ("rpghome/cmd/#", "rpghome/cmd", True),  # `#` also matches the level it sits at
        ("rpghome/cmd/#", "rpghome/state", False),
        ("rpghome/cmd/+", "rpghome/cmd/scene", True),
        ("rpghome/cmd/+", "rpghome/cmd/a/b", False),  # `+` is one level only
        ("rpghome/cmd/+", "rpghome/cmdx", False),
        ("rpghome/state", "rpghome/state", True),
        ("rpghome/state", "rpghome/state/extra", False),
        ("#", "anything/here", True),
    ],
)
def test_topic_filters_use_the_mqtt_wildcards(topic_filter, topic, want):
    assert topic_matches(topic_filter, topic) is want


def test_topics_name_everything_the_services_use():
    assert topics.cmd("scene") == "rpghome/cmd/scene"
    assert topics.command_of("rpghome/cmd/stop") == "stop"
    assert topics.command_of("rpghome/cmd") is None
    assert topics.command_of(topics.STATE) is None
    assert set(topics.COMMANDS) == {"scene", "overlay", "fx", "set", "stop"}


def test_in_memory_bus_is_a_bus():
    assert isinstance(InMemoryBus(), Bus)


def test_a_late_subscriber_gets_the_retained_message_at_once():
    bus = InMemoryBus()
    bus.publish(topics.STATE, {"scene": "cave"}, retain=True)
    bus.publish(topics.EVENTS, {"type": "ok"})  # not retained
    got = []
    bus.subscribe("rpghome/#", lambda topic, payload: got.append((topic, payload)))
    assert got == [(topics.STATE, {"scene": "cave"})]


def test_retained_message_is_replaced_and_reaches_a_subscriber_once_per_publish():
    bus = InMemoryBus()
    seen = []
    bus.subscribe(topics.STATE, lambda _t, p: seen.append(p["scene"]))
    bus.publish(topics.STATE, {"scene": "a"}, retain=True)
    bus.publish(topics.STATE, {"scene": "b"}, retain=True)
    late = []
    bus.subscribe(topics.STATE, lambda _t, p: late.append(p["scene"]))
    assert seen == ["a", "b"]
    assert late == ["b"]  # only the last one is retained
    assert bus.retained == {topics.STATE: {"scene": "b"}}


def test_publish_is_synchronous_in_the_publishing_thread():
    bus = InMemoryBus()
    order = []
    bus.subscribe("rpghome/#", lambda topic, _p: order.append(topic))
    bus.publish("rpghome/a", {})
    bus.publish("rpghome/b", {})
    assert order == ["rpghome/a", "rpghome/b"]


def test_kill_publishes_the_last_will_and_then_delivers_nothing():
    bus = InMemoryBus(will=(topics.ONLINE, topics.ONLINE_OFF))
    seen = []
    bus.subscribe(topics.ONLINE, lambda _t, p: seen.append(p))
    bus.publish(topics.ONLINE, topics.ONLINE_ON, retain=True)
    bus.kill()
    assert seen == [topics.ONLINE_ON, topics.ONLINE_OFF]
    assert bus.retained[topics.ONLINE] == topics.ONLINE_OFF
    bus.publish(topics.ONLINE, topics.ONLINE_ON)  # the client is gone: nothing more gets out
    assert seen == [topics.ONLINE_ON, topics.ONLINE_OFF]
    assert bus.dead is True


def test_a_handler_that_raises_does_not_break_the_bus(caplog):
    def boom(_topic, _payload):
        raise RuntimeError("handler blew up")

    bus = InMemoryBus()
    seen = []
    with caplog.at_level(logging.ERROR, logger="rpghome.bus"):
        bus.subscribe("rpghome/#", boom)
        bus.subscribe("rpghome/#", lambda _t, p: seen.append(p))
        bus.publish("rpghome/x", {"n": 1})
    assert seen == [{"n": 1}]  # the other handler still got it
    assert "handler" in caplog.text


def test_a_payload_that_is_not_an_object_goes_to_on_invalid():
    """The test bus is strict the way a broker is: a handler never sees a non-object."""
    bus = InMemoryBus()
    seen, bad = [], []
    bus.subscribe(topics.CMD_FILTER, lambda _t, p: seen.append(p), lambda *b: bad.append(b))
    bus.publish(topics.cmd("scene"), "not JSON")
    bus.publish(topics.cmd("scene"), ["scene"])
    bus.publish(topics.cmd("set"), {"name": "master_gain_db"})  # an object: the normal path
    assert seen == [{"name": "master_gain_db"}]
    assert bad == [
        (topics.cmd("scene"), b"not JSON", "not a JSON object, got a str"),
        (topics.cmd("scene"), b'["scene"]', "not a JSON object, got a list"),
    ]
    assert len(bad) == 2  # only the two the subscriber was asked about


def test_a_repeated_command_id_arrives_as_many_times_as_it_was_published():
    """No bus second-guesses an id: what a repeated `(topic, id)` means is the answerer's
    call (`rpghome.bus.recent`; tests/engine/test_link.py is the engine making it)."""
    bus = InMemoryBus()
    seen = []
    bus.subscribe(topics.CMD_FILTER, lambda _t, p: seen.append(p))
    command = {"name": "master_gain_db", "value": -6.0, "id": "1"}
    for _ in range(3):  # what a broker that promises "at least once" can do
        bus.publish(topics.cmd("set"), dict(command))
    assert seen == [command] * 3


def test_a_message_no_handler_can_read_is_logged_when_nobody_asks_for_it(caplog):
    bus = InMemoryBus()
    seen = []
    bus.subscribe("rpghome/#", lambda _t, p: seen.append(p))  # no on_invalid: the old behaviour
    with caplog.at_level(logging.WARNING, logger="rpghome.bus"):
        bus.publish("rpghome/other", 3)
    assert seen == []  # it never reached a handler
    assert "dropped a message" in caplog.text


def test_an_on_invalid_that_raises_does_not_break_the_bus(caplog):
    def boom(_topic, _raw, _reason):
        raise RuntimeError("invalid handler blew up")

    bus = InMemoryBus()
    seen = []
    bus.subscribe("rpghome/#", lambda _t, p: seen.append(p), boom)
    with caplog.at_level(logging.ERROR, logger="rpghome.bus"):
        bus.publish("rpghome/x", "junk")
        bus.publish("rpghome/y", {"n": 1})
    assert seen == [{"n": 1}]  # the bus is still itself afterwards
    assert "invalid handler blew up" in caplog.text


def test_the_bus_is_connected_until_it_is_gone():
    bus = InMemoryBus()
    assert bus.connected is True  # an in-process bus has no broker to reach
    bus.kill()
    assert bus.connected is False  # a client that is gone publishes nothing, state included
    closed = InMemoryBus()
    closed.close()
    assert closed.connected is False


def test_close_stops_the_bus():
    bus = InMemoryBus()
    seen = []
    bus.subscribe("rpghome/#", lambda _t, p: seen.append(p))
    bus.close()
    bus.publish("rpghome/x", {})
    bus.subscribe("rpghome/#", lambda _t, p: seen.append(p))
    assert seen == []
    assert bus.closed is True
