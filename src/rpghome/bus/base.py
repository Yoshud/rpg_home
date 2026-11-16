"""The Bus: everything a service needs from the message bus, and nothing else.

Payloads are JSON objects (dicts). Topic filters use the MQTT wildcards: `+` matches one
level, `#` matches this level and everything below it.

Two implementations: `MqttBus` (a real broker, the only way between processes) and
`InMemoryBus` (one process, for tests).
"""

from collections.abc import Callable
from typing import Protocol, runtime_checkable

Handler = Callable[[str, dict], None]

#: What a bus calls instead of the handler when a message is not a JSON object:
#: `(topic, raw bytes, why it is not one)`. A subscriber that does not pass one gets the
#: message logged and dropped, as it always did.
InvalidHandler = Callable[[str, bytes, str], None]


@runtime_checkable
class Bus(Protocol):
    """publish / subscribe / close. Handlers run in the bus's own thread."""

    def publish(self, topic: str, payload: dict, retain: bool = False) -> None: ...

    def subscribe(
        self, topic_filter: str, handler: Handler, on_invalid: InvalidHandler | None = None
    ) -> None: ...

    def close(self) -> None: ...

    @property
    def connected(self) -> bool:
        """True when a publish now would reach the subscribers. A bus that is retrying a
        broker that is down is not connected: a service should not queue state behind it."""
        ...


def topic_matches(topic_filter: str, topic: str) -> bool:
    """MQTT matching: does `topic_filter` (`+`, `#`) cover this concrete `topic`?"""
    if topic_filter == topic:
        return True
    parts, levels = topic_filter.split("/"), topic.split("/")
    for i, part in enumerate(parts):
        if part == "#":
            return i <= len(levels)  # `#` also matches the level it sits at: a/# covers a
        if i >= len(levels):
            return False
        if part != "+" and part != levels[i]:
            return False
    return len(parts) == len(levels)
