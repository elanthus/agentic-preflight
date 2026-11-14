- The example reviewer wrappers in `docs/examples/reviewers/` (`claude_review.py`,
  `codex_review.py`, and `_reviewer_common.py`) are now deprecated pointers that import
  the packaged reviewer, and they are removed in 0.10.0. Replace a configured
  `python3 -B docs/examples/reviewers/<provider>_review.py` command with
  `agentic-preflight reviewer run --provider <provider>`. A wrapper that you copied from
  an earlier release keeps working but no longer receives fixes.
