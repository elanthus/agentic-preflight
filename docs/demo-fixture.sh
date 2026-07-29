#!/usr/bin/env bash
#
# Builds the throwaway fixture that docs/demo.tape records against, then prints
# the command that renders docs/demo.gif.
#
# The recording is only worth anything if it can be reproduced, so this script
# exists to make `vhs docs/demo.tape` a one-command operation rather than a
# prose description of a repository someone has to assemble by hand.
#
# Everything lives under /tmp and is destroyed and rebuilt on each run. The
# paths are fixed rather than configurable because two of them are visible in
# the recording: the push error names the origin, so a different location would
# make the committed GIF disagree with the committed tape.
#
# Usage:  ./docs/demo-fixture.sh  &&  vhs docs/demo.tape
#
set -euo pipefail

ROOT=/tmp/ap-demo
ORIGIN=/tmp/ap-demo-origin.git
REPO="$ROOT/repo"
WORKSPACE="$ROOT/workspace"

for tool in git agentic-preflight uvx; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "error: $tool is not on PATH" >&2
        exit 1
    }
done

rm -rf "$ROOT" "$ORIGIN"
mkdir -p "$REPO" "$WORKSPACE"

# ---------------------------------------------------------------- the origin
# A local bare repo, so the push attempt in the recording is a real push that
# the pre-push hook can intercept, with no network and no remote account.
git init -q --bare "$ORIGIN"

# ------------------------------------------------------------------ the repo
cd "$REPO"
git init -q -b main
git config user.name "Demo"
git config user.email "demo@example.com"

cat > .gitignore <<'EOF'
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
.venv/
uv.lock
EOF

cat > pyproject.toml <<'EOF'
[project]
name = "calc-demo"
version = "0.1.0"
requires-python = ">=3.11"

[tool.ruff]
line-length = 100

[tool.pytest.ini_options]
pythonpath = ["."]
EOF

cat > .agentic-preflight.toml <<'EOF'
[general]
base_ref = "main"

[commands]
test = "uvx pytest -q"
lint = "uvx ruff check ."
EOF

cat > README.md <<'EOF'
# calc-demo

A tiny arithmetic CLI used to demonstrate the `agentic-preflight` workflow.

## Usage

```
python main.py add 2 3
python main.py subtract 5 3
python main.py divide 10 2
```

## Development

```
uvx pytest -q
uvx ruff check .
```
EOF

# main: no percentage() yet. The feature branch adds it, so the reviewed diff
# is the three files the recording shows as changed.
cat > calc.py <<'EOF'
"""Small arithmetic helpers used by the demo CLI."""


def add(a: float, b: float) -> float:
    return a + b


def subtract(a: float, b: float) -> float:
    return a - b


def divide(a: float, b: float) -> float:
    if b == 0:
        raise ValueError("cannot divide by zero")
    return a / b
EOF

cat > main.py <<'EOF'
"""Tiny CLI wrapper around calc.py, used only to give the demo repo a shape."""

import argparse

import calc


def main() -> None:
    parser = argparse.ArgumentParser(description="calc demo CLI")
    parser.add_argument("op", choices=["add", "subtract", "divide"])
    parser.add_argument("a", type=float)
    parser.add_argument("b", type=float)
    args = parser.parse_args()

    op = getattr(calc, args.op)
    print(op(args.a, args.b))


if __name__ == "__main__":
    main()
EOF

mkdir -p tests
cat > tests/test_calc.py <<'EOF'
import pytest

import calc


def test_add():
    assert calc.add(2, 3) == 5


def test_subtract():
    assert calc.subtract(5, 3) == 2


def test_divide():
    assert calc.divide(10, 2) == 5


def test_divide_by_zero_raises():
    with pytest.raises(ValueError):
        calc.divide(1, 0)
EOF

git add -A
git commit -q -m "Add the calc demo CLI"
git remote add origin "$ORIGIN"
git push -q origin main

# ------------------------------------------------------- the reviewed change
# percentage() lands WITHOUT the zero guard that divide() already has. That
# asymmetry is the finding the recording catches: it is a real defect in the
# diff rather than a strawman planted for the camera.
git switch -q -c feature/percentage

cat >> calc.py <<'EOF'


def percentage(part: float, whole: float) -> float:
    return part / whole * 100
EOF

# Not `sed -i`: the flag takes a mandatory argument on BSD sed and refuses one
# on GNU sed, and this repo supports both macOS and Linux.
python3 - <<'PYEOF'
import pathlib

p = pathlib.Path("main.py")
old = 'choices=["add", "subtract", "divide"]'
new = 'choices=["add", "subtract", "divide", "percentage"]'
text = p.read_text()
assert old in text, "main.py did not contain the expected choices list"
p.write_text(text.replace(old, new))
PYEOF

cat >> tests/test_calc.py <<'EOF'


def test_percentage():
    assert calc.percentage(50, 200) == 25.0
EOF

git add -A
git commit -q -m "Add a percentage helper"

# The hook has to exist before the recording opens, since the first shot is it
# refusing a push. init is run last so it cannot be swept into a commit.
agentic-preflight init >/dev/null

# ------------------------------------------------------------- the workspace
# Kept outside the repo: an untracked findings.json in the validation checkout
# would dirty the tree and stop the run.
cat > "$WORKSPACE/findings.json" <<'EOF'
{"findings": [{"path": "calc.py", "line": 19, "severity": "high", "action": "auto_fix", "title": "percentage() divides by zero with no guard", "detail": "percentage(part, whole) computes part / whole * 100 with no check on whole. Any caller passing whole=0 hits an unhandled ZeroDivisionError instead of a clear, controlled error like divide() already raises for the same case.", "suggestion": "if whole == 0:\n    raise ValueError(\"cannot divide by zero\")\nreturn part / whole * 100"}]}
EOF

echo '{"findings": []}' > "$WORKSPACE/findings-empty.json"

cat > "$WORKSPACE/fix_percentage.py" <<'PYEOF'
"""Applies the F001 fix: guard percentage() against a zero whole."""

calc = open("calc.py").read()
calc = calc.replace(
    "def percentage(part: float, whole: float) -> float:\n    return part / whole * 100",
    "def percentage(part: float, whole: float) -> float:\n"
    "    if whole == 0:\n"
    '        raise ValueError("cannot divide by zero")\n'
    "    return part / whole * 100",
)
open("calc.py", "w").write(calc)

tests = open("tests/test_calc.py").read()
tests = tests.rstrip("\n") + (
    "\n\n\n"
    "def test_percentage_of_zero_whole_raises():\n"
    "    with pytest.raises(ValueError):\n"
    "        calc.percentage(1, 0)\n"
)
open("tests/test_calc.py", "w").write(tests)
print("Patched calc.py and tests/test_calc.py")
PYEOF

echo "fixture ready"
echo "  repo:      $REPO  (on feature/percentage, one unpushed commit)"
echo "  origin:    $ORIGIN  (bare, main only)"
echo "  workspace: $WORKSPACE"
echo
echo "render with:  vhs docs/demo.tape"
