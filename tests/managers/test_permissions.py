"""Roles and permissions: loading the config, checking actions, switching modes."""

import json
import tomllib
from importlib.resources import files
from pathlib import Path

import pytest

from rpghome.managers import permissions as perms
from rpghome.managers.permissions import (
    PERMISSIONS,
    REQUIRED_PERMISSION,
    PermissionDenied,
    RolesConfigError,
    allowed,
    allowed_action,
    can_switch,
    check_switch,
    effective_permissions,
    includes,
    load_roles,
)
from rpghome.models.roles import Role, RolesConfig

SESJA = frozenset({"state.read", "library.read", "scene.play", "overlay.play", "fx.play"})
EDYCJA = SESJA | {"scene.write", "overlay.write", "fx.write", "music.fetch", "chat.use"}
STUDIO = EDYCJA | {
    "library.review",
    "library.tag",
    "library.fetch",
    "library.generate",
    "library.use_unreviewed",
}


@pytest.fixture(scope="module")
def config() -> RolesConfig:
    """The packaged default config (roles.toml)."""
    return load_roles()


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "roles.toml"
    path.write_text(text, encoding="utf-8")
    return path


def role_table(name: str, includes: list[str] = (), permissions: list[str] = ()) -> str:
    """One `[roles.<name>]` TOML table."""
    lines = [f"[roles.{name}]"]
    if includes:
        lines.append(f"includes = {json.dumps(includes)}")
    if permissions:
        lines.append(f"permissions = {json.dumps(permissions)}")
    return "\n".join(lines) + "\n"


# --- the packaged default --------------------------------------------------------------


def test_packaged_default_loads(config):
    assert config.roles["sesja"].name == "sesja"
    assert config.roles["sesja"].includes == ()
    assert config.roles["szybka-edycja"].includes == ("sesja",)
    assert config.devices == {"phone": "szybka-edycja", "mac": "studio"}


def test_load_roles_without_argument_reads_the_packaged_file():
    resource = files("rpghome.managers") / perms.DEFAULT_CONFIG_FILE
    assert resource.is_file(), f"{resource} is not part of the package"
    assert "state.read" in resource.read_text(encoding="utf-8")
    assert load_roles() == load_roles(None)


def test_packaged_default_has_no_duplicate_entries():
    text = (files("rpghome.managers") / perms.DEFAULT_CONFIG_FILE).read_text(encoding="utf-8")
    for name, body in tomllib.loads(text)["roles"].items():
        for key in ("includes", "permissions"):
            entries = body.get(key, [])
            assert len(entries) == len(set(entries)), f"role {name!r} repeats an entry in {key}"
    assert load_roles()  # the packaged file passes the same validation


@pytest.mark.parametrize(
    ("role", "expected"),
    [("sesja", SESJA), ("szybka-edycja", EDYCJA), ("studio", STUDIO)],
)
def test_each_default_role_gets_exactly_its_permissions(config, role, expected):
    assert effective_permissions(config, role) == expected


def test_studio_covers_every_known_permission(config):
    assert STUDIO == PERMISSIONS
    assert len(SESJA) == 5 and len(EDYCJA) == 10 and len(STUDIO) == 15


@pytest.mark.parametrize(
    ("role", "other", "expected"),
    [
        ("studio", "sesja", True),
        ("studio", "szybka-edycja", True),
        ("szybka-edycja", "sesja", True),
        ("sesja", "sesja", True),
        ("sesja", "studio", False),
        ("szybka-edycja", "studio", False),
    ],
)
def test_includes_is_transitive(config, role, other, expected):
    assert includes(config, role, other) is expected


# --- actions per mode -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "permission", "expected"),
    [
        ("sesja", "state.read", True),
        ("sesja", "fx.play", True),
        ("sesja", "scene.write", False),
        ("sesja", "chat.use", False),
        ("sesja", "library.fetch", False),
        ("szybka-edycja", "scene.write", True),
        ("szybka-edycja", "music.fetch", True),
        ("szybka-edycja", "library.fetch", False),
        ("szybka-edycja", "library.generate", False),
        ("studio", "library.use_unreviewed", True),
        ("studio", "scene.play", True),
    ],
)
def test_allowed_per_mode(config, mode, permission, expected):
    assert allowed(config, mode, permission) is expected


@pytest.mark.parametrize(
    ("mode", "action", "expected"),
    [
        ("sesja", "cmd.scene", True),
        ("sesja", "cmd.status", True),
        ("sesja", "tool.list_scenes", True),
        ("sesja", "tool.write_scene", False),
        ("sesja", "tool.check_scene", False),
        ("sesja", "chat.send", False),
        ("sesja", "library.rate", False),
        ("szybka-edycja", "tool.write_scene", True),
        ("szybka-edycja", "library.fetch_music", True),
        ("szybka-edycja", "library.fetch_effect", False),
        ("szybka-edycja", "library.set_tags", False),
        ("szybka-edycja", "library.generate", False),
        ("studio", "library.rate", True),
        ("studio", "library.set_tags", True),
        ("studio", "library.fetch_effect", True),
        ("studio", "library.generate", True),
        ("studio", "chat.send", True),
        ("studio", "cmd.fx", True),
    ],
)
def test_allowed_action_per_mode(config, mode, action, expected):
    assert allowed_action(config, mode, action) is expected


def test_every_action_permission_is_known():
    assert set(REQUIRED_PERMISSION.values()) <= PERMISSIONS
    assert set(REQUIRED_PERMISSION) == {
        "cmd.scene",
        "cmd.stop",
        "cmd.set",
        "cmd.overlay",
        "cmd.fx",
        "cmd.status",
        "mode.switch",
        "tool.describe",
        "tool.list_scenes",
        "tool.read_scene",
        "tool.write_scene",
        "tool.check_scene",
        "tool.play_scene",
        "tool.overlay",
        "tool.fx",
        "tool.status",
        "library.search",
        "library.pools",
        "library.rate",
        "library.set_tags",
        "library.fetch_effect",
        "library.fetch_music",
        "library.generate",
        "chat.send",
    }


def test_every_permission_is_used_by_a_default_role(config):
    used = set()
    for role in config.roles:
        used |= effective_permissions(config, role)
    assert used == set(PERMISSIONS)


def test_no_role_names_in_the_logic():
    source = Path(perms.__file__).read_text(encoding="utf-8")
    for name in load_roles().roles:
        assert name not in source


# --- switching modes --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("device", "mode", "expected"),
    [
        ("phone", "sesja", True),
        ("phone", "szybka-edycja", True),
        ("phone", "studio", False),
        ("mac", "sesja", True),
        ("mac", "szybka-edycja", True),
        ("mac", "studio", True),
    ],
)
def test_can_switch(config, device, mode, expected):
    assert can_switch(config, device, mode) is expected


def test_check_switch_denies_above_the_maximum_role(config):
    with pytest.raises(PermissionDenied) as excinfo:
        check_switch(config, "phone", "studio")
    message = str(excinfo.value)
    assert "phone" in message
    assert "studio" in message
    assert "szybka-edycja" in message


def test_check_switch_accepts_allowed_modes(config):
    assert check_switch(config, "phone", "sesja") is None
    assert check_switch(config, "mac", "studio") is None


# --- validation errors ------------------------------------------------------------------


def test_unknown_permission_suggests_the_closest_one(tmp_path):
    path = write_config(tmp_path, '[roles.gm]\npermissions = ["state.read", "scene.plai"]\n')
    with pytest.raises(RolesConfigError) as excinfo:
        load_roles(path)
    message = str(excinfo.value)
    assert "roles.toml" in message
    assert "'gm'" in message
    assert "scene.plai" in message
    assert "did you mean 'scene.play'" in message


def test_unknown_included_role(tmp_path):
    body = role_table("gm-main", permissions=["state.read"]) + role_table(
        "helper", includes=["gm-majn"]
    )
    path = write_config(tmp_path, body)
    with pytest.raises(RolesConfigError) as excinfo:
        load_roles(path)
    message = str(excinfo.value)
    assert "role 'helper' includes unknown role 'gm-majn'" in message
    assert "did you mean 'gm-main'" in message


def test_cycle_of_two_roles(tmp_path):
    body = role_table("a", includes=["b"]) + role_table("b", includes=["a"])
    path = write_config(tmp_path, body)
    with pytest.raises(RolesConfigError) as excinfo:
        load_roles(path)
    assert "cycle in includes: a -> b -> a" in str(excinfo.value)


def test_cycle_of_three_roles_shows_the_path(tmp_path):
    body = (
        role_table("a", includes=["b"])
        + role_table("b", includes=["c"])
        + role_table("c", includes=["a"])
    )
    path = write_config(tmp_path, body)
    with pytest.raises(RolesConfigError) as excinfo:
        load_roles(path)
    assert "cycle in includes: a -> b -> c -> a" in str(excinfo.value)


def test_role_including_itself(tmp_path):
    path = write_config(tmp_path, role_table("a", includes=["a"]))
    with pytest.raises(RolesConfigError) as excinfo:
        load_roles(path)
    assert "cycle in includes: a -> a" in str(excinfo.value)


def test_cycle_behind_an_acyclic_role(tmp_path):
    body = (
        role_table("top", includes=["a"])
        + role_table("a", includes=["b"])
        + role_table("b", includes=["a"])
    )
    path = write_config(tmp_path, body)
    with pytest.raises(RolesConfigError) as excinfo:
        load_roles(path)
    assert "cycle in includes: a -> b -> a" in str(excinfo.value)


def test_device_points_to_a_missing_role(tmp_path):
    path = write_config(tmp_path, '[roles.a]\n[devices]\nphone = "beta"\n')
    with pytest.raises(RolesConfigError) as excinfo:
        load_roles(path)
    message = str(excinfo.value)
    assert "device 'phone' points to unknown role 'beta'" in message
    assert "known roles: a" in message


def test_device_value_must_be_a_role_name(tmp_path):
    path = write_config(tmp_path, role_table("a") + "\n[devices]\nphone = 5\n")
    with pytest.raises(RolesConfigError) as excinfo:
        load_roles(path)
    assert "device 'phone' must map to a role name (a string), got 5" in str(excinfo.value)


def test_unknown_top_level_key_suggests_the_closest_one(tmp_path):
    path = write_config(tmp_path, role_table("a") + '\n[device]\nphone = "a"\n')
    with pytest.raises(RolesConfigError) as excinfo:
        load_roles(path)
    message = str(excinfo.value)
    assert "unknown top-level key 'device', did you mean 'devices'?" in message
    assert "known top-level keys: devices, roles" in message


def test_unknown_top_level_table_without_a_typo(tmp_path):
    path = write_config(tmp_path, role_table("a") + "\n[permissions]\nstate.read = true\n")
    with pytest.raises(RolesConfigError) as excinfo:
        load_roles(path)
    assert "unknown top-level key 'permissions'" in str(excinfo.value)


def test_duplicate_permissions_are_rejected(tmp_path):
    path = write_config(tmp_path, role_table("a", permissions=["state.read", "state.read"]))
    with pytest.raises(RolesConfigError) as excinfo:
        load_roles(path)
    assert "role 'a' key 'permissions' has a duplicate entry: 'state.read'" in str(excinfo.value)


def test_duplicate_includes_are_rejected(tmp_path):
    body = role_table("a", includes=["b", "c", "b"]) + role_table("b") + role_table("c")
    path = write_config(tmp_path, body)
    with pytest.raises(RolesConfigError) as excinfo:
        load_roles(path)
    assert "role 'a' key 'includes' has a duplicate entry: 'b'" in str(excinfo.value)


def test_invalid_toml(tmp_path):
    path = write_config(tmp_path, "[roles.a\npermissions = [\n")
    with pytest.raises(RolesConfigError) as excinfo:
        load_roles(path)
    assert "not valid TOML" in str(excinfo.value)
    assert "roles.toml" in str(excinfo.value)


def test_empty_roles_table(tmp_path):
    path = write_config(tmp_path, "[roles]\n")
    with pytest.raises(RolesConfigError) as excinfo:
        load_roles(path)
    assert "[roles] table is missing or empty" in str(excinfo.value)


def test_missing_roles_table(tmp_path):
    path = write_config(tmp_path, '[devices]\nphone = "a"\n')
    with pytest.raises(RolesConfigError) as excinfo:
        load_roles(path)
    assert "[roles] table is missing or empty" in str(excinfo.value)


def test_missing_file(tmp_path):
    path = tmp_path / "absent.toml"
    with pytest.raises(RolesConfigError) as excinfo:
        load_roles(path)
    assert "absent.toml" in str(excinfo.value)


@pytest.mark.parametrize(
    "body",
    [
        '[roles.a]\npermissions = "state.read"\n',  # not a list
        '[roles.a]\npermissions = ["state.read", 7]\n',  # not strings
        '[roles.a]\nincludes = "sesja"\n',  # not a list
        "[roles.a]\nincludes = [3]\n",  # not strings
        '[roles.a]\npermisions = ["state.read"]\n',  # typo: unknown key
        '[roles.a]\npermissions = ["state.read"]\nmax = 2\n',  # unknown key
        "[roles.a]\npermissions = 5\n",
    ],
)
def test_bad_role_tables(tmp_path, body):
    path = write_config(tmp_path, body)
    with pytest.raises(RolesConfigError) as excinfo:
        load_roles(path)
    assert "roles.toml" in str(excinfo.value)
    assert "'a'" in str(excinfo.value)


def test_devices_table_is_optional(tmp_path):
    config = load_roles(write_config(tmp_path, '[roles.a]\npermissions = ["state.read"]\n'))
    assert config.devices == {}
    with pytest.raises(RolesConfigError) as excinfo:
        can_switch(config, "phone", "a")
    assert "unknown device class 'phone'" in str(excinfo.value)


def test_devices_table_is_not_a_table(tmp_path):
    path = write_config(tmp_path, 'devices = "phone"\n[roles.a]\n')
    with pytest.raises(RolesConfigError) as excinfo:
        load_roles(path)
    assert "[devices]" in str(excinfo.value)


# --- unknown names are programming errors ----------------------------------------------


def test_unknown_mode_or_permission_raises(config):
    with pytest.raises(RolesConfigError) as excinfo:
        allowed(config, "no-such-mode", "state.read")
    assert "unknown role 'no-such-mode'" in str(excinfo.value)
    with pytest.raises(RolesConfigError) as excinfo:
        allowed(config, "sesja", "no.such.permission")
    assert "unknown permission 'no.such.permission'" in str(excinfo.value)
    with pytest.raises(RolesConfigError):
        effective_permissions(config, "no-such-role")
    with pytest.raises(RolesConfigError):
        includes(config, "sesja", "no-such-role")


def test_unknown_action_raises(config):
    with pytest.raises(RolesConfigError) as excinfo:
        allowed_action(config, "sesja", "cmd.dance")
    assert "unknown action 'cmd.dance'" in str(excinfo.value)


def test_unknown_device_or_mode_when_switching_raises(config):
    with pytest.raises(RolesConfigError) as excinfo:
        can_switch(config, "no-such-device", "sesja")
    assert "unknown device class 'no-such-device'" in str(excinfo.value)
    with pytest.raises(RolesConfigError) as excinfo:
        check_switch(config, "phone", "no-such-mode")
    assert "unknown role 'no-such-mode'" in str(excinfo.value)


# --- a new role needs no code change ----------------------------------------------------


NEW_ROLE = """
[roles.sesja]
permissions = ["state.read", "library.read", "scene.play", "overlay.play", "fx.play"]

[roles.gm-light]
includes = ["sesja"]
permissions = ["chat.use"]

[roles.studio]
includes = ["sesja"]
permissions = ["library.review"]

[devices]
tablet = "gm-light"
mac = "studio"
"""


def test_new_role_defined_only_in_the_config(tmp_path):
    config = load_roles(write_config(tmp_path, NEW_ROLE))
    assert effective_permissions(config, "gm-light") == SESJA | {"chat.use"}
    assert includes(config, "gm-light", "sesja")
    assert allowed(config, "gm-light", "chat.use")
    assert allowed_action(config, "gm-light", "chat.send")
    assert not allowed_action(config, "gm-light", "library.rate")
    assert not allowed_action(config, "gm-light", "tool.write_scene")
    assert can_switch(config, "tablet", "sesja")
    assert can_switch(config, "tablet", "gm-light")
    assert not can_switch(config, "tablet", "studio")
    with pytest.raises(PermissionDenied):
        check_switch(config, "tablet", "studio")


def test_role_with_several_includes(tmp_path):
    body = (
        role_table("a", permissions=["state.read"])
        + role_table("b", permissions=["fx.play"])
        + role_table("c", includes=["a", "b"], permissions=["chat.use"])
    )
    config = load_roles(write_config(tmp_path, body))
    assert effective_permissions(config, "c") == {"state.read", "fx.play", "chat.use"}
    assert includes(config, "c", "b")
    assert not includes(config, "a", "b")


def test_diamond_in_includes_is_not_a_cycle(tmp_path):
    body = (
        role_table("a", includes=["b", "c"], permissions=["library.read"])
        + role_table("b", includes=["d"], permissions=["fx.play"])
        + role_table("c", includes=["d"], permissions=["chat.use"])
        + role_table("d", permissions=["state.read"])
    )
    config = load_roles(write_config(tmp_path, body))
    assert effective_permissions(config, "a") == {
        "library.read",
        "fx.play",
        "chat.use",
        "state.read",
    }
    assert includes(config, "a", "d")
    assert includes(config, "b", "d")
    assert includes(config, "c", "d")
    assert effective_permissions(config, "b") == {"state.read", "fx.play"}


def test_config_can_be_built_in_code():
    config = RolesConfig(
        roles={
            "a": Role(name="a", permissions=frozenset({"state.read"})),
            "b": Role(name="b", includes=("a",), permissions=frozenset({"chat.use"})),
        },
        devices={"phone": "a"},
    )
    assert effective_permissions(config, "b") == {"state.read", "chat.use"}
    assert can_switch(config, "phone", "a")
    assert not can_switch(config, "phone", "b")
