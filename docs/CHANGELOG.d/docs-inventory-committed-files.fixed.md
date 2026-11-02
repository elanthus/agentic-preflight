- The documentation inventory lists only files committed at the head commit. Ignored
  and generated files, such as a Sphinx `docs/_build/` tree or a nested worktree, no
  longer enter `doc_surface`, the docs allowlist, or `doc_surface_sha256`.
