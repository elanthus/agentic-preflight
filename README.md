# agentic-preflight

[![CI](https://github.com/elanthus/agentic-preflight/actions/workflows/ci.yml/badge.svg)](https://github.com/elanthus/agentic-preflight/actions/workflows/ci.yml)
[![Coverage](https://raw.githubusercontent.com/elanthus/agentic-preflight/badges/coverage.svg)](https://github.com/elanthus/agentic-preflight/tree/badges)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://github.com/elanthus/agentic-preflight/blob/v0.5.2.1/pyproject.toml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](https://github.com/elanthus/agentic-preflight/blob/v0.5.2.1/LICENSE)

**Stops your coding agent from pushing unverified work.**

Agentic Preflight records a review, test, documentation, and lint result against a
commit, and a pre-push hook refuses a commit with no applicable green run. A
freshly fetched base keeps green only when synchronization leaves the exact attested
commit unchanged, that commit contains the fresh base, its clean merge result still
matches the attested base, and the recorded branch, base ref, effective configuration,
and user intent still match. Any rewritten commit requires a new run.

![A push blocked by the pre-push hook, a review that catches an unguarded division by
zero, the fix verified, and the gate stopping to ask before it pushes](docs/demo.gif)

Every frame above is real CLI output, recorded with [VHS](https://github.com/charmbracelet/vhs).
Regenerate it yourself with `./docs/demo-fixture.sh && vhs docs/demo.tape`: the script
builds a throwaway repo with a genuine unguarded division in it, and the tape drives the
run. The judgment between the commands is the agent's; the commands are all this package
does.

Three things separate it from a checklist in a prompt:

- **Within a run, no gate can be bypassed.** An inapplicable test or disabled docs stage
  advances only through an explicit skip transition that records why. No path exists from
  review to push without traversing every load-bearing gate. That property is proved by
  enumerating every path through the machine, not by testing a few of them.
- **Your agent judges by default; this keeps the record.** The core CLI has no API key
  and calls no model. It drives the coding agent already active in your workspace, while
  an optional command executor lets repository policy require an independent reviewer
  for selected risk levels.
- **Gaps do not turn into green evidence.** An unattested SHA fails the hook and CI
  verifier, an applicable skip carries its reason, and a failed stage stays in local run
  history without producing a green attestation. A record that can only report success
  is marketing.

## Dogfooding case study

From August 3–17, 2026, the public history of four repositories contains 153 merged pull
requests. Of those, 122 PR descriptions explicitly record Agentic Preflight use, and 24
conservatively record at least one concrete finding. The recurring catches were semantic
boundary failures: stale evidence reuse, approval eligibility, secret normalization,
trust-domain selection, resumability, and immutable inputs.

[Read the evidence, representative findings, methodology, and limits](docs/dogfooding-case-study.md).

Agentic Preflight is a deterministic state machine with a JSON-over-stdout CLI. It runs
on macOS, Linux, and Windows.

[Independent review and agreement](docs/independent-review.md) shows how to run and measure
a second command-line reviewer over the same review bundle.

## Regression eval

The [public regression eval](docs/regression-eval.md) is a synthetic smoke corpus that drives
the real command-review product path across vulnerable and fixed toy snapshots. Its dry mode
checks deterministic plumbing and scoring; it is not the private decision-quality evaluation,
and its results are not comparable to that evaluation.

## Quickstart

From a repository using a supported Python version (currently 3.11 through 3.13) and
Git 2.30+:

```bash
uv tool install agentic-preflight
agentic-preflight integrations install codex claude cursor opencode amp
cd your-repo
agentic-preflight init
```

When working from this source checkout, `./install.sh` installs or updates the CLI and
all five supported agent integrations in one step (`.\install.ps1` on Windows). Pass
integration names to choose only the coding agents you use.
Run `./uninstall.sh` (or `.\uninstall.ps1`) to remove the managed skills and CLI. It pauses first so you can
enter `agentic-preflight:uninstall` in every initialized repository; that trigger
removes the repository configuration and managed hook logic while preserving unrelated
hooks, run history, and attestations.

`init` writes `.agentic-preflight.toml` and installs an advisory pre-push hook. Make and
commit a change, then try to push it before validation:

```console
$ git push
agentic-preflight: push blocked.
  commit: 4f15c2a (no green run recorded for this exact SHA)
  reason: no valid attestation note is attached to this exact SHA
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
{"ok":true,"state":"AWAITING_PUSH_CONFIRM","data":{"token":"d8697c2068b4853b","remote":"origin","branch":"demo","pr_mode":"auto","approval_mode":"manual_merge"},"next":{"command":"agentic-preflight push --confirm <token>","instruction":"Show the user the remote, branch, and commit list in plain language. If the user explicitly requested a push, publish, or asked to create or open a pull request in this task, that request authorizes this push when the summary matches the requested work; proceed without asking again. Otherwise, ask whether to push and wait for their answer. After user authorization, the agent substitutes data.token for <token> in next.command. This high-risk pull request must be merged manually by the user; the agent must not merge it or enable auto-merge. After the confirmed push and preflight finish, automatically open or reuse the pull request; auto mode is standing authorization, so do not ask again."}}
$ agentic-preflight push --confirm <token> | jq -c '{ok,state,data:{remote:.data.remote,branch:.data.branch,pr_mode:.data.pr_mode},next}'
{"ok":true,"state":"PUSHED","data":{"remote":"origin","branch":"demo","pr_mode":"auto"},"next":{"command":"agentic-preflight finish","instruction":"Close the pushed validation run."}}
```

The agent must show you the target remote, branch, and commits before the final command.
If you explicitly asked it to push, publish, or create/open a pull request in the
current task, that request is already approval for the matching push—there is no second
confirmation after verification. If you requested only implementation or committing,
or the summary contains an unexpected target or change, the agent asks before pushing.
Automatic PR mode is standing authorization to open the PR after that push completes.
For a human-only final push, set `[gate] mode = "manual"`.

Installing a single agent, checking a skill into one repository, upgrading, and using
other Agent Skills clients are covered in
[installation guide](https://github.com/elanthus/agentic-preflight/blob/v0.5.2.1/docs/installation.md).

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

`start` requires the user's objective and acceptance criteria. When `origin` exists, it
fetches it and rebases the validation checkout onto the fresh base before review. The
agent drives the loop. Every agent-facing workflow command returns one JSON object
containing `next`, the single next legal command, so the agent never has to guess.

Review submissions bind an `examined: "all"` assertion to the manifest returned by
`context`. Findings cite a review unit when their path and line do not identify one
unambiguously. The CLI derives a compact receipt: units cited by findings and every
remaining unit explicitly examined clean. A findings-only review payload is rejected.
The [grounded context layer](docs/context-grounding.md) adds bounded repository-owned
knowledge to that snapshot without a model or network access.

When every changed file is documentation or standard CI configuration, the gate does not
run the final software test command. After lint, it takes an explicit `SKIP_TEST`
transition through `TEST_GREEN` and records the test stage as `skipped` with its reason,
so the exception is visible in `status` and the commit's attestation note. Any source or
otherwise unclassified file keeps tests mandatory.
[change-scope reference](https://github.com/elanthus/agentic-preflight/blob/v0.5.2.1/docs/change-scope.md)
lists the exact classification.

Risk is classified separately from that execution scope and from diff size. Repository
policy maps changed paths to `low`, `medium`, or `high`, and findings can raise the final
risk. Every high-risk green result produces the deterministic verdict `needs_human`. It
does not prevent an approved push. The skill honors the configured approval mode as a
manual merge, a GitHub Environment approval, or an exact-head peer review. Forge-level
enforcement additionally requires the documented protected-base workflow and required
status check; configuration alone does not change repository branch rules. The model
reports findings; it cannot override the policy verdict.

By default the run happens directly in the current checkout, which suits a clean,
dedicated one-agent/one-PR worktree. Two isolated modes keep the source checkout
untouched during verification. All three, along with dependency handling and secret
protection, are described in the
[worktree-modes guide](https://github.com/elanthus/agentic-preflight/blob/v0.5.2.1/docs/worktree-modes.md).

Run ownership is scoped to that source worktree, not the clone. Multiple agents can gate
different PR branches concurrently from linked worktrees; `status` in each checkout
resolves its own run. A matching repeated `start` resumes the existing run, while a moved
source head automatically orphans the stale record without deleting its logs or isolated
fixes. Use `status --all` to inventory the clone and `agentic-preflight --run RUN_ID ...`
to inspect or recover a specific stored run.

## The pre-push hook

`init` installs a pre-push hook that blocks a pushed ref when its tip has no green run
recorded for that **exact SHA**:

```
agentic-preflight: push blocked.
  commit: abc1234 (no green run recorded for this exact SHA)
  reason: no valid attestation note is attached to this exact SHA
  fix:    invoke the skill (/agentic-preflight in Claude Code, $agentic-preflight in Codex)
  bypass: git push --no-verify   (documented escape hatch)
```

The hook reads the tip's note in `refs/notes/agentic-preflight`, never touches the
network, and never mutates anything. It is deliberately fail-open when the executable is
unavailable. Existing-hook composition, force-push policy, and the exact failure modes
are covered in the
[pre-push hook guide](https://github.com/elanthus/agentic-preflight/blob/v0.5.2.1/docs/pre-push-hook.md).

## Portable attestations and CI enforcement

Successful merge-back writes a versioned JSON attestation as a Git note on the exact
commit. `agentic-preflight push` atomically pushes the branch and
`refs/notes/agentic-preflight`, so the attestation is not stranded in one clone. To
inspect one after an ordinary checkout:

```bash
git fetch origin refs/notes/agentic-preflight:refs/notes/agentic-preflight
git notes --ref=refs/notes/agentic-preflight show HEAD
agentic-preflight verify HEAD
```

The note schema, fail-closed CI check, protected-base verifier pattern, and high-risk
approval workflow are documented in
[Portable attestations and CI enforcement](https://github.com/elanthus/agentic-preflight/blob/v0.5.2.1/docs/attestations-and-ci.md).

## Limits

**The gate is advisory, not a security boundary.** Three things follow, and you should
know all three before relying on it:

1. **`git push --no-verify` defeats the hook.** By design: it is the documented escape
   hatch for humans who need it.
2. **The confirmation token is not a secret.** The agent can read it from `status`. It is
   deliberate ceremony that makes an *accidental* push impossible and an unconfirmed push
   a visible protocol violation. It does not stop a determined agent. For a hard boundary
   inside this CLI, set `[gate] mode = "manual"`: Agentic Preflight then refuses to run its
   push command and hands the exact Git command to a person. A shell-capable agent could
   still invoke Git directly, which is why this remains advisory rather than a security
   boundary.
3. **This guards against mistakes, not against a careless or misaligned agent.** There is
   no cryptographic answer here, and claiming otherwise would be worse than the gap.

Attestation mutability, history-rewrite reuse, environment drift, and the boundary of
what a green run proves are covered in the
[limits guide](https://github.com/elanthus/agentic-preflight/blob/v0.5.2.1/docs/limits.md).

## Configuration

`.agentic-preflight.toml` in the repo root (committed), layered over
`~/.config/agentic-preflight/config.toml`. Unknown keys are errors that name the key.
`init` writes a commented starting configuration. The complete example, every section,
and the behavior behind the less-obvious keys live in the
[configuration reference](https://github.com/elanthus/agentic-preflight/blob/v0.5.2.1/docs/configuration.md).

**The configuration executes committed code.** During a run, `[worktree]
setup_command` runs at `start`, every `[commands]` entry runs at its lint or test
stage, and `[review] command` runs when command review is in effect (`executor =
"command"`, or a risk level listed in `require_command_for`). Each may come from the
committed file or your user config, with the repository file overriding your user
config section by section — so the repository being validated decides what runs with
your privileges. Starting a preflight run on a repository is equivalent in trust to
running that repository's own build or tests — do not run it on a clone you would not
build.

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

- A supported macOS/Linux/Windows and Python combination from the
  [compatibility policy](https://github.com/elanthus/agentic-preflight/blob/v0.5.2.1/COMPATIBILITY.md)
- git 2.30+
- A POSIX shell, only for stage commands that use shell grammar such as pipes or `&&`.
  Plain commands run without one. On Windows this is the shell Git for Windows
  installs; nothing extra to set up.
- `gh` (optional; used for pull requests, hosted checks, and merge verification during
  cleanup; it owns auth, we never handle credentials)

## Development

See the [contributor guide](https://github.com/elanthus/agentic-preflight/blob/v0.5.2.1/CONTRIBUTING.md)
for the full workflow and [support guide](https://github.com/elanthus/agentic-preflight/blob/v0.5.2.1/SUPPORT.md)
for help channels. CI rejects overall coverage below 85% and also installs the built
wheel as a `uv` tool before invoking its CLI.

The review and CLI module boundaries are recorded in
[ADR 0001](docs/adr/0001-orchestration-boundaries.md).

```bash
uv sync --group dev
uv run pytest
```

Git fixtures drive a real `git` binary rather than mocks: the product *is* git semantics,
so mocking it would test our idea of git instead of git.

## Prior art and differentiation

Agentic Preflight was inspired by [`no-mistakes`](https://github.com/kunchenguid/no-mistakes)
and its staged review, test, documentation, lint, push, pull-request, and CI workflow. As
of [`no-mistakes` v1.48.0](https://github.com/kunchenguid/no-mistakes/releases/tag/v1.48.0),
both projects bind publication to reviewed work and both emit structured evidence. They
make different tradeoffs about who owns the workflow and what the durable record proves:

| Area | `no-mistakes` | Agentic Preflight |
|---|---|---|
| Agent execution | Launches a required, configurable pipeline agent with ordered fallbacks | Uses the coding agent already active by default; an external command reviewer can be required by risk |
| Git integration | Routes an opted-in push through a local proxy remote | Uses an advisory pre-push hook; manual mode disables the CLI's own push path |
| Stage control | Fixes the stage order but permits per-run and approval-time skips | Makes every gate load-bearing; only explicit code/config-driven skips traverse it and record a reason |
| Review completeness | Reviews the diff and records the exact approved head | Inventories every included changed hunk and non-text change after `[diff] exclude`, then requires a snapshot-bound `examined: "all"` assertion and derives a cited/clean receipt |
| Durable evidence | Writes a data-only step-status snapshot into the PR body; it can become stale until the body is rewritten | Atomically pushes a schema-validated Git note bound to the exact commit and tree, with config/intent bindings, review coverage, executor evidence, and shell-output hashes |
| Risk and approval | The reviewer returns `risk_level` and rationale; findings pause for user action | Repository path policy and recorded findings deterministically derive risk; the model cannot lower the verdict |
| Publication approval | Automatically forwards the validated branch after the local pipeline | Shows the exact remote, branch, commits, and risk before a token-gated push, or refuses its own push in manual mode |
| Local architecture | Runs a daemon, proxy repository, SQLite store, TUI, and disposable worktrees | Runs as a daemonless JSON-over-stdout CLI with file-based state and an agent skill |
| Validation checkout | Always isolates the pipeline in a disposable worktree | Offers in-place, reusable isolated, and fresh strict worktree modes |
| Hosted lifecycle | Creates PRs across several forges, monitors CI, and can auto-fix failures | Keeps hosted lifecycle outside the stateful core and delegates GitHub operations to the active agent and `gh` |
| Runtime and platforms | Ships as a Go application for macOS, Linux, and Windows | Ships as a Python package for supported macOS, Linux, and Windows combinations |

## Credits

Created by [@elanthus](https://github.com/elanthus) with development contributions from
OpenAI Codex and Anthropic Claude.

## License

Apache 2.0. See the [license](https://github.com/elanthus/agentic-preflight/blob/v0.5.2.1/LICENSE).
