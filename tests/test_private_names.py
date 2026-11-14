"""Shared helpers have public names.

A module that other modules use exports its shared helpers without a leading
underscore. This test fails when code outside a guarded module imports, or reads
as an attribute, one of its underscore-prefixed names. Tests may still exercise
private helpers directly, so only the shipped package and the repository's
helper scripts are checked.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "agentic_preflight"
#: The modules whose underscore-prefixed names must stay inside them.
GUARDED = frozenset(
    {
        f"{PACKAGE}.run_summary",
        f"{PACKAGE}.telemetry",
        f"{PACKAGE}.store",
        f"{PACKAGE}.runs._session",
    }
)
SCANNED = (PACKAGE, "evals", "tools", "scripts")


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve(module: str, node: ast.ImportFrom, is_package: bool) -> str:
    """The absolute module an ``ImportFrom`` names."""
    if not node.level:
        return node.module or ""
    base = module.split(".")
    # A package's own ``__init__`` resolves ``.`` to itself.
    drop = node.level - 1 if is_package else node.level
    anchor = base[: len(base) - drop] if drop else base
    return ".".join([*anchor, node.module] if node.module else anchor)


def _private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _violations(path: Path) -> list[str]:
    module = _module_name(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    is_package = path.name == "__init__.py"
    #: Local names bound to a guarded module, such as ``telemetry`` or ``storemod``.
    aliases: dict[str, str] = {}
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in GUARDED and alias.asname:
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom):
            source = _resolve(module, node, is_package)
            for alias in node.names:
                target = f"{source}.{alias.name}"
                if target in GUARDED and target != module:
                    aliases[alias.asname or alias.name] = target
                elif source in GUARDED and source != module and _private(alias.name):
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno} imports {target}")

    def guarded(expr: ast.expr) -> str | None:
        """The guarded module an expression names: an alias, or a dotted module path."""
        if isinstance(expr, ast.Name) and expr.id in aliases:
            return aliases[expr.id]
        dotted = _dotted(expr)
        return dotted if dotted in GUARDED else None

    # Out of scope: a guarded module rebound to another name after import, and
    # modules reached through ``importlib`` or ``sys.modules``.
    for node in ast.walk(tree):
        owner = name = None
        if isinstance(node, ast.Attribute):
            owner, name = guarded(node.value), node.attr
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            owner, name = guarded(node.args[0]), node.args[1].value
        if owner is not None and name is not None and owner != module and _private(name):
            found.append(f"{path.relative_to(ROOT)}:{node.lineno} uses {owner}.{name}")
    return sorted(found, key=lambda item: int(item.split(" ", 1)[0].rsplit(":", 1)[1]))


def _dotted(expr: ast.expr) -> str | None:
    """``a.b.c`` for a chain of names and attributes, else ``None``."""
    parts: list[str] = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name):
        return None
    return ".".join([expr.id, *reversed(parts)])


def _sources() -> list[Path]:
    paths: list[Path] = []
    for top in SCANNED:
        folder = ROOT / top
        if folder.is_dir():
            paths.extend(
                path
                for path in folder.rglob("*.py")
                # Eval cases are fixture repositories, not repository code.
                if "cases" not in path.relative_to(folder).parts
            )
    return sorted(paths)


def test_no_module_uses_a_private_name_of_a_guarded_module():
    found = [item for path in _sources() for item in _violations(path)]

    assert found == [], "\n".join(found)


def test_the_scan_detects_each_kind_of_private_use(tmp_path, monkeypatch):
    package = tmp_path / PACKAGE
    (package / "runs").mkdir(parents=True)
    (package / "runs" / "probe.py").write_text(
        "from ._session import Session, _apply\n"
        "from .. import store as storemod, telemetry\n"
        "storemod._atomic_write\n"
        "telemetry.summarize\n"
        "telemetry._now\n"
        "import agentic_preflight.telemetry\n"
        "agentic_preflight.telemetry._describe\n"
        "agentic_preflight.telemetry.describe_error\n"
        "getattr(storemod, '_atomic_write')\n"
        "getattr(telemetry, 'summarize')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(f"{__name__}.ROOT", tmp_path)

    found = _violations(package / "runs" / "probe.py")

    assert [item.split(" ", 1)[1] for item in found] == [
        f"imports {PACKAGE}.runs._session._apply",
        f"uses {PACKAGE}.store._atomic_write",
        f"uses {PACKAGE}.telemetry._now",
        f"uses {PACKAGE}.telemetry._describe",
        f"uses {PACKAGE}.store._atomic_write",
    ]
