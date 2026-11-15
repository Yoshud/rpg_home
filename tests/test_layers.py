"""Imports between rpghome subpackages go only downwards, and the engine package uses only
the bottom of rpghome (see README, "Layout")."""

import ast
from pathlib import Path

SRC = Path(__file__).parents[1] / "src" / "rpghome"
ENGINE_SRC = Path(__file__).parents[1] / "src" / "rpghome_engine"

_LOW = {"core", "models", "repositories", "bus", "infra", "managers"}
ALLOWED: dict[str, set[str]] = {
    "core": set(),
    "models": {"core"},
    "repositories": {"core", "models"},
    "bus": {"core", "models"},
    "infra": {"core", "models"},
    "managers": {"core", "models", "repositories", "bus", "infra"},
    # Entry points: anything below, but not each other.
    "api": _LOW,
    "cli": _LOW,
    "mcp": _LOW,
    "runner": _LOW,
}
# rpghome_engine gets scene data and commands over the bus only; rpghome never imports it.
ENGINE_ALLOWED = {"core", "models", "bus"}


def _module_name(path: Path, root: Path) -> list[str]:
    parts = [root.name, *path.relative_to(root).with_suffix("").parts]
    return parts[:-1] if parts[-1] == "__init__" else parts


def _imported(tree: ast.AST, module: list[str], is_package: bool) -> list[str]:
    """Absolute dotted names of everything a module imports."""
    package = module if is_package else module[:-1]
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                prefix = ".".join(base + ([node.module] if node.module else []))
            else:
                prefix = node.module or ""
            # `from rpghome import cli` imports a subpackage: record rpghome.cli.
            names += [f"{prefix}.{alias.name}" for alias in node.names]
    return names


def _modules(root: Path):
    """(dotted module parts, absolute imported names) for every module under `root`."""
    for path in sorted(root.rglob("*.py")):
        module = _module_name(path, root)
        tree = ast.parse(path.read_text(), filename=str(path))
        yield module, _imported(tree, module, path.name == "__init__.py")


def check_layers(root: Path) -> list[str]:
    """Return one message per import that breaks the layer rules under package `root`."""
    top = root.name
    errors = []
    for module, imported in _modules(root):
        layer = module[1] if len(module) > 1 else None
        dotted = ".".join(module)
        if layer is not None and layer not in ALLOWED:
            errors.append(f"{dotted}: unknown layer {layer!r} (add it to the rules)")
            continue
        allowed = ALLOWED.get(layer, set()) if layer else set()
        for name in imported:
            parts = name.split(".")
            if parts[0] == ENGINE_SRC.name:
                errors.append(f"{dotted}: imports {name}; {top} must not import the engine")
                continue
            if parts[0] != top or len(parts) < 2:
                continue
            target = parts[1]
            if target == layer or target not in ALLOWED:
                continue  # same layer, or a plain name from the root package
            if target not in allowed:
                rule = (
                    f"{layer} may import only: {', '.join(sorted(allowed)) or 'nothing'}"
                    if layer
                    else f"the {top} root package must not import subpackages"
                )
                errors.append(f"{dotted}: imports {name} ({top}.{target}); {rule}")
    return errors


def check_engine(root: Path, app: str = SRC.name) -> list[str]:
    """Return one message per import under the engine package `root` that reaches into the
    app package `app` beyond the layers the engine may use."""
    errors = []
    for module, imported in _modules(root):
        for name in imported:
            parts = name.split(".")
            if parts[0] != app:
                continue  # itself, the standard library or a third-party library
            if len(parts) < 2 or parts[1] not in ENGINE_ALLOWED:
                rule = (
                    f"{root.name} may import from {app} only: {', '.join(sorted(ENGINE_ALLOWED))}"
                )
                errors.append(f"{'.'.join(module)}: imports {name}; {rule}")
    return errors


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)


def test_package_respects_layers():
    errors = check_layers(SRC)
    assert not errors, "layer violations:\n" + "\n".join(errors)


def test_engine_uses_only_core_models_bus():
    errors = check_engine(ENGINE_SRC)
    assert not errors, "engine import violations:\n" + "\n".join(errors)


def test_checker_catches_violations(tmp_path):
    pkg = tmp_path / "rpghome"
    files = {
        "__init__.py": "",
        "core/__init__.py": "",
        "core/config.py": "from rpghome.managers import scenes\n",
        "infra/__init__.py": "",
        "infra/ffmpeg.py": "from ..core import config\nfrom . import other\n",
        "infra/other.py": "",
        "managers/__init__.py": "",
        "managers/scenes.py": "import rpghome.infra.ffmpeg\nfrom ..cli import main\n",
        "cli/__init__.py": "",
        "cli/main.py": "from rpghome.api.app import app\n",
        "api/__init__.py": "",
        "api/app.py": "",
        "repositories/__init__.py": "",
        "weird/__init__.py": "",
    }
    _write_tree(pkg, files)

    errors = check_layers(pkg)
    assert len(errors) == 4, errors
    joined = "\n".join(errors)
    assert "rpghome.core.config: imports rpghome.managers.scenes" in joined
    assert "core may import only: nothing" in joined
    assert "rpghome.managers.scenes: imports rpghome.cli.main (rpghome.cli)" in joined
    assert "rpghome.cli.main: imports rpghome.api.app" in joined
    assert "rpghome.weird: unknown layer 'weird'" in joined


def test_checker_catches_app_importing_engine(tmp_path):
    pkg = tmp_path / "rpghome"
    _write_tree(
        pkg,
        {
            "__init__.py": "",
            "core/__init__.py": "",
            "api/__init__.py": "",
            "api/app.py": "import rpghome_engine.core\n",
            "cli/__init__.py": "",
            "cli/main.py": "def f():\n    from rpghome_engine import __main__\n",
        },
    )

    errors = check_layers(pkg)
    assert len(errors) == 2, errors
    joined = "\n".join(errors)
    assert "rpghome.api.app: imports rpghome_engine.core; rpghome must not import" in joined
    assert "rpghome.cli.main: imports rpghome_engine.__main__" in joined


def test_checker_catches_engine_importing_app(tmp_path):
    pkg = tmp_path / "rpghome_engine"
    _write_tree(
        pkg,
        {
            "__init__.py": "",
            "core.py": (
                "import numpy\nfrom rpghome.core.config import Settings\n"
                "from rpghome.models import scene\nfrom . import mixer\n"
                "from rpghome_engine.mixer import Mixer\n"
            ),
            "mixer.py": "import rpghome.bus\nfrom rpghome.repositories import catalog\n",
            "__main__.py": "def f():\n    from rpghome import managers\n    import rpghome\n",
        },
    )

    errors = check_engine(pkg)
    assert len(errors) == 3, errors
    joined = "\n".join(errors)
    assert "rpghome_engine.mixer: imports rpghome.repositories.catalog" in joined
    assert "may import from rpghome only: bus, core, models" in joined
    assert "rpghome_engine.__main__: imports rpghome.managers" in joined
    assert "rpghome_engine.__main__: imports rpghome;" in joined
