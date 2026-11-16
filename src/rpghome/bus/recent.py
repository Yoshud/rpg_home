"""The commands a service has recently answered, keyed by `(topic, id)`.

QoS 1 promises "at least once": the broker can hand the same message twice, and a sender can
also use an id it has already used. Only the service that answers a command can say what a
repeated `(topic, id)` means, because its own answer is part of the answer - so this
bookkeeping sits next to the answers, not in the transport. A bus delivers what the broker
gives it; the answerer decides.

- the same message again is a redelivery: it is not applied again, and the earlier event is
  published again, so the sender is never left waiting for an answer that already went out;
- the same id with a different payload is the sender's mistake: it is refused, and the service
  keeps doing what it was doing.

A message without an `id` cannot be recognised this way and is taken as it comes.
"""

import threading
from dataclasses import dataclass
from typing import Any

#: how many answered commands to remember against a redelivery or a reused id
RECENT_COMMANDS = 500


def message_id(payload: Any) -> str | None:
    """The `id` of a command message, or None when it carries none that can identify it."""
    if not isinstance(payload, dict):
        return None
    msg_id = payload.get("id")
    return msg_id if isinstance(msg_id, str) and msg_id else None


@dataclass(frozen=True)
class Answered:
    """What a `(topic, id)` was: the message as it arrived, and the event that answered it."""

    payload: Any
    event: dict


class RecentCommands:
    """`(topic, id)` -> what it was answered with, of the last `limit` answered commands."""

    def __init__(self, limit: int = RECENT_COMMANDS) -> None:
        self._limit = limit
        self._answered: dict[tuple[str, str], Answered] = {}
        self._lock = threading.Lock()  # the command thread answers, close() may drain too

    def answer_of(self, topic: str, payload: Any) -> Answered | None:
        """The earlier answer for this `(topic, id)`, or None when it has no id or is new."""
        msg_id = message_id(payload)
        if msg_id is None:
            return None
        with self._lock:
            return self._answered.get((topic, msg_id))

    def remember(self, topic: str, payload: Any, event: dict) -> None:
        """Record the answer just given; the oldest beyond the limit are forgotten."""
        msg_id = message_id(payload)
        if msg_id is None:
            return
        with self._lock:
            self._answered[(topic, msg_id)] = Answered(payload, event)
            while len(self._answered) > self._limit:  # the oldest ids are forgotten
                self._answered.pop(next(iter(self._answered)))
