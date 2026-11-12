- The trusted `pull_request_target` attestation and human-approval workflows install the
  verifier with `uv sync --locked --no-dev` and run it with `uv run --locked --no-dev`
  instead of `uv tool install .`. Its dependencies now come from the protected base's
  `uv.lock`, so a new or compromised release of `click`, `pydantic`, or their dependencies
  no longer runs inside the policy jobs before the lock admits it. (GHSA-vp48-82vp-gr6c)
- `pyproject.toml` pins the build backend to `hatchling==1.33.0`, because `uv.lock` does
  not record it.
- `install.sh`, `install.ps1`, and `uv tool install agentic-preflight` still resolve runtime
  dependencies fresh. The installation guide now says so and shows how to run the locked
  versions.
