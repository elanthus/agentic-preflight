#!/bin/sh
# Prepare this command for a maintainer to run only after an in-harness review.
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 REPOSITORY" >&2
    exit 2
fi

cd "$1"
comparison=$(agentic-preflight review compare)
printf '%s\n' "$comparison"
run_id=$(printf '%s\n' "$comparison" | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
git_common_dir=$(git rev-parse --git-common-dir)
case "$git_common_dir" in
    /*) ;;
    *) git_common_dir=$(cd "$git_common_dir" && pwd -P) ;;
esac
source_file="$git_common_dir/agentic-preflight/runs/$run_id/review-compare.json"
agreement_dir="$HOME/.local/share/agentic-preflight/agreement"
mkdir -p "$agreement_dir"
cp "$source_file" "$agreement_dir/$run_id.json"
