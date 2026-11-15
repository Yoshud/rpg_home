"""Roles and permissions: what a mode of work may do, and what each action needs.

The role names live only in the TOML config (`roles.toml`), never here: a role added
in a config file works without any code change. `REQUIRED_PERMISSION` is the single
table the app server and the MCP server ask later.
"""

import tomllib
from collections.abc import Iterable
from difflib import get_close_matches
from importlib.resources import files
from pathlib import Path

from rpghome.core.errors import RpgHomeError
from rpghome.models.roles import Role, RolesConfig

DEFAULT_CONFIG_FILE = "roles.toml"
_ROLE_KEYS = ("includes", "permissions")
_TOP_KEYS = ("devices", "roles")

PERMISSIONS: frozenset[str] = frozenset(
    {
        "state.read",
        "library.read",
        "scene.play",
        "overlay.play",
        "fx.play",
        "scene.write",
        "overlay.write",
        "fx.write",
        "music.fetch",
        "chat.use",
        "library.review",
        "library.tag",
        "library.fetch",
        "library.generate",
        "library.use_unreviewed",
    }
)

#: What each action needs. Keys are the actions the server, the engine and the MCP
#: tools use; values are permissions from `PERMISSIONS`.
REQUIRED_PERMISSION: dict[str, str] = {
    "cmd.scene": "scene.play",
    "cmd.stop": "scene.play",
    "cmd.set": "scene.play",
    "cmd.overlay": "overlay.play",
    "cmd.fx": "fx.play",
    "cmd.status": "state.read",
    "mode.switch": "state.read",
    "tool.describe": "library.read",
    "tool.list_scenes": "library.read",
    "tool.read_scene": "library.read",
    "tool.write_scene": "scene.write",
    "tool.check_scene": "scene.write",
    "tool.play_scene": "scene.play",
    "tool.overlay": "overlay.play",
    "tool.fx": "fx.play",
    "tool.status": "state.read",
    "library.search": "library.read",
    "library.pools": "library.read",
    "library.rate": "library.review",
    "library.set_tags": "library.tag",
    "library.fetch_effect": "library.fetch",
    "library.fetch_music": "music.fetch",
    "library.generate": "library.generate",
    "chat.send": "chat.use",
}


class RolesConfigError(RpgHomeError):
    """The roles config is broken, or a caller asked about something unknown."""


class PermissionDenied(RpgHomeError):
    """A device tried to switch to a mode above its maximum role."""


def load_roles(path: Path | None = None) -> RolesConfig:
    """Read and validate a roles config; `None` means the packaged `roles.toml`."""
    label, text = _read(path)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RolesConfigError(f"{label}: not valid TOML: {exc}") from None
    return _parse(data, label)


def effective_permissions(config: RolesConfig, role: str) -> frozenset[str]:
    """The role's own permissions plus everything from the roles it includes."""
    _known_role(config, role)
    collected: set[str] = set()
    for name in _reaches(config, role):
        collected |= config.roles[name].permissions
    return frozenset(collected)


def includes(config: RolesConfig, role: str, other: str) -> bool:
    """True when `role` is `other` or includes it transitively."""
    _known_role(config, role)
    _known_role(config, other)
    return other in _reaches(config, role)


def allowed(config: RolesConfig, mode: str, permission: str) -> bool:
    """True when a device in `mode` may use `permission`.

    An unknown mode or permission is a programming error, so it raises instead of
    answering "no".
    """
    if permission not in PERMISSIONS:
        raise RolesConfigError(_unknown(permission, PERMISSIONS, "permission"))
    return permission in effective_permissions(config, mode)


def allowed_action(config: RolesConfig, mode: str, action: str) -> bool:
    """True when a device in `mode` may run `action` (see `REQUIRED_PERMISSION`)."""
    permission = REQUIRED_PERMISSION.get(action)
    if permission is None:
        known = ", ".join(sorted(REQUIRED_PERMISSION))
        raise RolesConfigError(f"unknown action {action!r} (known actions: {known})")
    return allowed(config, mode, permission)


def can_switch(config: RolesConfig, device: str, mode: str) -> bool:
    """True when the device class's maximum role includes `mode`."""
    return includes(config, _max_role(config, device), mode)


def check_switch(config: RolesConfig, device: str, mode: str) -> None:
    """Raise `PermissionDenied` when the device may not switch to `mode`."""
    maximum = _max_role(config, device)
    if not includes(config, maximum, mode):
        raise PermissionDenied(
            f"device {device!r} cannot switch to mode {mode!r}: its maximum role is {maximum!r}"
        )


# --- reading and validating the config -------------------------------------------------


def _read(path: Path | None) -> tuple[str, str]:
    """(label used in error messages, file content)."""
    if path is None:
        label = f"the packaged {DEFAULT_CONFIG_FILE}"
        resource = files("rpghome.managers") / DEFAULT_CONFIG_FILE
        if not resource.is_file():
            raise RolesConfigError(f"{label} is missing")
        return label, resource.read_text(encoding="utf-8")
    try:
        return str(path), path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RolesConfigError(f"{path}: cannot read the roles config: {exc}") from None


def _parse(data: dict[str, object], label: str) -> RolesConfig:
    for key in sorted(set(data) - set(_TOP_KEYS)):
        raise RolesConfigError(f"{label}: " + _unknown(key, _TOP_KEYS, "top-level key"))
    raw_roles = data.get("roles")
    if not isinstance(raw_roles, dict) or not raw_roles:
        raise RolesConfigError(f"{label}: the [roles] table is missing or empty")
    roles = {name: _role(name, body, label) for name, body in raw_roles.items()}
    _check_permissions(roles, label)
    _check_includes(roles, label)
    cycle = _cycle(roles)
    if cycle:
        raise RolesConfigError(f"{label}: cycle in includes: {' -> '.join(cycle)}")
    return RolesConfig(roles=roles, devices=_devices(data, roles, label))


def _role(name: str, body: object, label: str) -> Role:
    if not isinstance(body, dict):
        raise RolesConfigError(f"{label}: role {name!r} is not a table")
    unknown = sorted(set(body) - set(_ROLE_KEYS))
    if unknown:
        raise RolesConfigError(
            f"{label}: role {name!r} has unknown keys: {', '.join(unknown)} "
            f"(allowed: {', '.join(_ROLE_KEYS)})"
        )
    return Role(
        name=name,
        includes=_strings(body.get("includes", []), name, "includes", label),
        permissions=frozenset(_strings(body.get("permissions", []), name, "permissions", label)),
    )


def _strings(value: object, role: str, key: str, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RolesConfigError(
            f"{label}: role {role!r} key {key!r} must be a list of strings, got {value!r}"
        )
    duplicates = sorted({item for item in value if value.count(item) > 1})
    if duplicates:
        listed = ", ".join(repr(item) for item in duplicates)
        raise RolesConfigError(
            f"{label}: role {role!r} key {key!r} has a duplicate entry: {listed}"
        )
    return tuple(value)


def _check_permissions(roles: dict[str, Role], label: str) -> None:
    for role in roles.values():
        for permission in sorted(role.permissions - PERMISSIONS):
            raise RolesConfigError(
                f"{label}: role {role.name!r} has "
                + _unknown(permission, PERMISSIONS, "permission")
            )


def _check_includes(roles: dict[str, Role], label: str) -> None:
    for role in roles.values():
        for other in role.includes:
            if other not in roles:
                raise RolesConfigError(
                    f"{label}: role {role.name!r} includes " + _unknown(other, roles.keys(), "role")
                )


def _cycle(roles: dict[str, Role]) -> list[str] | None:
    """Depth-first search over `includes`; returns the cycle path or None."""
    visiting, done = 0, 1
    state: dict[str, int] = {}
    for start in roles:
        if state.get(start) == done:
            continue
        state[start] = visiting
        path = [start]
        stack = [(start, iter(roles[start].includes))]
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                state[node] = done
                stack.pop()
                path.pop()
                continue
            if state.get(child) == visiting:
                cycle_start = path.index(child)  # the cycle itself, not the way to it
                return [*path[cycle_start:], child]
            if state.get(child) is None:
                state[child] = visiting
                path.append(child)
                stack.append((child, iter(roles[child].includes)))
    return None


def _devices(data: dict[str, object], roles: dict[str, Role], label: str) -> dict[str, str]:
    raw = data.get("devices", {})
    if not isinstance(raw, dict):
        raise RolesConfigError(f"{label}: the [devices] table must map a device to a role name")
    for device, role in raw.items():
        if not isinstance(role, str):
            raise RolesConfigError(
                f"{label}: device {device!r} must map to a role name (a string), got {role!r}"
            )
        if role not in roles:
            raise RolesConfigError(
                f"{label}: device {device!r} points to " + _unknown(role, roles.keys(), "role")
            )
    return dict(raw)


# --- lookups ---------------------------------------------------------------------------


def _unknown(name: str, known: Iterable[str], what: str) -> str:
    candidates = sorted(known)
    hint = get_close_matches(name, candidates, n=1)
    suggestion = f", did you mean {hint[0]!r}?" if hint else ""
    return f"unknown {what} {name!r}{suggestion} (known {what}s: {', '.join(candidates)})"


def _known_role(config: RolesConfig, role: str) -> Role:
    known = config.roles.get(role)
    if known is None:
        raise RolesConfigError(_unknown(role, config.roles.keys(), "role"))
    return known


def _max_role(config: RolesConfig, device: str) -> str:
    maximum = config.devices.get(device)
    if maximum is None:
        known = ", ".join(sorted(config.devices)) or "none"
        raise RolesConfigError(f"unknown device class {device!r} (known devices: {known})")
    _known_role(config, maximum)
    return maximum


def _reaches(config: RolesConfig, role: str) -> frozenset[str]:
    """The role itself plus every role it includes, transitively."""
    seen: set[str] = set()
    pending = [role]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        pending.extend(config.roles[name].includes)
    return frozenset(seen)
