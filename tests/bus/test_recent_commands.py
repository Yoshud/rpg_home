"""RecentCommands: what a repeated `(topic, id)` means for the service that answers it.

The bus hands over everything the broker gives it (test_mqtt_bus.py); this is the bookkeeping
the answerer keeps, and tests/engine/test_link.py drives it through the real engine.
"""

from rpghome.bus import topics
from rpghome.bus.recent import RECENT_COMMANDS, RecentCommands, message_id

SET_TOPIC = topics.cmd("set")
FIRST = {"name": "master_gain_db", "value": -6.0, "id": "1"}
OK = {"type": "ok", "command": "set", "id": "1"}


def recent_with_first() -> RecentCommands:
    recent = RecentCommands()
    recent.remember(SET_TOPIC, FIRST, OK)
    return recent


def test_a_command_is_recognised_by_its_topic_and_id():
    assert message_id(FIRST) == "1"
    recent = recent_with_first()
    earlier = recent.answer_of(SET_TOPIC, dict(FIRST))  # the same JSON, decoded again
    assert earlier is not None
    assert earlier.payload == FIRST  # so the caller can tell a redelivery from a new message
    assert earlier.event == OK  # and answer a redelivery with what it answered before


def test_a_different_payload_under_the_same_id_is_not_the_same_message():
    recent = recent_with_first()
    again = {"name": "master_gain_db", "value": -3.0, "id": "1"}  # the review's trigger
    earlier = recent.answer_of(SET_TOPIC, again)
    assert earlier is not None
    assert earlier.payload != again  # the caller refuses it as the sender's mistake


def test_the_same_id_on_another_topic_is_another_command():
    recent = recent_with_first()
    assert recent.answer_of(topics.cmd("scene"), FIRST) is None


def test_a_message_without_an_id_is_never_recognised():
    recent = recent_with_first()
    for payload in (
        {"name": "master_gain_db", "value": -6.0},  # no id at all
        {"name": "master_gain_db", "value": -6.0, "id": ""},  # an empty one identifies nothing
        {"name": "master_gain_db", "value": -6.0, "id": 1},  # not a string
        "not an object",
        None,
    ):
        assert recent.answer_of(SET_TOPIC, payload) is None
        recent.remember(SET_TOPIC, payload, OK)  # remembered as nothing: it cannot be found
    assert recent.answer_of(SET_TOPIC, FIRST) is not None  # the one real id is still there


def test_only_the_last_commands_are_kept():
    recent = RecentCommands(limit=3)
    ids = [f"id-{n}" for n in range(6)]
    for msg_id in ids:
        recent.remember(SET_TOPIC, {"id": msg_id}, {"type": "ok", "id": msg_id})
    kept = [msg_id for msg_id in ids if recent.answer_of(SET_TOPIC, {"id": msg_id}) is not None]
    assert kept == ids[-3:], kept

    big = RecentCommands()  # the default is what the engine uses
    for n in range(RECENT_COMMANDS + 100):
        big.remember(SET_TOPIC, {"id": f"id-{n}"}, OK)
    assert big.answer_of(SET_TOPIC, {"id": "id-0"}) is None
    assert big.answer_of(SET_TOPIC, {"id": f"id-{RECENT_COMMANDS + 99}"}) is not None
