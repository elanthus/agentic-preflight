# agentic-preflight — agent-driven quality gate

> Historical design record. Fresh-base synchronization, persisted user intent,
> review → test → docs → lint ordering, and host-driven CI monitoring were implemented
> after this plan; statements below that defer those capabilities describe the original
> v1 scope, not the current CLI.

## Context

The Go tool [`no-mistakes`](https://github.com/kunchenguid/no-mistakes) gates pushes behind an AI validation pipeline (review → test → docs → lint → push → PR → CI) by running a **local git proxy remote**: you `git push no-mistakes`, a daemon intercepts, validates in a disposable worktree, and only then forwards to the real remote.

We want the same guarantee — *nothing reaches the remote until every check is green* — without the proxy. Roughly half of the Go project's `internal/` (daemon, IPC, proxy, Windows process handling) exists only to support proxy interception. Dropping it removes that mass entirely.

The replacement architecture: **Python is a deterministic state machine with a JSON-over-stdout CLI; the host coding agent is the only thinking component; a pre-push git hook is a pure predicate over a SHA ledger.** Python never calls an LLM — no API keys, no model config, no token budgets — which makes the tool agent-agnostic for free and removes most of what would otherwise need building.

Outcome: a standalone, pip-installable repo named **`agentic-preflight`** whose `SKILL.md` any coding agent can drive to review, document, lint, test, and ship a branch, with a hook that makes skipping the gate hard to do by accident.

## Decisions (settled during brainstorming)

| Decision | Choice |
|---|---|
| Name | `agentic-preflight`. Package `agentic_preflight`, console script `agentic-preflight` (alias `ap`), skill `/agentic-preflight`, config `.agentic-preflight.toml`, state under `$GIT_COMMON_DIR/agentic-preflight/`, external worktrees on branches `ap/<run_id>`. |
| Home | Standalone new repo. Does not depend on `appkit`. |
| Trigger | On-demand skill (`/agentic-preflight`) **plus** optional pre-push hook installed by `init`. |
| v1 stages | review, **docs**, lint, test, push+PR. (intent, rebase, CI monitoring are out.) |
| AI execution | **Host agent does all thinking.** Python calls no LLM, ever. |
| Hook role | Pure predicate. It's a subprocess of the agent's own Bash call and cannot call back up, so it verifies and bounces the agent back with an instructional stderr message. |
| Ledger key | Exact commit SHA. Any amend/new commit invalidates. No per-stage caching in v1. |
| Isolation | Disposable worktree; user's tree untouched. |
| Merge-back | Cherry-pick, **never auto-resolve** — clean abort on conflict, hand back an explicit resolution path. |
| Worktree env | `setup_command` + `copy_files` (default `[".env"]`, copied untracked and refused unless already gitignored), plus a **baseline check** (run tests against the base commit; if base is red, say so instead of blaming the diff). |

## Architecture

Package `agentic_preflight`, console script `agentic-preflight` (alias `ap`). Deps: `click`, `pydantic>=2.6`, `tomllib`. **No LLM SDKs — enforced by an import-graph test.**

```
agentic-preflight/
  skill/SKILL.md              # agent-facing contract
  skill/reference/            # commands.md, findings-schema.md, docs-rubric.md
  agentic_preflight/
    cli.py                    # arg parsing + envelope emit ONLY, no logic
    envelope.py machine.py store.py models.py events.py
    gitx.py worktree.py diff.py findings.py mergeback.py ledger.py
    stages/  review.py docs.py shellstage.py detect.py
    publish/ provider.py github.py gate.py
    hook.py initcmd.py config.py errors.py
  tests/
```

**Critical files, in dependency order:** `machine.py` (state enum + transition table + guards — every module consumes it), `store.py` (atomic persistence), `models.py`, `envelope.py`, `mergeback.py`, `skill/SKILL.md`.

### The stdout contract

Every command prints **exactly one JSON object** to stdout; human prose goes to stderr. The agent must be able to `json.loads(stdout)` blindly.

```json
{"ok": true, "run_id": "r_...", "state": "AWAITING_FINDINGS", "stage": "review",
 "data": {}, "blocking": [], "next": {"instruction": "...", "command": "agentic-preflight ..."},
 "error": null}
```

`next` is the anti-wandering device: after any command, the agent is told the single next legal command.

Exit codes: `0` ok · `1` usage/internal · `2` stage failed · `3` precondition violated · `4` human resolution required · `5` confirmation required · `10` hook block.

### State machine and persistence (the crux)

A run spans **multiple agent turns** — Python cannot block waiting for the agent to think — so state persists to disk between CLI invocations.

Run dir lives under `$GIT_COMMON_DIR/agentic-preflight/` (use `GIT_COMMON_DIR`, not `GIT_DIR`, so it works when the user is already inside a worktree). Never committed, never in `git status`, one namespace per clone.

```
ledger.json · current · runs/<run_id>/{run.json,events.jsonl,findings.json,diff/,stages/,logs/}
```

Worktrees live outside `.git` in a per-clone hidden sibling directory by default. This
is required for tools such as Jest that hard-ignore every real path beneath `.git`.

`run.json` is the state document: `run_id, seq, state, branch, base_ref, merge_base_sha, head_sha, worktree_path, worktree_branch, config_snapshot, config_digest, fix_commits[], stages{}, gate_token, pushed_sha, pr_url`.

Persistence discipline: every mutation is `load → guard → mutate → write tmp → os.replace`, under an `fcntl.flock` for the read-modify-write window (two parallel Bash calls in one agent turn are a real hazard). `--expect-seq N` rejects stale writes with exit 3.

States — the two agent-judgment stages (review, docs) share one sub-machine shape, then the deterministic shell stages run:

```
CREATED → WORKTREE_READY
  → REVIEW_AWAITING_FINDINGS → REVIEW_SUBMITTED → {REVIEW_AWAITING_RESPONSES|REVIEW_FIXING} → REVIEW_GREEN
  → DOCS_AWAITING_FINDINGS   → DOCS_SUBMITTED   → {DOCS_AWAITING_RESPONSES|DOCS_FIXING}     → DOCS_GREEN
  → LINT_RUNNING → LINT_GREEN | LINT_RED
  → TEST_RUNNING → TEST_GREEN | TEST_RED
  → MERGEBACK_PENDING → VERIFIED → AWAITING_PUSH_CONFIRM → PUSHED → PR_OPEN → DONE
plus MERGEBACK_CONFLICT / ABORTED / ORPHANED
```

Transitions live in one table (`TRANSITIONS`, `GUARDS`) in `machine.py`. Every command declares `requires_state`. **Stage-skipping is prevented structurally, not by prose — it is not expressible.** A wrong-state command exits 3 with `next` pointing at the correct one; `status` is legal in every state and is the universal recovery entry point.

**Staleness:** every command re-compares the branch tip to `run.head_sha`. If it moved past the first findings submission, mark stale and force restart. Never continue against a moved head — that is exactly how a false green enters the ledger.

### Findings — code owns identity, agent owns judgment

Review and docs share one findings pipeline. `FindingSubmission` (what the agent sends) has **no `id` and no `stage` field**, and `extra="forbid"` makes a hallucinated one a hard validation error rather than a silently-honoured one. `Finding` (what code stores) adds code-assigned `id` (`F001`, append-only across the whole run, not per stage), `stage` (**derived from the active state at submission time, never agent-supplied**), `status`, `fix_commit`.

- **Code validates (reject):** path inside worktree (no `..`/absolute/symlink escape), path in the changed-file set *or* the docs allowlist when `stage == docs`, line within file bounds, enums, length caps, count under `max_findings`.
- **Agent is trusted for:** severity, action, title, detail, suggestion.
- **Code derives:** ID, stage, status, ordering, blocking-set (`severity in {critical,high}` OR `action == ask_user`).

`respond --id F003 --action fixed --commit <sha>` verifies the commit exists in the worktree branch and touches the finding's file — the claim is checked, not trusted. Unknown ID → exit 3 listing valid IDs.

### The docs stage

Agent-driven, no shell command — it reuses the findings machinery entirely, which is why it's cheap to add.

`context --section docs` returns: the diff, plus a **code-built inventory** of the repo's documentation surface — `README*`, `CLAUDE.md`/`AGENTS.md`, `.claude/rules/**`, `.github/instructions/**`, `PRODUCT.md`, `DESIGN.md`, `docs/**`, `CHANGELOG*`, and (from `[docs] paths` config) anything else — each with existence, size, and last-modified-vs-diff status. Code assembles this; the agent does not go hunting.

The agent judges whether the diff obligates a doc change and submits findings against doc files. Typical shape: a new CLI flag with no README entry (`auto_fix`), a changed public API with a stale usage example (`auto_fix`), a behavior change whose documented contract is now ambiguous (`ask_user`). Zero findings → `DOCS_GREEN`; a docs-clean diff is the common case and must be cheap.

Two guards worth naming:

- **Docs findings may target files outside the diff** — that's the entire point (the diff changed code, the doc that needs updating didn't change). So `stage == docs` relaxes the changed-file constraint to a **docs allowlist** derived from `[docs] paths` + the standard names above. It does not become unconstrained; a "docs" finding against `src/auth.py` is still rejected.
- **`require_changelog`** (config, default `false`): when true, code checks deterministically whether the changelog was touched and injects a code-owned blocking finding if not. That's a mechanical rule, so code owns it rather than asking the agent to remember.

Rubric lives in `skill/reference/docs-rubric.md`, not in SKILL.md, so it's pulled just-in-time.

### Worktree lifecycle

Create via `git worktree add --detach <path> <head_sha>` then `git switch -c ap/<run_id>` inside (detached-then-branch avoids clobbering an existing name). Write the intent record to `run.json` *before* the git call so a crash mid-create is recoverable.

Agent gets an **absolute** `worktree_path` from `context`; don't rely on `cd` persisting across tool calls. `submit-findings`/`respond` reject paths resolving outside it.

**Env gap mitigation:** `worktree.copy_files` (default `[".env"]`) and `worktree.setup_command` (`uv sync`) run after creation. On first run, also run the test command against the **base commit** — if the base is red, report that loudly rather than attributing it to the diff.

**Copied files must never enter a commit.** The agent commits inside the worktree, so an un-ignored `.env` copied in could be swept up by `git add -A`, cherry-picked onto the branch at merge-back, and pushed. Git's `info/exclude` lives in the common dir and is shared across worktrees, so there is no clean per-worktree exclude to lean on. Two independent guards instead:

1. **Preflight refusal.** Before copying, run `git check-ignore -q <path>` for every `copy_files` entry. Not ignored → **refuse to copy that file**, exit 3, and tell the user to add it to `.gitignore` first. Never copy a secret-bearing file that git would happily track. (This is also a free lint on the user's repo hygiene.)
2. **Commit-content invariant.** Fix-commit verification (`respond --commit`) and merge-back both reject any commit whose changed-file set intersects `copy_files`. Enforced in code, tested directly, and independent of guard 1 — so a `.gitignore` edited mid-run cannot open the hole.

Copies are made with `shutil.copy2` and mode `0600`, recorded in `run.json` as `copied_files[]`, and die with the worktree on cleanup. They are never read, logged, echoed into an envelope, or included in `context` output — `copy_files` paths are added to the redaction set for stage logs.

`gc` reconciles three sources (run dirs, `git worktree list --porcelain`, `ap/*` branches); anything holding unmerged fix commits is reported, never auto-deleted without `--force`.

### Merge-back (cherry-pick, strict)

1. Preflight: branch tip == `run.head_sha` and paths touched by fix commits have no local modifications, else exit 3. Unrelated tracked edits and untracked files are preserved.
2. Record `pre_sha`. `git cherry-pick <fix_commits...>` in order.
3. **On conflict:** immediately `--abort`, verify `HEAD == pre_sha` and the complete pre-existing working-tree status is restored. State → `MERGEBACK_CONFLICT`, exit 4, persist and emit the resolution block (worktree path, fix commit SHAs — kept, conflicting files, literal commands). **No `-X ours/theirs`, no rerere, no automatic resolution, ever.** `mergeback` becomes legal again after human resolution; exact verified trees are attested without repeating prior stages.
4. **On success — tree-equivalence attestation:** compare `HEAD^{tree}` on the local branch vs the worktree branch tip tree. Equal ⇒ verified content is byte-identical, green transfers, write ledger for the post-cherry-pick tip. Not equal ⇒ re-verify; do not transfer green. *This is the single mechanism reconciling "ledger keyed on exact SHA" with "cherry-pick changes the SHA."*

### Lint / test

Command resolution: `--command` flag → `commands.<name>` config → **detect path** (exit 2, `data.mode="needs_command"`, candidates from `pyproject.toml`, `package.json` scripts, `Makefile`, `justfile`, `.github/workflows/*`). Agent picks and re-invokes with `--command ... --record`.

Pass/fail is **exit code only** — never parse stdout for "0 errors". Run as `bash -lc` with `cwd=worktree`, `timeout_seconds` (600) killing the process group. Committed Node pins are explicitly activated for supported version managers before setup and stages. Full output goes to `logs/<stage>.txt`; the envelope carries head 50 + tail 200 lines with a `truncated` flag. `stage.max_attempts` (5) stops infinite agent fix loops.

### Push / PR / gate

Provider detected from the push remote URL (SSH + HTTPS forms, host-aware so GHE works). **v1 is GitHub-only via the `gh` CLI, not the API** — `gh` owns auth, and "no credential handling in our code" is a design invariant. No token reading, no keyring, no `GITHUB_TOKEN` plumbing. Missing/unauthenticated `gh` → exit 4 with a prefilled compare URL.

`gate` mints a token and prints a summary block (remote, refspec, commit list, PR title); `push`/`pr` require `--confirm <token>`.

Be honest in the README: the token is **not a security boundary** — the agent can read it from `status`. It is deliberate ceremony that makes accidental pushes impossible and makes an unconfirmed push a visible protocol violation. `gate.mode = "manual"` refuses to proceed at all so a human must type the command; default is `token`.

### Ledger and hook

`ledger.json`: `{schema_version, entries: {<sha>: LedgerEntry}}`, pruned to 100. `LedgerEntry` carries `sha, tree_sha, branch, base_ref, merge_base_sha, run_id, green_at, stages{review,docs,lint,test}, findings_summary{}`. Written once per run for the final local tip. (`tree_sha` is unused in v1 but present so a v2 rebase-tolerant predicate is a one-line change.)

`.git/hooks/pre-push` is ~5 lines of sh calling `agentic-preflight hook-check`, reading the stdin protocol `<local_ref> <local_sha> <remote_ref> <remote_sha>`. Per line: all-zeros (deletion) → allow; `local_sha` green in ledger → allow; else block. Force-push (non-zero `remote_sha` not an ancestor) blocks regardless unless `hook.allow_force_push`.

Block message → **stderr**, written for an agent to read, and it names `/agentic-preflight` so it functions as a skill trigger that loops the agent back in:

```
agentic-preflight: push blocked.
  commit: abc1234 (no green run recorded for this exact SHA)
  reason: ledger has 9f2c1de; you amended or added a commit since
  fix:    run /agentic-preflight
  bypass: git push --no-verify   (documented escape hatch)
```

Constraints: <50ms, no network, no mutation, reads `ledger.json` only. **If `agentic-preflight` isn't on PATH the hook allows and warns** — a broken tool must not brick the repo.

### SKILL.md

Short front matter + ~150 line body; details deferred to `reference/`. Section order matters (early tokens get followed):

1. **Non-negotiables** — you think, the CLI holds state; parse JSON and obey `next`; never invent finding IDs; never `--no-verify`; never push or open a PR without asking the user in plain language.
2. **The loop** — a literal pseudo-transcript of the happy path with real commands and abbreviated JSON. *Agents imitate examples far more reliably than they follow rules.*
3. **How to review** — severity rubric with one example each; `auto_fix` (mechanical, locally verifiable) vs `ask_user` (behavioral/API/product judgment) vs `no_op`.
4. **How to check docs** — the obligation test ("would a reader following the docs now be wrong?"), pointer to `reference/docs-rubric.md`, and the explicit note that zero findings is a normal outcome.
5. **Findings schema** — filled example; the "no `id`, no `stage`" rule called out.
6. **Exit codes** + universal recovery rule: *any exit 3 → run `status` → obey `next`*.
7. **Failure playbooks** — merge-back conflict (paste block, stop, do not resolve), stage red after max attempts, stale head.
8. **Escalation etiquette** — what to show the user at the gate, verbatim.

Anti-skip is layered: state guards make skipping impossible, `next` makes the right move obvious, the hook catches bypassing the skill entirely.

### Config

`.agentic-preflight.toml` (repo root, committed) over `~/.config/agentic-preflight/config.toml`. Unknown keys error, naming the key. Sections:

```toml
[general] base_ref
[commands] lint, test
[stage] timeout_seconds, max_attempts
[review] blocking_severities, max_findings, require_fix_commits
[docs] paths, require_changelog, blocking_severities, enabled
[worktree] ttl_hours, root, copy_files, setup_command
[runtime] manager, strict
[gate] mode
[hook] enabled, allow_force_push
[publish] provider, draft_pr, pr_title
```

`[docs] enabled = false` exists for repos with no meaningful doc surface — the stage is skipped as a legal transition, not silently passed.

## Build order

- **M0 — walking skeleton.** `start → context → submit-findings → verify → status` for the review stage only, with real worktree, real diff, real persistence, real envelope contract. No docs/lint/test, merge-back, push, or hook. *Ship the state machine and JSON contract first — everything hangs off them and both are what you'd most regret getting wrong.*
- **M1 — resolution loop.** `respond` + fix-commit verification, `logs`, `events`, `abort`, `gc`, orphan sweep.
- **M2 — docs stage.** Second instance of the review sub-machine: doc-surface inventory, docs allowlist, `require_changelog` check, `docs-rubric.md`. *Doing this right after M1 proves the stage abstraction generalizes before lint/test harden it into a single-use shape.*
- **M3 — shell stages.** `stage run lint|test`, configured + detect paths, attempt limits, log capture, baseline check.
- **M4 — merge-back.** Cherry-pick, clean-abort contract, tree-equivalence attestation, ledger writes.
- **M5 — hook.** `init`, hook install, `hook-check`, block wording, force-push guard.
- **M6 — publish.** `gate`, `push`, provider detection, `pr` via `gh`, `--dry-run`.
- **M7 — SKILL.md + reference docs**, written last *deliberately*: the finished CLI is the spec, which keeps the docs from lying.

M0 and M4 are the substantial milestones; M2, M5, and M6 are hours each.

**Step 0:** write the validated design to `docs/plans/2026-07-20-agentic-preflight-design.md` in the new repo and commit it (deferred from brainstorming, which cannot write files in plan mode).

## Verification

- **Git fixtures use real git, not mocks** — the product *is* git semantics. `tmp_repo` (deterministic commits, fixed `GIT_*` env) and `bare_remote` so push/hook paths run for real.
- **Scripted agent driver.** `ScriptedAgent` runs `(argv, expected_exit)` lists through both `CliRunner` and `subprocess` (the hook must be a real subprocess). Happy path + each failure and recovery branch = one script each. Golden-file the envelopes with SHAs/timestamps normalized.
- **Property test over the machine** (Hypothesis, random command sequences). Invariants: never an unhandled exception; `run.json` always parses; **`PUSHED` unreachable without a matching green ledger entry covering all enabled stages**; `seq` monotonic; finding IDs never reused or renumbered **across stage boundaries**.
- **Cherry-pick conflict test — the single most important test.** Construct a guaranteed conflict; assert exit 4, `HEAD == pre_sha`, clean tree, fix commits and worktree still present, `.git/CHERRY_PICK_HEAD` gone.
- **Docs stage tests:** a code-only diff yields zero docs findings and reaches `DOCS_GREEN`; a docs finding against a source file is rejected; `require_changelog = true` with an untouched changelog injects the code-owned blocking finding; `[docs] enabled = false` transitions straight to lint.
- **Copied-file containment tests** (secret-leak class, treat as blocking): a gitignored `.env` is copied and `git status --porcelain` in the worktree stays empty; a **non**-ignored `.env` is refused with exit 3 and is not copied; a fix commit that adds a `copy_files` path is rejected by `respond` *and* by merge-back independently; `.env` contents never appear in `context` output, stage logs, or any envelope; worktree cleanup removes the copies.
- **Node dependency isolation tests:** pnpm uses a frozen install backed by its content-addressable store; npm always runs `npm ci` in the disposable worktree and never symlinks the main checkout's `node_modules`.
- **Crash atomicity:** monkeypatch `os.replace` to raise → `run.json` still valid, `gc` recovers.
- **`gh` stub** on `PATH` recording argv; assert we never pass a token and never hit the network. Same trick for lint/test via `--command "exit 1"`.
- **Hook tests** run a real `git push` against the bare remote, including: green run → `git commit --amend` → push blocked.
- **No-LLM invariant:** import-graph assertion that no module imports `anthropic`/`openai`/`httpx`/`requests`.
- Manual `evals/` folder with 3–4 scripted agent scenarios, run by hand before releases.

## Known risks

1. **Worktree environment gap** (highest). Fresh worktree lacks `.venv`/`node_modules`/`.env`; tests fail for reasons unrelated to the diff and the agent fixes phantom problems. Mitigated by isolated lockfile-aware Node setup, `setup_command` + `copy_files` + base-commit baseline check. If this proves insufficient in practice, the escape hatch is a non-default `general.worktree = false` in-place mode — explicitly deferred, not in v1.
2. **SHA identity churn.** People amend, rebase, and squash constantly; each invalidates the green and forces a full re-run, which may push users to `--no-verify` permanently. Tree-equivalence covers the cherry-pick case; accepting a `tree_sha` match (rebase with no content change) is the planned v2 relief, which is why `LedgerEntry` carries it now.
3. **The gate is advisory.** The agent can read its own token; `--no-verify` defeats the hook. No cryptographic answer exists here — the README must state plainly that this guards against mistakes, not against a careless or misaligned agent. `gate.mode = "manual"` is the honest fix for those who need one.
4. **Docs-stage noise.** A docs check with a loose rubric generates low-value findings on every run ("consider documenting this helper"), and stage fatigue is what gets a gate disabled. Mitigations: docs findings default to non-blocking below `high`, zero findings is explicitly framed as the normal outcome in SKILL.md, and the rubric is written around a single obligation test — *would a reader following the current docs now be wrong?* — rather than an aspirational completeness standard.

Smaller: parallel invocations racing on `run.json` (flock + `--expect-seq` should hold — test it), and large diffs blowing agent context (`context` must enforce `diff.max_bytes` and degrade to chunked per-file review rather than silently truncating).
