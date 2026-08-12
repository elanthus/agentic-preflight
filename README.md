# agentic-preflight

[![CI](https://github.com/elanthus/agentic-preflight/actions/workflows/ci.yml/badge.svg)](https://github.com/elanthus/agentic-preflight/actions/workflows/ci.yml)
[![Coverage](/../badges/coverage.svg)](/../badges/coverage.svg)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

**Stops your coding agent from pushing unverified work.**

Agentic Preflight records a review, test, documentation, and lint result against a
commit, and a pre-push hook refuses a commit with no applicable green run. A
history-only rebase keeps green only when the complete tree, effective preflight
configuration, and Git's clean merge result against the freshly fetched base are
unchanged; content-changing rewrites and config changes still require a new run.

![A push blocked by the pre-push hook, a review that catches an unguarded division by
zero, the fix verified, and the gate stopping to ask before it pushes](docs/demo.gif)

Every frame above is real CLI output, recorded with [VHS](https://github.com/charmbracelet/vhs).
Regenerate it yourself with `./docs/demo-fixture.sh && vhs docs/demo.tape`: the script
builds a throwaway repo with a genuine unguarded division in it, and the tape drives the
run. The judgment between the commands is the agent's; the commands are all this package
does.

Three things separate it from a checklist in a prompt:

- **Skipping a stage is structurally unrepresentable.** Not discouraged by
  documentation — no transition exists from a review state to a push state at all. That
  property is proved by enumerating every path through the machine, not by testing a
  few of them.
- **Your agent judges; this keeps the record.** No API key and no second model, and no
  opinion of its own about what good code looks like. It drives the agent you already
  have.
- **The record includes its own gaps.** A bypassed hook, a stage that could not run, a
  SHA with no green run — each stays visible in `status` and the attestation. A record
  that can only report success is marketing.

Agentic Preflight is a deterministic state machine with a JSON-over-stdout CLI. It runs
on macOS and Linux.

## Quickstart

From a repository with Python 3.11+ and Git 2.30+:

```bash
uv tool install agentic-preflight
agentic-preflight integrations install codex claude cursor opencode amp
cd your-repo
agentic-preflight init
```

When working from this source checkout, `./install.sh` installs or updates the CLI and
all five supported agent integrations in one step. Pass integration names to choose
only the coding agents you use.
Run `./uninstall.sh` to remove the managed skills and CLI. It pauses first so you can
enter `agentic-preflight:uninstall` in every initialized repository; that trigger
removes the repository configuration and managed hook logic while preserving unrelated
hooks, run history, and attestations.

`init` writes `.agentic-preflight.toml` and installs an advisory pre-push hook. Make and
commit a change, then try to push it before validation:

```console
$ git push
agentic-preflight: push blocked.
  commit: 4f15c2a (no green run recorded for this exact SHA)
  reason: no green run recorded for this exact SHA
  fix:    invoke the skill (/agentic-preflight in Claude Code, $agentic-preflight in Codex)
  bypass: git push --no-verify   (documented escape hatch)
error: failed to push some refs
```

Ask your coding agent to invoke `$agentic-preflight` in Codex or `/agentic-preflight` in
Claude Code. A run follows the command in each JSON envelope. This output was captured
from a local demo repository; `jq` limits each envelope to the fields relevant here:

```console
$ agentic-preflight start --intent "Add retries and document the failure policy" | jq -c '{ok,state,next}'
{"ok":true,"state":"REVIEW_AWAITING_FINDINGS","next":{"command":"agentic-preflight context","instruction":"Fetch the diff before judging it."}}
$ agentic-preflight context | jq -c '{ok,state,data:{changed_files:.data.changed_files,review_coverage:.data.review_coverage|{manifest,total_units}},next}'
{"ok":true,"state":"REVIEW_AWAITING_FINDINGS","data":{"changed_files":[".agentic-preflight.toml","change.txt"],"review_coverage":{"manifest":"…","total_units":2}},"next":{"command":"agentic-preflight submit-findings --file findings.json","instruction":"Review every delivered unit, then submit snapshot-bound coverage and findings."}}
... review, docs, lint, and tests complete ...
$ agentic-preflight gate | jq -c '{ok,state,data:{token:.data.token,remote:.data.remote,branch:.data.branch,pr_mode:.data.pr_mode,approval_mode:.data.approval_mode},next}'
{"ok":true,"state":"AWAITING_PUSH_CONFIRM","data":{"token":"d8697c2068b4853b","remote":"origin","branch":"demo","pr_mode":"auto","approval_mode":"manual_merge"},"next":{"command":"agentic-preflight push --confirm d8697c2068b4853b","instruction":"Show the user the remote, branch, and commit list in plain language, then ask whether to push. Never push without asking. This high-risk change requires the user to merge the pull request manually; do not merge it or enable auto-merge. After the confirmed push and preflight finish, automatically open or reuse the pull request; auto mode is standing authorization, so do not ask again."}}
$ agentic-preflight push --confirm d8697c2068b4853b | jq -c '{ok,state,data:{remote:.data.remote,branch:.data.branch,pr_mode:.data.pr_mode},next}'
{"ok":true,"state":"PUSHED","data":{"remote":"origin","branch":"demo","pr_mode":"auto"},"next":{"command":"agentic-preflight finish","instruction":"Close the pushed validation run."}}
```

The agent must show you the target remote, branch, and commits and obtain fresh approval
before the final command. Automatic PR mode is standing authorization to open the PR
after that push completes, so PR creation has no separate prompt. For a human-only final
push, set `[gate] mode = "manual"`.

Installing a single agent, checking a skill into one repository, upgrading, and using
other Agent Skills clients are covered in
[docs/installation.md](docs/installation.md).

## How it works

```
start --intent "..." → fetch/rebase → context → submit-findings → verify (review)
      → context --section docs → submit-findings → verify   (docs)
      → stage run lint
      → stage run test (automatically skipped for documentation/CI-only changes)
      → mergeback → gate → push → finish → gc
```

After the atomic push, use the forge normally. In automatic PR mode, the skill uses `gh`
directly to create a pull request and inspect its checks. In manual PR mode, it gives the
user a compare URL instead. Those hosted lifecycle operations are not part of the
stateful preflight CLI.

`start` requires the user's objective and acceptance criteria, fetches `origin`, and
rebases the validation checkout onto the fresh base before review. The agent drives the
loop. Every command returns one JSON object containing `next`, the single next legal
command, so the agent never has to guess.

Review submissions bind an `examined: "all"` assertion to the manifest returned by
`context`. Findings cite a review unit when their path and line do not identify one
unambiguously. The CLI derives a compact receipt: units cited by findings and every
remaining unit explicitly examined clean. A findings-only review payload is rejected.

When every changed file is documentation or standard CI configuration, the gate does not
run the final software test command. After lint, it takes an explicit `SKIP_TEST`
transition through `TEST_GREEN` and records the test stage as `skipped` with its reason,
so the exception is visible in `status` and the commit's attestation note. Any source or
otherwise unclassified file keeps tests mandatory.
[docs/change-scope.md](docs/change-scope.md) lists the exact classification.

Risk is classified separately from that execution scope and from diff size. Repository
policy maps changed paths to `low`, `medium`, or `high`, and findings can raise the final
risk. Every high-risk result produces the deterministic verdict `needs_human`. It does
not prevent an approved push. The configured approval mode then requires a manual merge,
a GitHub Environment approval, or an exact-head peer review before merge. The model
reports findings; it cannot override the policy verdict.

By default the run happens directly in the current checkout, which suits a clean,
dedicated one-agent/one-PR worktree. Two isolated modes keep the source checkout
untouched during verification. All three, along with dependency handling and secret
protection, are described in [docs/worktree-modes.md](docs/worktree-modes.md).

## The pre-push hook

`init` installs a pre-push hook that blocks any commit without a green run recorded for
that **exact SHA**:

```
agentic-preflight: push blocked.
  commit: abc1234 (no green run recorded for this exact SHA)
  reason: no valid attestation note is attached to this exact SHA
  fix:    invoke the skill (/agentic-preflight in Claude Code, $agentic-preflight in Codex)
  bypass: git push --no-verify   (documented escape hatch)
```

The hook reads the commit's note in `refs/notes/agentic-preflight`, never touches the
network, and never mutates anything. **If `agentic-preflight` is not on `PATH`, the hook
allows the push and warns.** That is deliberate: a teammate who clones your repo without
installing this tool must not end up with a repository they cannot push from. A broken
tool must not brick the repo.

`init` does not compose with an existing `pre-push` hook. If Husky, pre-commit, or a
custom hook already owns that path, `init` refuses to change it. Add
`agentic-preflight hook-check` to the existing hook manually if you need both. Treat
`init --force` as replacement, not composition: it overwrites the existing hook and
removes whatever behavior that hook previously provided.

## Portable attestations and CI enforcement

Successful merge-back writes a versioned JSON attestation as a Git note on the exact
commit. The note includes the run identity, commit and tree hashes, finding summary,
finding status and severity totals, and a complete stage set. Green lint and test stages
include the exact command, exit code, and SHA-256 of the redacted captured output.
Explicitly skipped stages say why and carry no invented process evidence.

`agentic-preflight push` atomically pushes the branch and
`refs/notes/agentic-preflight`, so the attestation is not stranded in one clone. Git
does not fetch notes in an ordinary checkout; fetch the dedicated ref before reading
or verifying it:

```bash
git fetch origin refs/notes/agentic-preflight:refs/notes/agentic-preflight
git notes --ref=refs/notes/agentic-preflight show HEAD
agentic-preflight verify HEAD
```

`verify <sha>` exits non-zero when the note is missing or malformed, names another
commit, describes another tree, omits a stage, or claims a green shell stage without
its command, zero exit code, and output hash. A minimal GitHub Actions required check
is:

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0
- run: git fetch origin refs/notes/agentic-preflight:refs/notes/agentic-preflight
- run: pipx install agentic-preflight
- name: Verify the attested commit
  env:
    ATTESTED_SHA: ${{ github.event.pull_request.head.sha || github.sha }}
  run: agentic-preflight verify "$ATTESTED_SHA"
```

Make that job a required status check in branch protection. The local hook remains
fail-open and bypassable so it cannot brick a repository; the required remote check
is what rejects a branch tip without an attestation.

This repository dogfoods that check by installing the verifier from the protected PR
base commit, then fetching the proposed commit and its note from the contributor's
remote. The pull request cannot change the verifier that judges it. Governance paths
are also listed in `.github/CODEOWNERS`; enable **Require review from Code Owners** in
the branch ruleset because the file alone only requests reviewers.

High-risk merge handling is enforced by a separate `pull_request_target` workflow that
also installs its policy checker from the protected base and never executes proposed
branch content. It reruns when the head changes or a review is submitted or dismissed.
The default `manual_merge` mode reports success only while GitHub auto-merge is disabled,
and instructs the agent never to merge or enable auto-merge. `environment` pauses a
dedicated job at the configured GitHub Environment, and `peer_review` retains the
exact-head approval rule for an eligible person other than the pull-request author. Make
**high-risk human approval** a required status check on `main`; keep **Require review from
Code Owners** enabled as the stricter ownership rule for sensitive paths. Because the
workflow and policy are loaded from the protected base, a pull request that changes
approval mode is judged by the old mode until that change is merged.

## Limits

**The gate is advisory, not a security boundary.** Three things follow, and you should
know all three before relying on it:

1. **`git push --no-verify` defeats the hook.** By design: it is the documented escape
   hatch for humans who need it.
2. **The confirmation token is not a secret.** The agent can read it from `status`. It is
   deliberate ceremony that makes an *accidental* push impossible and an unconfirmed push
   a visible protocol violation. It does not stop a determined agent. If you need a real
   boundary, set `[gate] mode = "manual"`: agentic-preflight then refuses to push at all
   and a person must run the command themselves.
3. **This guards against mistakes, not against a careless or misaligned agent.** There is
   no cryptographic answer here, and claiming otherwise would be worse than the gap.

**Content-changing rewrites invalidate green.** A fresh run normally binds its note to
an exact SHA. During `start`, an in-place history rewrite may reuse a prior note only if
the old and new commits have identical complete trees, use the same effective preflight
configuration, and produce the same clean Git merge tree against the freshly fetched
base, and the prior attestation has the same user intent. Amends, rebases, and squashes
that fail those proofs require a fresh run.
Cherry-picked merge-back uses the same strict tree-equivalence principle.

**The note is an audit record, not a signature.** Anyone allowed to update the notes
ref can replace it. Protect `refs/notes/agentic-preflight` on the remote if your forge
supports ref-level policy, and treat `verify` as enforcement that a structurally valid,
commit-bound record exists—not proof that the agent's review judgment was good.

**Cryptographic unforgeability is future work.** It requires a signing authority whose
key and execution path the evaluated agent cannot reach; putting an agent-accessible key
around the current note would add ceremony, not a security boundary. The threat model,
key lifecycle, replay protection, and transparency-ledger design are tracked in
[issue #25](https://github.com/elanthus/agentic-preflight/issues/25).

Environment drift between isolated worktrees and your shell, runtime pin activation, and
the exact rebase-reuse boundary are covered in [docs/limits.md](docs/limits.md).

## Configuration

`.agentic-preflight.toml` in the repo root (committed), layered over
`~/.config/agentic-preflight/config.toml`. Unknown keys are errors that name the key.

```toml
[general]
base_ref = "main"

[commands]
lint = "ruff check ."
test = "pytest"

[stage]
timeout_seconds = 600
max_attempts = 5

[review]
blocking_severities = ["critical", "high"]
max_findings = 50

[policy]
# These ownership-sensitive paths are high-risk and require human merge approval.
human_review_paths = [
  ".agentic-preflight.toml",
  ".github/workflows/**",
  ".github/CODEOWNERS",
  "CODEOWNERS",
]
# Every high-risk result requires human merge approval; medium risk does not.
high_risk_paths = ["db/migrations/**", "infra/**"]
medium_risk_paths = ["dependencies/**"]

[docs]
enabled = true
paths = []
require_changelog = false

[diff]
max_bytes = 200000
# Setting exclude REPLACES the eight built-in globs rather than adding to them.
# Omit the key to keep them; see docs/configuration.md for the full default list.
exclude = ["*.lock", "*-lock.json", "vendor/**", "**/*.min.js"]

[worktree]
mode = "in_place"            # default; or "reusable" / "strict"
root = "/optional/external/path" # isolated modes only; defaults outside .git
copy_files = [".env"]        # protected in place, copied when isolated; must be ignored
dependency_setup = "auto"    # pnpm/npm lockfile-aware; use "off" to disable
# setup_command = "uv sync"  # overrides automatic dependency setup

[runtime]
manager = "auto"             # or "none", "nvm", "volta", "asdf", "mise", ...
strict = true                 # never fall back when a pin cannot be activated

[gate]
mode = "token"               # or "manual"

[pr]
mode = "auto"                # default; "manual" reports a compare URL instead

[approval]
mode = "manual_merge"        # default; or "environment" / "peer_review"
environment = "high-risk-review" # GitHub Environment used by environment mode

[hook]
enabled = true
allow_force_push = false

```

The resolved configuration is snapshotted when `start` creates a run, so editing
`.agentic-preflight.toml` afterward does not change that run. Commit configuration
changes before starting the run they should affect.

`[gate] mode` and `[pr] mode` are independent. The gate decides who performs the push;
PR mode decides what happens after an approved push. In `auto` mode, the committed
configuration authorizes the agent to open the PR automatically after preflight finishes.
In `manual` mode, the agent never opens the pull request and reports a compare URL for
the user instead.

`[approval] mode` controls high-risk merge handling. `manual_merge` makes the hosted
check succeed only while auto-merge is disabled and requires the user to perform the
merge; the agent must never merge or enable auto-merge. `environment` requires approval
through the named GitHub Environment. `peer_review` preserves the eligible, non-author,
exact-head pull-request review rule.

The documentation surface and oversized-diff handling are described in
[docs/configuration.md](docs/configuration.md).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | OK |
| 1 | Usage or internal error |
| 2 | Stage failed |
| 3 | Precondition violated |
| 4 | Human resolution required |
| 5 | Confirmation required |
| 10 | Hook blocked a push |

## Requirements

- macOS or Linux (Windows is not supported)
- Python 3.11+
- git 2.30+
- Bash
- `gh` (optional; used for pull requests, hosted checks, and merge verification during
  cleanup; it owns auth, we never handle credentials)

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full contributor workflow and
[SUPPORT.md](SUPPORT.md) for help channels. CI rejects overall coverage below 85% and
also installs the built wheel as a `uv` tool before invoking its CLI.

```bash
uv sync --group dev
uv run pytest
```

Git fixtures drive a real `git` binary rather than mocks: the product *is* git semantics,
so mocking it would test our idea of git instead of git.

## Prior art and differentiation

Agentic Preflight was inspired by [`no-mistakes`](https://github.com/kunchenguid/no-mistakes)
and its staged review, test, documentation, lint, push, pull-request, and CI workflow. It
keeps that useful progression while exploring a different control model:

| Area | `no-mistakes` | Agentic Preflight |
|---|---|---|
| Agent execution | Runs a configurable validation-agent pipeline | Uses local coding agents already active in your workspace |
| Git integration | Routes pushes through a local proxy remote | Uses an advisory pre-push hook; manual mode keeps the final push human-only |
| Interface | Provides a daemon, TUI, and agent skill | Provides a JSON-over-stdout CLI and agent skill |
| Workflow control | Owns the end-to-end validation pipeline | Persists a deterministic state machine and returns the single next legal command |
| Runtime | Ships as a Go application | Ships as a Python CLI package |

## Credits

Created by [@elanthus](https://github.com/elanthus) with development contributions from
OpenAI Codex and Anthropic Claude.

## License

Apache 2.0. See [LICENSE](LICENSE).
