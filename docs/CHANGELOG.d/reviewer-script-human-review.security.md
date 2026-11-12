- A branch can no longer replace the reviewer that reviews it without human approval.
  `[review] command` and `[review.contracts] planner_command` run from the worktree under
  review, and verification compared only the command string, so a branch could rewrite a
  repository reviewer script to report nothing and pass with no owner review. Risk
  classification now treats every repository path these commands name, and every file
  under that path's directory, as a human-review path. `approval-check` and attestation
  verification read the commands from the protected base. (GHSA-3372-q2xp-jr7q)
- New `[review] trusted_paths` lists further reviewer files, such as prompts or modules
  loaded by name, whose changes need human review.
- This repository adds `docs/examples/reviewers/**` to `[policy] human_review_paths` and
  CODEOWNERS.
- `docs/limits.md` and `docs/independent-review.md` now say that verification compares
  command strings, not the scripts that they run.
- Reviewer path detection also protects directories that a command changes to (`cd`,
  `pushd`, `-C`, `--directory`, `--cwd`, `--prefix`, `--project`, `--chdir`), directories
  in `PYTHONPATH` and `NODE_PATH`, the top-level package of a `-m` module, and the literal
  path after a leading `$VAR/` or `$(...)/` expansion.
- The example Claude wrapper passes `--safe-mode --setting-sources user`, and the example
  Codex wrapper passes `--config project_doc_max_bytes=0 --ignore-rules`, so the branch
  under review cannot instruct or reconfigure its reviewer through `CLAUDE.md`,
  `AGENTS.md`, or project settings. The example configurations run the wrapper with
  `python3 -S -B`, so a branch-added `sitecustomize` module does not run.
- This repository adds a root `sitecustomize.py` to `human_review_paths` and CODEOWNERS,
  because its reviewer command starts with `uv run`.
