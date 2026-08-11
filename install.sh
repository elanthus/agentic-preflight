#!/usr/bin/env bash

set -euo pipefail

repo_root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv is required; install it from https://docs.astral.sh/uv/" >&2
    exit 1
fi

agents=("$@")
if [[ ${#agents[@]} -eq 0 ]]; then
    agents=(codex claude cursor opencode amp)
fi

echo "Installing agentic-preflight from $repo_root"
uv tool install --force --reinstall "$repo_root"

tool_bin_dir="$(uv tool dir --bin)"
agentic_preflight_bin="$tool_bin_dir/agentic-preflight"
if [[ ! -x "$agentic_preflight_bin" ]]; then
    echo "error: uv installed the tool, but $agentic_preflight_bin is not executable" >&2
    exit 1
fi

echo "Installing or updating agent skills for: ${agents[*]}"
"$agentic_preflight_bin" integrations install "${agents[@]}"

echo "agentic-preflight is installed and up to date."
if [[ ":$PATH:" != *":$tool_bin_dir:"* ]]; then
    echo "Add $tool_bin_dir to PATH, or run: uv tool update-shell"
fi
