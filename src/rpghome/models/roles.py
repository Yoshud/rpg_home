"""Roles (the modes of work) and the maximum role of each device class.

Data only: the logic over these values is in `rpghome.managers.permissions`. Pydantic v2
models, as in `scene.py`: frozen, extra fields forbidden.
"""

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Role(_Model):
    """One mode of work: its own permissions, plus the roles it includes."""

    name: str
    includes: tuple[str, ...] = ()
    permissions: frozenset[str] = frozenset()


class RolesConfig(_Model):
    """All roles by name, and the maximum role of each device class."""

    roles: dict[str, Role]
    devices: dict[str, str] = Field(default_factory=dict)
