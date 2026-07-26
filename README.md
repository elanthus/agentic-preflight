# agentic-preflight

[![CI](https://github.com/elanthus/agentic-preflight/actions/workflows/ci.yml/badge.svg)](https://github.com/elanthus/agentic-preflight/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-84%25-brightgreen)](https://github.com/elanthus/agentic-preflight/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

An advisory guard against accidental unverified pushes by coding agents. It records an
exact commit's review, tests, documentation check, and lint result, then makes the next
legal action explicit. The default hook can be bypassed and fails open when the command
is missing. Manual mode provides the stronger contract: Agentic Preflight will never
push, and a person must perform the final push.

Agentic Preflight is a deterministic state machine with a JSON-over-stdout CLI. It does
not call an LLM, require API keys, or choose what good code looks like. Your coding agent
does the judgment; this package keeps the workflow state and verifies its claims.

## 60-second walkthrough

Agentic Preflight supports macOS and Linux. From a repository with Python 3.11+ and Git
2.30+:

```bash
uv tool install agentic-preflight
agentic-preflight integrations install codex claude
cd your-repo
agentic-preflight init
```

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

Ask your coding agent to invoke `$agentic-preflight` in Codex or
`/agentic-preflight` in Claude Code. A normal run follows the command in each JSON
envelope. This output was captured from a local demo repository; `jq` limits each
envelope to the fields relevant to the walkthrough:

```console
$ agentic-preflight start --intent "Add retries and document the failure policy" | jq -c '{ok,state,next}'
{"ok":true,"state":"REVIEW_AWAITING_FINDINGS","next":{"command":"agentic-preflight context","instruction":"Fetch the diff before judging it."}}
$ agentic-preflight context | jq -c '{ok,state,data:{changed_files:.data.changed_files},next}'
{"ok":true,"state":"REVIEW_AWAITING_FINDINGS","data":{"changed_files":[".agentic-preflight.toml","change.txt"]},"next":{"command":"agentic-preflight submit-findings --file findings.json","instruction":"Review the diff, then submit findings (an empty list is a valid outcome)."}}
... review, test, docs, and lint complete ...
$ agentic-preflight gate | jq -c '{ok,state,data:{token:.data.token,remote:.data.remote,branch:.data.branch},next}'
{"ok":true,"state":"AWAITING_PUSH_CONFIRM","data":{"token":"d8697c2068b4853b","remote":"origin","branch":"demo"},"next":{"command":"agentic-preflight push --confirm d8697c2068b4853b","instruction":"Show the user the remote, branch, and commit list in plain language and ask whether to push. Never push without asking."}}
$ agentic-preflight push --confirm d8697c2068b4853b | jq -c '{ok,state,data:{remote:.data.remote,branch:.data.branch},next}'
{"ok":true,"state":"PUSHED","data":{"remote":"origin","branch":null},"next":{"command":"agentic-preflight pr","instruction":"Open the pull request."}}
```

The agent must show you the target remote, branch, and commits and obtain fresh approval
before the final command. For a human-only final push, set `[gate] mode = "manual"`.

## How it works

```
start --intent "..." → fetch/rebase → context → submit-findings → verify (review)
      → stage run test (automatically skipped for documentation/CI-only changes)
      → context --section docs → submit-findings → verify   (docs)
      → stage run lint
      → mergeback → gate → push → finish → gc               (no PR)
      → mergeback → gate → push → pr → ci → cleanup         (PR)
```

`start` requires the user's objective and acceptance criteria, fetches `origin`, and
rebases the validation checkout onto the fresh base before review. The agent
drives the loop. Every command returns one JSON object containing `next`,
the single next legal command, so the agent never has to guess. Stage-skipping is not
forbidden by documentation; it is **structurally unrepresentable**, because no
transition exists from a review state to a push state. That property is proved by
enumerating every path through the machine, not by testing a few.

When every changed file is documentation or standard CI configuration, the gate does
not run the software test command. It takes an explicit `SKIP_TEST` transition through
`TEST_GREEN` and records the test stage as `skipped` with its reason, so the exception
is visible in `status` and the ledger. Any source or otherwise unclassified file keeps
tests mandatory. Documentation includes Markdown, MDX, reStructuredText, AsciiDoc, the
standard documentation surface, and `[docs] paths`; CI configuration includes common
GitHub Actions, CircleCI, GitLab CI, Azure Pipelines, Bitbucket Pipelines, Buildkite,
Travis CI, AppVeyor, and Jenkins paths.

By default, work happens directly in the current checkout (`[worktree] mode =
"in_place"`). This is intended for a clean, dedicated one-agent/one-PR worktree: the
fresh-base rebase and accepted repair commits land directly on the PR branch, and
`mergeback` becomes a no-op attestation of the exact SHA that passed every stage. Any
uncommitted change or unaccounted branch movement stops the run.

Two isolated modes remain available. `mode = "reusable"` leases one runner in a hidden
sibling directory serially across runs, preserving ignored dependency and build caches.
`mode = "strict"` creates a fresh worktree for every run and removes it afterward. Both
keep the source checkout untouched during verification; the runner is outside `.git`,
so Jest and other tools that ignore VCS directories can see it.

Reusable mode resets tracked files, removes non-ignored untracked files, explicitly
removes every `[worktree] copy_files` entry, and then detaches the runner before its
lease ends. Other ignored files survive deliberately. This reduces local disk churn but
is not a hermetic environment: a test can mutate an ignored cache. Use strict mode when
each local validation must begin with no retained artifacts; remote CI should remain the
clean verification boundary in either mode.

## Install

```bash
uv tool install agentic-preflight
agentic-preflight integrations install codex claude
cd your-repo
agentic-preflight init             # installs the pre-push hook, writes .agentic-preflight.toml
```

Install only the agents you use if you do not need both. Then invoke the skill with
`$agentic-preflight` in Codex or `/agentic-preflight` in Claude Code. If `uv` reports that its tool
directory is not on `PATH`, run `uv tool update-shell` and open a new shell first.
Restart a running agent if the newly created skill directory is not detected immediately.

The integration installer copies the same bundled skill to each agent's documented
discovery directory. It refuses to overwrite local edits unless you pass `--force`.
After upgrading the CLI, refresh any installed copies:

```bash
uv tool upgrade agentic-preflight
agentic-preflight integrations update
```

User scope is the default. To check a skill into one repository instead, run
`agentic-preflight integrations install codex claude --scope project`. For another agent
that supports Agent Skills, `--target PATH` installs beneath a custom skills directory.
Use `agentic-preflight integrations status` to inspect installed copies and
`agentic-preflight integrations uninstall codex claude` to remove managed user copies.

## The pre-push hook

`init` installs a pre-push hook that blocks any commit without a green run recorded for
that **exact SHA**:

```
agentic-preflight: push blocked.
  commit: abc1234 (no green run recorded for this exact SHA)
  reason: ledger has 9f2c1de; you amended or added a commit since
  fix:    invoke the skill (/agentic-preflight in Claude Code, $agentic-preflight in Codex)
  bypass: git push --no-verify   (documented escape hatch)
```

The hook reads one file (`ledger.json`), never touches the network, and never mutates
anything. **If `agentic-preflight` is not on `PATH`, the hook allows the push and warns.** That
is deliberate: a teammate who clones your repo without installing this tool must not end
up with a repository they cannot push from. A broken tool must not brick the repo.

## Limits

**The gate is advisory, not a security boundary.** Three things follow from that, and
you should know all three before relying on it:

1. **`git push --no-verify` defeats the hook.** By design: it is the documented escape
   hatch for humans who need it.
2. **The confirmation token is not a secret.** The agent can read it from `status`. It is
   deliberate ceremony that makes an *accidental* push impossible and makes an
   unconfirmed push a visible protocol violation. It does not stop a determined agent.
   If you need a real boundary, set `[gate] mode = "manual"`: then agentic-preflight refuses
   to push at all and a person must run the command themselves.
3. **This guards against mistakes, not against a careless or misaligned agent.** There is
   no cryptographic answer here, and claiming otherwise would be worse than the gap.

**Amending invalidates green.** The ledger is keyed on exact SHA, so any amend, rebase,
or squash forces a fresh run. Cherry-picked merge-back is handled via tree-equivalence
attestation; rebase tolerance is planned for v2 (the ledger already records `tree_sha`
for it).

**Isolated worktrees can differ from your environment.** In-place mode deliberately uses
the checkout's existing dependencies and ignored files. An isolated runner does not
inherit the source checkout's `.venv` or `.env`; configure `[worktree] setup_command`
for non-Node dependencies and `copy_files` for ignored files such as `.env`. Node
lockfiles are handled automatically: reusable mode retains a fingerprint-matched
install, while strict mode installs the frozen dependency tree in every fresh worktree.
Neither isolated mode uses the source checkout's `node_modules`.
Use `--baseline` so a pre-existing failure is reported rather than blamed on your diff.

**Runtime pins are activated explicitly.** Non-interactive agent shells often miss
interactive version-manager shims. Stages detect committed Node pins for NVM, Volta,
asdf, mise, fnm, and nodenv. A missing pinned manager fails clearly instead of silently
running a different system Node. `init` reports unpinned Node projects so a fresh clone,
CI, and the gate can agree on the version.

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

[docs]
enabled = true
paths = []
require_changelog = false

[diff]
max_bytes = 200000
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

[hook]
enabled = true
allow_force_push = false

[publish]
provider = "auto"
draft_pr = false
pr_title = "Optional fixed title"

[ci]
timeout_seconds = 3600
poll_interval_seconds = 30
```

After a PR opens, `agentic-preflight ci` monitors checks and mergeability. It reports passed
checks, fetches failed GitHub Actions logs, and persists the failure with the original
intent. Repairs are host-driven: agentic-preflight never calls a model. The host agent fixes
and commits the source branch, then starts a fresh synchronized full validation before
another push. Monitoring continues through host invocations until merge, close, or
timeout.

The resolved configuration is snapshotted when `start` creates a run. Editing
`.agentic-preflight.toml` afterward does not change that run; the snapshot and its digest are
recorded with the run events. Commit configuration changes before starting the run they
should affect.

The docs stage includes `README*`, `docs/**`, agent instructions such as
`.claude/rules/**` and `.github/instructions/**`, plus `PRODUCT.md` and `DESIGN.md`.
Use `[docs] paths` for repository-specific documentation surfaces.

### Large diffs

Over `[diff] max_bytes`, `context` **refuses** rather than truncating: an agent that
reviews half a diff believing it saw all of it is exactly how a false green happens. The
envelope lists per-file sizes so the agent can narrow with `[diff] exclude`. Common
generated-file globs are excluded by default, which resolves most oversized diffs
outright.

### Secrets in worktrees

Files in `[worktree] copy_files` are used in place or copied into an isolated worktree so
tests can run, and are protected by two independent guards:

1. **Preflight refusal**: a file git is not already ignoring in the validation checkout
   is never used or copied. Add it to `.gitignore` and commit that first.
2. **Commit-content invariant**: any commit touching a copied path is rejected by both
   `respond` and `mergeback`, checked against commit content rather than ignore rules, so
   a `.gitignore` edited mid-run cannot open the hole.

Isolated copies are mode `0600`, are removed explicitly when a reusable runner is
released (or die with a strict worktree). In-place files are never moved or removed.
Their contents are redacted from stage logs and never placed in any envelope.

### Node dependencies in worktrees

In-place mode reuses the checkout's existing dependency environment and does not run an
automatic install. An explicit `setup_command` still runs.

In isolated modes with `dependency_setup = "auto"`, a committed `pnpm-lock.yaml` uses
`pnpm install --frozen-lockfile`. pnpm hard-links package contents from its shared
content-addressable store. In reusable mode, the install is retained and skipped when
its fingerprint still matches. See the
[pnpm storage model](https://pnpm.io/motivation).

For npm, strict mode runs `npm ci` in every fresh worktree. Reusable mode runs it the
first time and whenever the dependency fingerprint changes; otherwise it retains the
runner's existing `node_modules`. The fingerprint covers dependency and runtime pin
files, the activated Node version and modules ABI, package-manager version, platform,
architecture, and install command. The source checkout's `node_modules` is never linked
or modified by isolated modes.

To switch modes, commit one of these settings and start a new run (an active run keeps
the configuration snapshot it started with):

```toml
[worktree]
mode = "in_place" # default; validate and repair directly in this clean PR checkout
```

```toml
[worktree]
mode = "reusable" # one serial isolated runner; retained ignored caches
```

```toml
[worktree]
mode = "strict"   # fresh worktree and dependency install for every run
```

The first strict run removes any idle reusable runner and its retained dependency
fingerprint. Switching back to reusable mode therefore begins with one clean install.
In-place mode leaves any idle reusable runner alone. Mode changes never reshape an
active run because its configuration is snapshotted.

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
- `gh` (optional; only for opening pull requests: it owns auth, we never handle
  credentials)

## Development

```bash
uv sync --group dev
uv run pytest
```

Git fixtures drive a real `git` binary rather than mocks: the product *is* git
semantics, so mocking it would test our idea of git instead of git.

## License

Apache 2.0. See [LICENSE](LICENSE).
