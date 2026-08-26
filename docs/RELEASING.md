# Releasing

`agentic-preflight` publishes to PyPI from GitHub Actions using
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/). No API token is
stored in the repository or in GitHub secrets — the workflow authenticates with a
short-lived OIDC token minted per run.

## One-time setup

### 1. Register the publisher on PyPI

The project already uses this trusted publisher. If the project or publisher must be
recreated, register it at
<https://pypi.org/manage/project/agentic-preflight/settings/publishing/>:

| Field           | Value                |
| --------------- | -------------------- |
| PyPI project    | `agentic-preflight`  |
| Owner           | `elanthus`           |
| Repository      | `agentic-preflight`  |
| Workflow name   | `release.yml`        |
| Environment     | `pypi`               |

For a new project, register the same values as a pending publisher from the account
publishing page before the first upload.

### 2. Create the `pypi` environment in GitHub

In **Settings → Environments → New environment**, name it `pypi`, then add
yourself under **Required reviewers**.

This is the release gate: the `publish` job cannot start until a human approves
it, and until then nothing has been uploaded.

Optionally restrict the environment's deployment branches to tags matching `v*`.

## Cutting a release

1. Update `version` in `pyproject.toml`.
2. Update `CHANGELOG.md`, and re-pin the README's `blob/vX.Y.Z` documentation
   links to the new version. They deliberately point at released pages, so
   between releases the README on `main` can claim things — Windows support,
   for one — that the pages it links do not say yet; this bump is what closes
   that gap.
3. Commit and merge to `main`.
4. Run the full test matrix before tagging. Pull requests and pushes to `main` run
   only `ubuntu-latest` and `windows-latest` on Python 3.13. Scheduled
   Monday/Thursday regression covers macOS 15 with Python 3.11, but a manual run of
   the CI workflow is the pre-release check across all nine supported combinations:

   ```bash
   gh workflow run ci.yml --ref main
   ```

   The same thing is available from **Actions → CI → Run workflow**. The tag run in
   step 6 covers these combinations too, but finding a failure here means fixing it
   before a tag exists.

   Check that the run **completed**, not merely that it started. A cancelled run
   reports neither pass nor fail, so nine jobs appearing in the Actions tab is not
   the same as nine jobs passing. Manual runs are given their own concurrency group
   precisely so nothing supersedes them, but a run can still be cancelled by hand or
   time out.

5. Tag and push:

   ```bash
   git tag v0.4.0 && git push origin v0.4.0
   ```

6. The tag run starts two jobs in parallel. `test` exercises the full matrix of
   Ubuntu, macOS, and Windows against Python 3.11, 3.12, and 3.13. `build` verifies the tag
   matches `pyproject.toml`, builds the sdist and wheel, and smoke-tests the wheel.
   `publish` requires both, so it stays pending until the matrix and the build are
   green. The build also produces a CycloneDX SBOM and GitHub build-provenance and SBOM
   attestations for the sdist and wheel before uploading the release artifact.
7. Approve the pending `publish` job in the Actions run. Upload happens after
   approval.

## Notes

- **Version numbers are permanent.** PyPI refuses re-uploads of a filename that
  has already existed, even after deletion. A bad release can only be *yanked*
  (hidden from resolvers), never replaced. The tag/version consistency check in
  the `build` job exists to catch the common "tagged the wrong version" mistake
  before anything is uploaded.
- **A matrix failure after tagging does not consume a version number.** `publish`
  needs `test`, so a macOS or Python 3.11/3.12 failure stops the run before anything
  reaches PyPI, and PyPI never sees the version. Recovery is to delete the tag
  locally and on the remote, fix the problem, and re-tag the *same* version. The cost
  is a deleted tag and a delayed release, not a permanent burn. Step 4 exists to make
  even that uncommon.
- **The sdist uses an explicit allowlist** (`[tool.hatch.build.targets.sdist]`).
  Hatchling would otherwise include everything not covered by `.gitignore`, which
  makes the published artifact depend on whatever happens to be in the working
  tree. Add new top-level files there if they belong in the sdist.
- **Release attestations are verifiable.** Download a distribution from the workflow
  artifact or PyPI and run
  `gh attestation verify <file> -R elanthus/agentic-preflight`. Add the CycloneDX
  predicate type when verifying the SBOM attestation. The separately downloadable SBOM
  is included in the workflow artifact under `sbom/`.
- **Testing the flow end to end** against TestPyPI requires a second pending
  publisher on <https://test.pypi.org> and a `repository-url` input on the
  publish step. Because the environment gate already prevents an accidental
  upload, this is usually not worth maintaining.
