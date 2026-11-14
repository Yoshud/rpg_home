"""Imports between rpghome subpackages go only downwards (see README, "Layout")."""

import ast
from pathlib import Path

SRC = Path(__file__).parents[1] / "src" / "rpghome"

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
    # The engine gets scene data and commands over the bus only.
    "engine": {"core", "models", "bus"},
}


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


def check_layers(root: Path) -> list[str]:
    """Return one message per import that breaks the layer rules under package `root`."""
    top = root.name
    errors = []
    for path in sorted(root.rglob("*.py")):
        module = _module_name(path, root)
        is_package = path.name == "__init__.py"
        layer = module[1] if len(module) > 1 else None
        dotted = ".".join(module)
        if layer is not None and layer not in ALLOWED:
            errors.append(f"{dotted}: unknown layer {layer!r} (add it to the rules)")
            continue
        allowed = ALLOWED.get(layer, set()) if layer else set()
        tree = ast.parse(path.read_text(), filename=str(path))
        for name in _imported(tree, module, is_package):
            parts = name.split(".")
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


def test_package_respects_layers():
    errors = check_layers(SRC)
    assert not errors, "layer violations:\n" + "\n".join(errors)


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
        "engine/__init__.py": "def f():\n    from rpghome import repositories\n",
        "cli/__init__.py": "",
        "cli/main.py": "from rpghome.api.app import app\n",
        "api/__init__.py": "",
        "api/app.py": "",
        "repositories/__init__.py": "",
        "weird/__init__.py": "",
    }
    for name, text in files.items():
        (pkg / name).parent.mkdir(parents=True, exist_ok=True)
        (pkg / name).write_text(text)

    errors = check_layers(pkg)
    assert len(errors) == 5, errors
    joined = "\n".join(errors)
    assert "rpghome.core.config: imports rpghome.managers.scenes" in joined
    assert "core may import only: nothing" in joined
    assert "rpghome.managers.scenes: imports rpghome.cli.main (rpghome.cli)" in joined
    assert "rpghome.engine: imports rpghome.repositories" in joined
    assert "engine may import only: bus, core, models" in joined
    assert "rpghome.cli.main: imports rpghome.api.app" in joined
    assert "rpghome.weird: unknown layer 'weird'" in joined
