#!/usr/bin/env bash

set -euo pipefail

print_hook_instructions() {
    cat <<'EOF'

To remove the pre-push hook from each repository that used agentic-preflight:

  1. Enter the repository and resolve its actual hook path:

       cd /path/to/repository
       hook_path="$(git rev-parse --git-path hooks/pre-push)"

  2. Inspect the hook before changing it:

       sed -n '1,120p' "$hook_path"

  3. If it is the standalone generated hook (it says "Installed by
     agentic-preflight" and ends with "exec agentic-preflight hook-check"), remove it:

       rm -- "$hook_path"

     If it is a shared or custom hook, do not delete the file. Edit it and remove only
     the agentic-preflight hook-check invocation and its associated wrapper logic.

Repeat these steps for every clone where `agentic-preflight init` installed a hook.
The preserved .agentic-preflight.toml files, run history, and Git-note attestations may
be kept for audit history or removed separately after review.
EOF
}

if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv is required to uninstall agentic-preflight" >&2
    exit 1
fi

agents=("$@")
if [[ ${#agents[@]} -eq 0 ]]; then
    agents=(codex claude)
fi

tool_bin_dir="$(uv tool dir --bin)"
agentic_preflight_bin="$tool_bin_dir/agentic-preflight"
if [[ ! -x "$agentic_preflight_bin" ]]; then
    echo "agentic-preflight is not installed in $tool_bin_dir; no CLI or skills were changed."
    print_hook_instructions
    exit 0
fi

echo "Removing managed agent skills for: ${agents[*]}"
"$agentic_preflight_bin" integrations uninstall "${agents[@]}"

echo "Uninstalling the agentic-preflight CLI"
uv tool uninstall agentic-preflight

echo "agentic-preflight has been uninstalled."
echo "Repository configs, Git hooks, run history, and attestations were left intact."
print_hook_instructions
