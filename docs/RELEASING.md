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

## Protected-verifier v1 cutover

Use this runbook when a pull request changes an attestation or configuration format that
the verifier installed from the pull request's protected base cannot read. This is a
maintainer-controlled bootstrap, not a compatibility mechanism. It does not authorize a
merge, ruleset bypass, protection change, release, or publication; obtain explicit user
authorization for the exact action when execution reaches that point.

The two relevant workflows deliberately trust only the protected event base:

- `CI` / `trusted preflight attestation` checks out
  `github.event.pull_request.base.sha`, installs that checkout, and verifies the proposed
  head's attestation.
- `Human approval policy` / `evaluate high-risk approval policy` does the same under
  `pull_request_target`; `high-risk human approval` enforces the resulting policy state.
  The proposed checkout remains inert Git data in both workflows.

The current workflow assertions in `tests/test_governance.py` cover the protected-base
checkout and install, the pinned base/head arguments, and use of the shared hosted
checker. Do not weaken that boundary, execute the proposed checkout, add
`continue-on-error`, or rename a check to evade a rule during a cutover.

### Planned v1 order

Recheck the final diff of every PR before applying this table. A later patch can move a
change across the boundary even when its issue description did not.

| Issue | Change and dependency | Landing decision |
| --- | --- | --- |
| [#123](https://github.com/elanthus/agentic-preflight/issues/123) | Remove Python compatibility exports. | Normal PR; independent. |
| [#124](https://github.com/elanthus/agentic-preflight/issues/124) | Remove the plain-directory hook fallback. | Normal PR; independent. |
| [#125](https://github.com/elanthus/agentic-preflight/issues/125) | Rename the accepted TOML key to `automated_cleanup`. The serialized field name and digest do not change, and the current base accepts that spelling. | Normal PR; independent. |
| [#126](https://github.com/elanthus/agentic-preflight/issues/126) | Remove defaulted `[review] require_fix_commits` and `[worktree] ttl_hours`. New raw snapshots omit them; the current verifier supplies its defaults while validating them. | Normal PR; independent, but confirm the final fingerprint and digest tests before merge. |
| [#127](https://github.com/elanthus/agentic-preflight/issues/127) | Require the object-shaped docs submission. | Normal PR; independent. |
| [#128](https://github.com/elanthus/agentic-preflight/issues/128) | Remove an eval-report field and bump its method version. | Normal PR; independent. |
| [#129](https://github.com/elanthus/agentic-preflight/issues/129) | Raise the Git minimum and remove a local merge fallback. | Normal PR; independent. |
| [#130](https://github.com/elanthus/agentic-preflight/issues/130) | Reject old run records with schema 1. | Normal PR; land before #131 and #132. |
| [#131](https://github.com/elanthus/agentic-preflight/issues/131) | Remove the old active-run pointer migration. | Normal PR after #130. |
| [#132](https://github.com/elanthus/agentic-preflight/issues/132) | Require creation-time run fields. | Normal PR after #130. |
| [#133](https://github.com/elanthus/agentic-preflight/issues/133) | Remove consumer negotiation, `[reuse] attestation_schema`, and `[ci] consumer_schema`. | Normal PR, but run tests locally and emit schema 5. The new producer cannot use the old base's removed CI declarations for delegation; do not interpret that expected fallback to local tests as a hosted failure. Land before #135 and #134. |
| [#135](https://github.com/elanthus/agentic-preflight/issues/135) | Always serialize the complete config. It depends on #133 and overlaps `attestation.py` with #134. This repository's non-default `[ci]` section is already serialized. | Normal PR after #133 and before #134. Recheck actual snapshots and digests; stop if the final patch instead produces a diagnostic from the old verifier. |
| [#134](https://github.com/elanthus/agentic-preflight/issues/134) | Require attestation schema 7 with an explicit outcome. The #133/#135 base accepts only schemas 4, 5, and 6. | **The one planned bootstrap PR.** Its schema-7 note must fail both old protected-base consumers with `reason: incompatible_schema`. |
| [#136](https://github.com/elanthus/agentic-preflight/issues/136) | Sweep transition-era documentation after all code PRs. | Normal PR after #123-#135. Preserve this operational runbook. |
| [#137](https://github.com/elanthus/agentic-preflight/issues/137) | Cut v0.6.0. | Last: requires #136 and the recorded post-cutover rehearsal below. |

This order puts #135 before #134 so the only incompatible protected-base transition is
the intentional schema-7 change. Do not combine unrelated cleanup PRs, add a dual reader,
emit an older production format, or weaken production validation to avoid that transition.

### 1. Identify and record the boundary

Open the candidate PR, then capture immutable identities rather than branch names. Run
these commands from a clean checkout of this repository and save their output in the
cutover record or its tracking issue:

```bash
repo=elanthus/agentic-preflight
pr=<candidate-pr-number>
base_sha="$(gh api "repos/$repo/pulls/$pr" --jq .base.sha)"
head_sha="$(gh api "repos/$repo/pulls/$pr" --jq .head.sha)"
head_ref="$(gh api "repos/$repo/pulls/$pr" --jq .head.ref)"
head_repo="$(gh api "repos/$repo/pulls/$pr" --jq .head.repo.full_name)"
head_repo_url="$(gh api "repos/$repo/pulls/$pr" --jq .head.repo.clone_url)"
printf 'base_sha=%s\nhead_sha=%s\nhead_ref=%s\nhead_repo=%s\n' \
  "$base_sha" "$head_sha" "$head_ref" "$head_repo"
git show "$base_sha:pyproject.toml" | grep '^version = '
git show "$head_sha:pyproject.toml" | grep '^version = '
```

Record the head as the producer revision and the base as the installed verifier revision.
Also record how the local producer was installed, `command -v agentic-preflight`, and
`agentic-preflight --version`; a package version alone is not a revision identity.

Inspect live rules instead of inferring required checks from workflow YAML:

```bash
gh api "repos/$repo/rulesets" \
  --jq '.[] | {id, name, enforcement, current_user_can_bypass}'
gh api "repos/$repo/rulesets/<active-ruleset-id>"
gh api "repos/$repo/branches/main/protection"
gh api "repos/$repo/commits/$head_sha/check-runs" \
  --jq '.check_runs[] | {name, status, conclusion, html_url}'
```

As inspected on 2026-09-13 at `97269221bf0735d3780c93f2e9f2404251577ebb`,
the active `PR first` ruleset requires a pull request, configures code-owner review with
zero required approvals, names no required status checks, and grants `elanthus` an
always-available ruleset bypass. The legacy branch-protection endpoint reports that
`main` is not protected. Treat this only as planning evidence: save the full live results
again immediately before the cutover. Record the exact check-run names and links returned
by the API, including the hosted
attestation check and both approval-policy jobs; GitHub's displayed names are the
authority, not the labels assumed above.

For every candidate, record the affected config keys, serialized snapshot shape,
`config_sha256`, attestation `schema_version` and `outcome`, plus the expected and actual
diagnostic. For #134 the acceptable old-consumer evidence is a present note rejected on
the first availability attempt with `data.reason` and that attempt's `reason` both equal
to `incompatible_schema`. Missing-note retries, `invalid_inputs`, a
generic red check, a failed test, or an approval-policy rejection do not prove the
format boundary.

### 2. Validate the candidate independently

Run the normal Agentic Preflight process against the recorded base and finish every local
stage. For #133, explicitly keep test execution local. For #134, use the reviewed
schema-7 producer and publish the note for the exact recorded head. Then run the complete
repository and package checks from the candidate checkout:

```bash
uv sync
uv run ruff check .
uv run ruff format --check agentic_preflight tests
uv run mypy agentic_preflight
uv run pytest
uv build
```

Record every exit code, the final pytest summary, the built filenames, and the ordinary
PR check links. Have a maintainer independently review `attestation_schema.py`,
`models.py`, `attestation.py`, configuration serialization and digest code, both hosted
workflows, and their governance tests. Ordinary tests and `workflow_dispatch` do not
exercise the pull-request-only attestation job.

Reproduce the expected failure with the base-installed verifier. Use separate temporary
tool directories so the command cannot silently resolve the candidate installation:

```bash
old_tool_root="$(mktemp -d)"
old_checkout="$(mktemp -d)"
git worktree add --detach "$old_checkout" "$base_sha"
UV_TOOL_DIR="$old_tool_root/tools" UV_TOOL_BIN_DIR="$old_tool_root/bin" \
  uv tool install --python 3.11 "git+https://github.com/$repo.git@$base_sha"
source_remote=origin
if [ "$head_repo" != "$repo" ]; then
  git -C "$old_checkout" remote add preflight-contributor "$head_repo_url"
  source_remote=preflight-contributor
fi
(cd "$old_checkout" && PATH="$old_tool_root/bin:$PATH" \
  agentic-preflight hosted-check "$head_sha" --base "$base_sha" \
  --source-remote "$source_remote" --head-ref "refs/heads/$head_ref")
```

Save the complete JSON envelope, exit code, verifier version/revision, and the hosted
workflow logs that show the same `incompatible_schema` result. Stop if either verifier
reports a different reason: investigate the candidate rather than broadening the
exception.

### 3. Obtain and execute the narrow bootstrap authorization

Present the record to the user and request explicit authorization to merge only the
recorded PR at `head_sha`, despite only these expected failures:

- the exact hosted attestation check name, failing with `incompatible_schema`; and
- the exact approval-policy check names whose protected-base decoder reports the same
  incompatibility or whose final job fails solely because that decoder job failed.

All other validation and applicable policy requirements must be green. Any new commit
invalidates the authorization and restarts this procedure. Under the rules observed
above, the authorized maintainer can use the normal manual PR merge; no protection
change or ruleset bypass is needed merely because the non-required format checks are
red. If live rules make a bypass necessary, record the exact blocking rule and obtain
authorization for that bypass separately. If no permitted exception exists, report the
blocker and leave the PR and this cutover open. Never disable a rule or check, fabricate
a status, or merge through a different branch.

### 4. Install the new trusted baseline

After GitHub reports the authorized merge, capture the new `main` SHA and confirm it
contains the authorized head before installing anything:

```bash
git fetch origin main
new_base_sha="$(git rev-parse origin/main)"
git merge-base --is-ancestor "$head_sha" "$new_base_sha"
uv tool install --force --reinstall \
  "git+https://github.com/$repo.git@$new_base_sha"
agentic-preflight --version
```

The generated hook executes the installed `agentic-preflight` command and contains no
embedded package revision. Inspect its effective location and content; if the managed
hook is absent, reinstall it from the new CLI. `init` leaves an existing repository
configuration untouched and refuses to replace a foreign hook unless `--force` is used:

```bash
hook_path="$(git rev-parse --git-path hooks/pre-push)"
grep -F 'agentic-preflight hook-check' "$hook_path" || agentic-preflight init
agentic-preflight integrations status
```

From the status output, name the integrations and custom `--target` directories that
are actually installed, then refresh only those managed, unmodified copies, for example:

```bash
agentic-preflight integrations update codex claude
agentic-preflight integrations update --target /absolute/installed/skills-directory
```

Do not use `--force` on a `modified` or `unmanaged` integration. Preserve it, record the
conflict, and reconcile its customizations separately. Record `new_base_sha`, the exact
install command, CLI version, hook path/result, integration targets, and status output.
The GitHub workflows need no mutable installation step: the next pull-request event will
install its checker directly from this new protected base.

### 5. Prove normal enforcement resumed

The cutover is incomplete until a fresh pull request is based on `new_base_sha`, carries
evidence in the new format, and exercises both pull-request workflows. With explicit
authorization to create and close the rehearsal PR, create a rehearsal-only change that
does not need to merge. Agentic Preflight rejects an empty diff, so add a clearly named
temporary file, run the normal preflight with the refreshed producer, publish its
attestation, and open the PR against `main`:

```bash
git switch --create rehearsal/protected-verifier-v1 "$new_base_sha"
touch docs/protected-verifier-v1-rehearsal.md
git add docs/protected-verifier-v1-rehearsal.md
git commit --message 'Rehearse protected verifier v1'
# Complete the normal Agentic Preflight flow and publish this exact head's note. If
# [pr] mode is auto, use the PR it opens and do not run the manual-mode command below.
gh pr create --repo "$repo" --base main \
  --head rehearsal/protected-verifier-v1 \
  --title 'Rehearse protected verifier v1' \
  --body 'Post-cutover proof only; close without merging.'
```

Record the rehearsal PR number, base/head SHAs, schema/outcome, and all workflow and
check links. Confirm from each run's checkout and install steps that both
`trusted preflight attestation` and `evaluate high-risk approval policy` used
`new_base_sha`, not the proposed checkout. The attestation check must succeed. The
approval evaluation and `high-risk human approval` must complete with the result required
by the protected base's current policy; record whether approval was required and why.
Close the rehearsal PR without merging, using `gh pr close <rehearsal-pr-number>`, and
retain its links as audit evidence.

### 6. Close the cutover

Add the final record to the tracking issue with:

- old base/installed-verifier SHA, authorized candidate head, producer revision, and new
  protected-base SHA;
- config snapshot and digest changes, attestation schema/outcome, exact exception reason,
  approver, authorization link, merge link, and time;
- local command exit codes and pytest summary, independent reviewer, package results, and
  pre-cutover check/log links; and
- rehearsal PR base/head, new-format evidence identity, exact workflow/check links,
  policy result, and closure link.

Keep the cutover issue open while the merge or rehearsal is pending or any recorded check
is inconclusive. Do not complete #137, create a release tag, or publish a package before
the post-cutover proof is complete. Coordinate #136 so it removes obsolete transition
guidance without deleting this still-current operational procedure.

## Cutting a release

1. Choose an unused version and update `version` in `pyproject.toml`. Run `uv lock`
   to keep the local project version in `uv.lock` in sync.
2. Update `CHANGELOG.md`, and re-pin the README's `blob/vX.Y.Z` documentation
   links to the new version. Check each target against the release tree; the
   new tag links will become available when the tag is pushed. Re-record the README
   animation against the release checkout:

   ```bash
   uv sync --group dev
   PATH="$PWD/.venv/bin:$PATH" ./docs/demo-fixture.sh
   PATH="$PWD/.venv/bin:$PATH" vhs docs/demo.tape
   ```

   This requires VHS, its recording dependencies, `zsh`, and `jq`. Watch the GIF
   through the final `AWAITING_PUSH_CONFIRM` frame and check the demo run's status;
   a successful renderer exit alone does not prove the recorded commands passed.
3. Commit and merge to `main`.
4. Run the full test matrix before tagging. Pull requests and pushes to `main` run
   only `ubuntu-latest` on Python 3.13. Scheduled
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
   release_version="$(uv run python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
   git tag "v$release_version"
   git push origin "v$release_version"
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
