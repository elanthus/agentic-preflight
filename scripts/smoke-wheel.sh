#!/usr/bin/env bash
set -euo pipefail

: "${UV_TOOL_DIR:?Set UV_TOOL_DIR to an isolated tool directory}"
: "${UV_TOOL_BIN_DIR:?Set UV_TOOL_BIN_DIR to an isolated tool bin directory}"
# The minimum and maximum supported Python versions (COMPATIBILITY.md).
# tests/test_python_support.py pins this list to the pyproject.toml classifiers.
for python in 3.11 3.14; do
  echo "Smoke testing the built wheel on Python $python"
  uv tool install --python "$python" dist/*.whl
  "$UV_TOOL_BIN_DIR/agentic-preflight" --version
  test ! -e "$UV_TOOL_BIN_DIR/ap"
  # Keep the checkout off sys.path so these assertions inspect the installed wheel.
  "$UV_TOOL_DIR/agentic-preflight/bin/python" -P - "$python" <<'PYTHON'
import importlib.resources as resources
import sys

expected = sys.argv[1]
actual = f"{sys.version_info.major}.{sys.version_info.minor}"
assert actual == expected, f"installed on Python {actual}, expected {expected}"
package = resources.files("agentic_preflight")
assert package.joinpath("py.typed").is_file(), "py.typed missing from wheel"
assert package.joinpath("_bundled_skill").is_dir(), "bundled skill missing from wheel"
print("wheel contents verified")
PYTHON
  uv tool uninstall agentic-preflight
done
