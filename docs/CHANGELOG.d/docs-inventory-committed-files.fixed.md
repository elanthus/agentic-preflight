- The documentation inventory lists only files committed at the head commit. Files not
  committed there, such as an ignored Sphinx `docs/_build/` tree, an uncommitted
  generated file, or a nested worktree, no longer enter `doc_surface`, the docs
  allowlist, or `doc_surface_sha256`. A committed generated file still counts.
