"""Roles (the modes of work) and the maximum role of each device class.

Data only: the logic over these values is in `rpghome.managers.permissions`.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Role:
    """One mode of work: its own permissions, plus the roles it includes."""

    name: str
    includes: tuple[str, ...] = ()
    permissions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RolesConfig:
    """All roles by name, and the maximum role of each device class."""

    roles: dict[str, Role]
    devices: dict[str, str] = field(default_factory=dict)
