- The trusted `pull_request_target` attestation and human-approval workflows no longer run
  `uv tool install .`. They install only the runtime dependencies pinned in the protected
  base's `uv.lock` with `uv sync --locked --no-dev --no-install-project`, and run the
  checker from the base checkout with
  `uv run --locked --no-dev --no-sync python -m agentic_preflight`. Nothing is resolved
  from PyPI or built in these jobs, so a new or compromised release of a runtime
  dependency, the build backend, or a build dependency can no longer run inside them.
  (GHSA-vp48-82vp-gr6c)
- Those jobs pin the uv release in `astral-sh/setup-uv`. Maintainers bump it by hand.
- `pyproject.toml` pins the build backend to `hatchling==1.33.0`, because `uv.lock` does
  not record it.
- `install.sh`, `install.ps1`, and `uv tool install agentic-preflight` still resolve runtime
  dependencies fresh. The installation guide now says so and shows how to run the locked
  versions.
