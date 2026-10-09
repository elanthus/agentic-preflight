#!/usr/bin/env bash
set -euo pipefail

: "${UV_TOOL_DIR:?Set UV_TOOL_DIR to an isolated tool directory}"
: "${UV_TOOL_BIN_DIR:?Set UV_TOOL_BIN_DIR to an isolated tool bin directory}"
uv tool install --python 3.11 dist/*.whl
"$UV_TOOL_BIN_DIR/agentic-preflight" --version
test ! -e "$UV_TOOL_BIN_DIR/ap"
# Keep the checkout off sys.path so these assertions inspect the installed wheel.
"$UV_TOOL_DIR/agentic-preflight/bin/python" -P - <<'PYTHON'
import importlib.resources as resources

package = resources.files("agentic_preflight")
assert package.joinpath("py.typed").is_file(), "py.typed missing from wheel"
assert package.joinpath("_bundled_skill").is_dir(), "bundled skill missing from wheel"
print("wheel contents verified")
PYTHON
