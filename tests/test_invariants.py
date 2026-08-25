"""Whole-package invariants that must hold regardless of what any module does."""

from __future__ import annotations

import ast
import pkgutil
from pathlib import Path

import agentic_preflight

PACKAGE_ROOT = Path(agentic_preflight.__file__).parent

#: Python never calls an LLM. That is what makes the tool agent-agnostic for
#: free, and what lets it promise no API keys, no model config, no token
#: budgets. An import of any of these would silently repeal that promise, so it
#: is asserted rather than documented.
FORBIDDEN_IMPORTS = {
    "anthropic",
    "openai",
    "httpx",
    "requests",
    "aiohttp",
    "urllib3",
    "litellm",
    "google.generativeai",
}


def _module_files() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_no_module_imports_an_llm_or_http_client():
    offenders: list[str] = []
    for path in _module_files():
        for name in _imported_names(path):
            root = name.split(".")[0]
            if name in FORBIDDEN_IMPORTS or root in FORBIDDEN_IMPORTS:
                offenders.append(f"{path.relative_to(PACKAGE_ROOT)} imports {name}")
    assert offenders == [], (
        "agentic-preflight must never call an LLM or reach the network: " + "; ".join(offenders)
    )


def test_every_module_is_importable():
    """A module that only fails at import time would break the JSON contract."""
    failures = []
    for info in pkgutil.walk_packages([str(PACKAGE_ROOT)], prefix="agentic_preflight."):
        try:
            __import__(info.name)
        except Exception as exc:  # noqa: BLE001 - reporting, not handling
            failures.append(f"{info.name}: {exc}")
    assert failures == []


def test_cli_declares_no_network_dependencies():
    """The declared dependency set is part of the promise, not just the code."""
    import tomllib

    pyproject = tomllib.loads((PACKAGE_ROOT.parent / "pyproject.toml").read_text(encoding="utf-8"))
    deps = pyproject["project"]["dependencies"]
    for dep in deps:
        name = dep.split(">")[0].split("=")[0].split("[")[0].strip().lower()
        assert name not in FORBIDDEN_IMPORTS, f"{name} is a network/LLM dependency"
