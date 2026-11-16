"""Every topic rpghome uses, in one place.

    rpghome/cmd/<command>   a command for the engine: scene, overlay, fx, set, stop
    rpghome/state           the engine's state, retained: a new subscriber gets it at once
    rpghome/events          the answer to a command (and later other engine events)
    rpghome/engine/online   retained {"online": true}; the engine's last will is
                            {"online": false}, so a killed engine is seen as offline

Browsers never talk to the broker: they go through the app server, which publishes here.
"""

COMMANDS = ("scene", "overlay", "fx", "set", "stop")

CMD_PREFIX = "rpghome/cmd"
CMD_FILTER = "rpghome/cmd/#"
STATE = "rpghome/state"
EVENTS = "rpghome/events"
ONLINE = "rpghome/engine/online"

ONLINE_ON = {"online": True}
ONLINE_OFF = {"online": False}


def cmd(command: str) -> str:
    """The topic of one command."""
    return f"{CMD_PREFIX}/{command}"


def command_of(topic: str) -> str | None:
    """The command name a command topic carries, or None when the topic is not one."""
    if not topic.startswith(f"{CMD_PREFIX}/"):
        return None
    return topic[len(CMD_PREFIX) + 1 :] or None
