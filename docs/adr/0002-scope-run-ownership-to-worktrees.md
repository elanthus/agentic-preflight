# ADR 0002: Scope active run ownership to source worktrees

- Status: Accepted
- Date: 2026-08-30
- Amended: 2026-10-20. Rewrote the text in ASD-STE100 style. The decisions are unchanged.

## Context

Run documents and Git refs belong to a clone. Agentic Preflight stores them under `GIT_COMMON_DIR`.
The original store also kept one clone-wide `current` pointer.
That pointer made command routing unambiguous.
It also permitted only one active run across all linked worktrees.
Thus, an interrupted run could block unrelated agents that prepared other pull requests.

Most run state is not shared.
Each run has its own document, findings, events, logs, and validation worktree.
The shared resources are narrower: the fetched base ref, the Git-notes attestation ref, and the single cached validation worktree in `reusable` mode.

Elapsed time cannot prove that a run was abandoned.
A run can wait indefinitely for a user decision or for publication authorization.
An isolated run can contain fix commits that exist nowhere else.

## Decision

Keep durable run history in the clone-wide store.
Scope active ownership to the source worktree's private Git directory.
Hash that directory's absolute path to obtain a filesystem-safe owner ID.
Store one atomic active-run pointer per owner.
Register an isolated validation worktree as an alias of the same run.

Commands resolve the invoking worktree's pointer by default.
The root `--run RUN_ID` option selects a stored run explicitly.
When its recorded source checkout still exists, orchestration uses that checkout, not the unrelated caller.
`status --all` gives a clone-wide inventory without making every command clone-wide.

Treat `start` as idempotent when the head, base, intent, and effective configuration match.
When the source head moved, transition the previous run to `ORPHANED`, release its owner aliases, preserve its evidence and work, and start fresh.
Require `start --replace` when the head is unchanged but the requested intent or configuration differs.
Never infer abandonment from age.

Use locks at the resource that they protect:

- A per-owner start lock prevents two starts from claiming one source worktree.
- A per-run operation lock serializes complete mutating commands. It prevents replacement of a run while one of its commands executes.
- A synchronization lock protects the shared fetched base ref.
- A notes lock protects the fetch, merge, write, and publication of attestations.
- `reusable` mode keeps its single-worktree lease. `in_place` and `strict` runs in other source worktrees continue independently.

The obsolete clone-wide `current` pointer is not part of the supported store format. It is ignored.
If a stray pointer exists, leave it byte-for-byte unchanged.
Its referenced run record stays in durable history.
Garbage collection keeps unsupported earlier-release records. It does not translate or delete them.

## Consequences

Agents in separate linked worktrees can have active runs at the same time.
When one run finishes, aborts, or is superseded, only that run's aliases are cleared.
Base synchronization and notes publication can still wait while their specific clone-wide operations are active.
Reusable-mode runs stay serial because they share one cached checkout.

Orphaning and deletion are separate.
`start` changes only ownership and state.
`gc` can later reclaim terminal validation worktrees that contain no unmerged fixes.
It keeps fix-bearing work unless `--force` is explicit.
Durable run directories and audit logs stay available.

Owner IDs are opaque hashes by design.
User-facing recovery therefore reports source and validation paths beside the IDs.
It supplies `status --all` and `--run` for discovery.

## Alternatives

- **Keep one clone-wide pointer.** Rejected. Unrelated PR work and abandoned runs stay mutually blocking.
- **Key active runs by branch.** Rejected. Branches move and can be renamed. Isolated validation uses temporary `ap/<run-id>` branches.
- **Require a run ID on every command.** Rejected. It makes the common one-agent, one-worktree workflow noisy. It makes copied recovery commands easier to misuse.
- **Expire leases after a timeout.** Rejected. Inactivity does not distinguish an abandoned run from one that waits legitimately. Automatic expiry could strand or destroy unique repair work.
