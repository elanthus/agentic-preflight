# Releasing

`agentic-preflight` publishes to PyPI from GitHub Actions with [Trusted Publishing](https://docs.pypi.org/trusted-publishers/).
No API token is stored in the repository or in GitHub secrets.
The workflow authenticates with a short-lived OIDC token that is minted for each run.

## One-time setup

### 1. Register the publisher on PyPI

The project already uses this trusted publisher.
If the project or the publisher must be recreated, register it at
<https://pypi.org/manage/project/agentic-preflight/settings/publishing/>:

| Field           | Value                |
| --------------- | -------------------- |
| PyPI project    | `agentic-preflight`  |
| Owner           | `elanthus`           |
| Repository      | `agentic-preflight`  |
| Workflow name   | `release.yml`        |
| Environment     | `pypi`               |

For a new project, register the same values as a pending publisher from the account publishing page before the first upload.

### 2. Create the `pypi` environment in GitHub

In **Settings → Environments → New environment**, name the environment `pypi`.
Then add yourself under **Required reviewers**.

With required reviewers configured, this is the release approval step.
The `publish` job waits for Environment approval. Until then, nothing is uploaded.
On 2026-10-13, a read-only inspection confirmed `elanthus` as the required reviewer, `v*` tag deployment restrictions, permitted self-review, and enabled administrator bypass.
See the [settings snapshot](attestations-and-ci.md#administrator-setup-and-this-repositorys-settings).
Workflow YAML alone does not configure required reviewers or remove bypass access.

Optionally, restrict the environment's deployment branches to tags that match `v*`.

## Cutting a release

1. Select an unused three-part SemVer version (`MAJOR.MINOR.PATCH`).
   Update `version` in `pyproject.toml`.
   A hotfix is a patch bump. Never add a fourth version component.
   Run `uv lock` to keep the local project version in `uv.lock` in sync.
2. Assemble the changelog with the selected version and release date:

   ```bash
   uv run python tools/changelog.py X.Y.Z --date YYYY-MM-DD
   ```

   The command combines the existing Unreleased entries with `docs/CHANGELOG.d/*.md` under the new release.
   It orders them by category, then by filename.
   It validates every fragment before it changes files.
   It replaces the changelog atomically and removes the consumed fragments.
   It rejects a repeated version.
   Inspect `CHANGELOG.md` and the fragment deletions. Commit them together.
   If the removal is interrupted, restore the pre-assembly tree before you retry. Do not reuse a partially consumed set.
   Historical release text is preserved.

   Then re-pin the README's `blob/vX.Y.Z` documentation links to the new version.
   Check each target against the release tree.
   The new tag links become available when the tag is pushed.
   This re-pin also covers each `blob/main` link that was introduced between releases.

   Optionally, re-record the README animation when the demonstrated workflow changes:

   ```bash
   uv sync --group dev
   PATH="$PWD/.venv/bin:$PATH" ./docs/demo-fixture.sh
   PATH="$PWD/.venv/bin:$PATH" vhs docs/demo.tape
   ```

   This requires VHS, its recording dependencies, `zsh`, `jq`, and `uvx`.
   The fixture warms the `uvx` caches for `pytest` and `ruff` first.
   A cold cache makes the recording install packages on camera, and the lint stage can then outlast its sleep in the tape.
   Watch the GIF through the final `AWAITING_PUSH_CONFIRM` frame and check the demo run's status.
   A successful renderer exit alone does not prove that the recorded commands passed.
   The current README animation is pinned to an existing commit in `main` history.
   Thus, the removal of the tracked GIF does not depend on the `badges` branch.
   The generated `docs/demo.gif` is ignored. It is not committed to `main`.
   When the coverage publisher that preserves other assets is on `main`, publish the GIF as `demo.gif` on the existing `badges` branch.
   Use a normal commit that preserves `coverage.svg` and the other assets.
   Then update the README to the commit-pinned raw URL.
   Never recreate or force-push the `badges` branch to update the animation.
   The coverage publisher updates only `coverage.svg` and preserves the animation.
3. Complete the four [repository checks](../AGENTS.md#checks).
   Build both distributions.
   Run `scripts/smoke-wheel.sh` with an isolated `UV_TOOL_DIR` and `UV_TOOL_BIN_DIR`.
   Inspect the wheel's bundled skill and the sdist contents.
   Test an upgrade from the current published release, including the configuration, saved runs and notes, and the managed skill refresh.
   Review [compatibility](../COMPATIBILITY.md) and the version's migration guide.
   Complete preflight and open a ready-for-review preparation PR.
   A manual CI dispatch with `--ref <preparation-branch>` can validate the nine supported combinations on the proposed commit before merge.
   Verify its `head_sha` and the conclusion of every job.
   After review, approval where required, and green checks, merge to `main`.
4. Run the full [compatibility matrix](../COMPATIBILITY.md#validation-tiers) before you tag.
   Dispatch the CI workflow:

   ```bash
   gh workflow run ci.yml --ref main
   ```

   The same action is available from **Actions → CI → Run workflow**.
   The tag run in step 6 also covers these combinations.
   A failure found here is fixed before a tag exists.

   Check that the run **completed**, not only that it started.
   A cancelled run reports neither pass nor fail.
   Nine jobs in the Actions tab are not the same as nine passed jobs.
   Manual runs have their own concurrency group, so nothing supersedes them.
   A run can still be cancelled by hand or time out.
5. Tag and push:

   ```bash
   release_version="$(uv run python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
   git tag "v$release_version"
   git push origin "v$release_version"
   ```

   The pre-push hook does not require an attestation for a tag push.
   Thus, this works when the tagged commit is a merge commit that GitHub created.
6. The tag run starts two jobs in parallel.
   `test` exercises the full compatibility matrix.
   `build` verifies that the tag matches `pyproject.toml`, builds the sdist and the wheel, and smoke-tests the wheel.
   `publish` requires both. It stays pending until the matrix and the build are green.
   The build also produces a CycloneDX SBOM and GitHub build-provenance and SBOM attestations for the sdist and the wheel.
   Then it uploads the release artifact.
7. Approve the pending `publish` job in the Actions run.
   The upload occurs after approval.

## Notes

- **Version numbers are permanent.**
  PyPI refuses to accept a filename that has already existed, even after deletion.
  A bad release can only be *yanked*, which hides it from resolvers. It can never be replaced.
  The tag and version consistency check in the `build` job catches the common "tagged the wrong version" mistake before an upload.
- **A matrix failure after the tag does not consume a version number.**
  `publish` needs `test`.
  A macOS or Python 3.11 or 3.12 failure stops the run before anything reaches PyPI. PyPI never sees the version.
  To recover, delete the tag locally and on the remote, fix the problem, and tag the *same* version again.
  The cost is a deleted tag and a delayed release, not a permanently used version.
  Step 4 makes even that uncommon.
- **The sdist uses an explicit allowlist** in `[tool.hatch.build.targets.sdist]`.
  Without it, Hatchling would include everything that `.gitignore` does not cover.
  The published artifact would then depend on the content of the working tree.
  Add new top-level files there if they belong in the sdist.
- **Release attestations are verifiable.**
  Download a distribution from the workflow artifact or from PyPI.
  Run `gh attestation verify <file> -R elanthus/agentic-preflight`.
  Add the CycloneDX predicate type when you verify the SBOM attestation.
  The separately downloadable SBOM is in the workflow artifact under `sbom/`.
- **An end-to-end test against TestPyPI** requires a second pending publisher on <https://test.pypi.org> and a `repository-url` input on the publish step.
  The environment approval already prevents an accidental upload, so this is usually not worth the maintenance.
