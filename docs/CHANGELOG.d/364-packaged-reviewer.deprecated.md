- The example reviewer wrappers in `docs/examples/reviewers/` (`claude_review.py`,
  `codex_review.py`, and `_reviewer_common.py`) are deprecated in favour of
  `agentic-preflight reviewer run --provider claude|codex`, and are planned for removal in
  0.10.0. Until then they stay as standard-library copies of the packaged reviewer, which a
  test keeps identical, so a configured `python3 -S -B docs/examples/reviewers/<provider>_review.py`
  command keeps working. A wrapper that you copied from an earlier release keeps working
  but does not receive later fixes.
