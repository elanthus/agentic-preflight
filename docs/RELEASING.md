# Releasing

`agentic-preflight` publishes to PyPI from GitHub Actions using
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/). No API token is
stored in the repository or in GitHub secrets — the workflow authenticates with a
short-lived OIDC token minted per run.

## One-time setup

### 1. Register the publisher on PyPI

The project does not exist on PyPI yet, so register a **pending** publisher at
<https://pypi.org/manage/account/publishing/>:

| Field           | Value                |
| --------------- | -------------------- |
| PyPI project    | `agentic-preflight`  |
| Owner           | `elanthus`           |
| Repository      | `agentic-preflight`  |
| Workflow name   | `release.yml`        |
| Environment     | `pypi`               |

The pending publisher is converted into a real one automatically on the first
successful upload.

### 2. Create the `pypi` environment in GitHub

In **Settings → Environments → New environment**, name it `pypi`, then add
yourself under **Required reviewers**.

This is the release gate: the `publish` job cannot start until a human approves
it, and until then nothing has been uploaded.

Optionally restrict the environment's deployment branches to tags matching `v*`.

## Cutting a release

1. Update `version` in `pyproject.toml`.
2. Update `CHANGELOG.md`.
3. Commit and merge to `main`.
4. Tag and push:

   ```bash
   git tag v0.3.0 && git push origin v0.3.0
   ```

5. The `build` job verifies the tag matches `pyproject.toml`, builds the sdist and
   wheel, and smoke-tests the wheel.
6. Approve the pending `publish` job in the Actions run. Upload happens after
   approval.

## Notes

- **Version numbers are permanent.** PyPI refuses re-uploads of a filename that
  has already existed, even after deletion. A bad release can only be *yanked*
  (hidden from resolvers), never replaced. The tag/version consistency check in
  the `build` job exists to catch the common "tagged the wrong version" mistake
  before anything is uploaded.
- **The sdist uses an explicit allowlist** (`[tool.hatch.build.targets.sdist]`).
  Hatchling would otherwise include everything not covered by `.gitignore`, which
  makes the published artifact depend on whatever happens to be in the working
  tree. Add new top-level files there if they belong in the sdist.
- **Testing the flow end to end** against TestPyPI requires a second pending
  publisher on <https://test.pypi.org> and a `repository-url` input on the
  publish step. Because the environment gate already prevents an accidental
  upload, this is usually not worth maintaining.
