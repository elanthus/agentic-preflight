# agentic-cli

An agent-driven quality gate. Nothing reaches your remote until review, docs, lint, and
tests are all green for that exact commit.

**Python here is a deterministic state machine with a JSON-over-stdout CLI. It never
calls an LLM.** No API keys, no model configuration, no token budgets. The coding agent
you already use does all the thinking; this tool holds the state, checks the claims, and
refuses to let a half-verified branch reach the remote. Being agent-agnostic falls out
of that for free — and is enforced by a test that fails if any module imports an LLM or
HTTP client.

## How it works

```
start → context → submit-findings → respond → verify        (review)
      → context --section docs → submit-findings → verify   (docs)
      → stage run lint → stage run test
      → mergeback → gate → push → finish → gc               (no PR)
      → mergeback → gate → push → pr → [merge] → cleanup    (PR)
```

The agent drives that loop. Every command returns one JSON object containing `next` —
the single next legal command — so the agent never has to guess. Stage-skipping is not
forbidden by documentation; it is **structurally unrepresentable**, because no
transition exists from a review state to a push state. That property is proved by
enumerating every path through the machine, not by testing a few.

Work happens in a disposable git worktree. By default it lives in a hidden sibling
directory, outside `.git`, so Jest and other tools that ignore VCS directories can see
the checkout. Your working tree is never touched during verification. `finish` closes
a pushed run with no PR, while `cleanup` waits for a merged PR and explicit user
confirmation before removing its worktree and local/remote branches.

## Install

```bash
uv tool install agentic-cli
agentic-cli integrations install codex claude
cd your-repo
agentic-cli init             # installs the pre-push hook, writes .agentic-cli.toml
```

Install only the agents you use if you do not need both. Then invoke the skill with
`$agentic-cli` in Codex or `/agentic-cli` in Claude Code. If `uv` reports that its tool
directory is not on `PATH`, run `uv tool update-shell` and open a new shell first.
Restart a running agent if the newly created skill directory is not detected immediately.

The integration installer copies the same bundled skill to each agent's documented
discovery directory. It refuses to overwrite local edits unless you pass `--force`.
After upgrading the CLI, refresh any installed copies:

```bash
uv tool upgrade agentic-cli
agentic-cli integrations update
```

User scope is the default. To check a skill into one repository instead, run
`agentic-cli integrations install codex claude --scope project`. For another agent
that supports Agent Skills, `--target PATH` installs beneath a custom skills directory.
Use `agentic-cli integrations status` to inspect installed copies and
`agentic-cli integrations uninstall codex claude` to remove managed user copies.

## The pre-push hook

`init` installs a pre-push hook that blocks any commit without a green run recorded for
that **exact SHA**:

```
agentic-cli: push blocked.
  commit: abc1234 (no green run recorded for this exact SHA)
  reason: ledger has 9f2c1de; you amended or added a commit since
  fix:    invoke the skill (/agentic-cli in Claude Code, $agentic-cli in Codex)
  bypass: git push --no-verify   (documented escape hatch)
```

The hook reads one file (`ledger.json`), never touches the network, and never mutates
anything. **If `agentic-cli` is not on `PATH`, the hook allows the push and warns.** That
is deliberate: a teammate who clones your repo without installing this tool must not end
up with a repository they cannot push from. A broken tool must not brick the repo.

## Honest limitations

**The gate is advisory, not a security boundary.** Three things follow from that, and
you should know all three before relying on it:

1. **`git push --no-verify` defeats the hook.** By design — it is the documented escape
   hatch for humans who need it.
2. **The confirmation token is not a secret.** The agent can read it from `status`. It is
   deliberate ceremony that makes an *accidental* push impossible and makes an
   unconfirmed push a visible protocol violation. It does not stop a determined agent.
   If you need a real boundary, set `[gate] mode = "manual"` — then agentic-cli refuses
   to push at all and a person must run the command themselves.
3. **This guards against mistakes, not against a careless or misaligned agent.** There is
   no cryptographic answer here, and claiming otherwise would be worse than the gap.

**Amending invalidates green.** The ledger is keyed on exact SHA, so any amend, rebase,
or squash forces a fresh run. Cherry-picked merge-back is handled via tree-equivalence
attestation; rebase tolerance is planned for v2 (the ledger already records `tree_sha`
for it).

**Worktrees can differ from your environment.** A fresh worktree has no `.venv` or
`.env`. Configure `[worktree] setup_command` for non-Node dependencies and
`copy_files` for ignored files such as `.env`. Node lockfiles are handled
automatically: pnpm gets a frozen install backed by its shared content-addressable
store; npm reuses the main checkout's `node_modules` only when dependency inputs are
unchanged and the activated Node ABI matches, otherwise it runs an isolated `npm ci`.
Use `--baseline` so a pre-existing failure is reported rather than blamed on your diff.

**Runtime pins are activated explicitly.** Non-interactive agent shells often miss
interactive version-manager shims. Stages detect committed Node pins for NVM, Volta,
asdf, mise, fnm, and nodenv. A missing pinned manager fails clearly instead of silently
running a different system Node. `init` reports unpinned Node projects so a fresh clone,
CI, and the gate can agree on the version.

## Configuration

`.agentic-cli.toml` in the repo root (committed), layered over
`~/.config/agentic-cli/config.toml`. Unknown keys are errors that name the key.

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
root = "/optional/external/path" # default: hidden sibling directory outside .git
copy_files = [".env"]        # must already be gitignored
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
```

The resolved configuration is snapshotted when `start` creates a run. Editing
`.agentic-cli.toml` afterward does not change that run; the snapshot and its digest are
recorded with the run events. Commit configuration changes before starting the run they
should affect.

The docs stage includes `README*`, `docs/**`, agent instructions such as
`.claude/rules/**` and `.github/instructions/**`, plus `PRODUCT.md` and `DESIGN.md`.
Use `[docs] paths` for repository-specific documentation surfaces.

### Large diffs

Over `[diff] max_bytes`, `context` **refuses** rather than truncating — an agent that
reviews half a diff believing it saw all of it is exactly how a false green happens. The
envelope lists per-file sizes so the agent can narrow with `[diff] exclude`. Common
generated-file globs are excluded by default, which resolves most oversized diffs
outright.

### Secrets in worktrees

Files in `[worktree] copy_files` are copied into the worktree so tests can run, and are
protected by two independent guards:

1. **Preflight refusal** — a file git is not already ignoring *in the worktree* is never
   copied. Add it to `.gitignore` and commit that first.
2. **Commit-content invariant** — any commit touching a copied path is rejected by both
   `respond` and `mergeback`, checked against commit content rather than ignore rules, so
   a `.gitignore` edited mid-run cannot open the hole.

Copies are mode `0600`, die with the worktree, and their contents are redacted from stage
logs and never placed in any envelope.

### Node dependencies in worktrees

With `dependency_setup = "auto"`, a committed `pnpm-lock.yaml` runs
`pnpm install --frozen-lockfile`. pnpm hard-links package contents from its shared
content-addressable store while creating the lockfile-specific layout inside each
worktree, avoiding cross-worktree package duplication. See the
[pnpm storage model](https://pnpm.io/motivation).

For npm, `package.json`, `package-lock.json`, and `.npmrc` must be unchanged from the
run's base and byte-identical to the main checkout before its `node_modules` can be
symlinked. A committed Node requirement must also match the activated Node major; this
prevents sharing native modules across incompatible Node ABIs. Any mismatch runs
`npm ci`, which installs the branch's frozen dependency graph in isolation. The
symlink belongs to the disposable worktree, so normal cleanup removes only the link.

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

- Python 3.11+
- git 2.30+
- `gh` (optional; only for opening pull requests — it owns auth, we never handle
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
