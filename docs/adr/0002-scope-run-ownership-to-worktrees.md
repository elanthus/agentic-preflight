# ADR 0002: Scope active run ownership to source worktrees

- Status: Accepted
- Date: 2026-08-30

## Context

Run documents and Git refs belong to a clone, so Agentic Preflight stores them under
`GIT_COMMON_DIR`. The original store also kept one clone-wide `current` pointer. That
pointer made command routing unambiguous, but it allowed only one active gate across all
linked worktrees. An interrupted run could therefore block unrelated agents preparing
other pull requests.

Most gate state is not shared. Each run has its own document, findings, events, logs, and
validation checkout. The resources that are shared are narrower: the fetched base ref,
the Git-notes attestation ref, and the single cached runner in `reusable` mode.

Elapsed time cannot prove that a run was abandoned. A run may wait indefinitely for a
user decision or publication authorization, and an isolated run may contain fix commits
that exist nowhere else.

## Decision

Keep durable run history in the clone-wide store, but scope active ownership to the
source worktree's private Git directory. Hash that directory's absolute path to obtain a
filesystem-safe owner ID. Store one atomic active-run pointer per owner and register an
isolated validation checkout as an alias of the same run.

Commands resolve the invoking worktree's pointer by default. The root `--run RUN_ID`
option selects a stored run explicitly; when its recorded source checkout still exists,
orchestration uses that checkout rather than the unrelated caller. `status --all`
provides clone-wide inventory without making every command clone-wide.

Treat `start` as idempotent when head, base, intent, and effective configuration match.
When the source head moved, transition the previous run to `ORPHANED`, release its owner
aliases, preserve its evidence and work, and start fresh. Require `start --replace` when
the head is unchanged but the requested intent or configuration differs. Never infer
abandonment from age.

Use locks at the resource they protect:

- a per-owner start lock prevents two starts from claiming one source worktree;
- a per-run operation lock serializes complete mutating commands and prevents replacing
  a run while one of its commands is executing;
- a synchronization lock protects the shared fetched base ref;
- a notes lock protects fetch, merge, write, and publication of attestations; and
- `reusable` mode retains its single-runner lease, while `in_place` and `strict` runs in
  other source worktrees continue independently.

Migrate the legacy clone-wide `current` pointer to the first invoking worktree on access.
Keep the old run record even when a newer worktree pointer already exists.

## Consequences

Agents in separate linked worktrees can run gates concurrently. Finishing, aborting, or
superseding one run clears only that run's aliases. Base synchronization and notes
publication may still wait while their specific clone-wide operations are active, and
reusable-mode runs remain serial because they share one cached checkout.

Orphaning and deletion are separate. `start` only changes ownership and state. `gc` may
later reclaim terminal validation worktrees that contain no unmerged fixes; it retains
fix-bearing work unless `--force` is explicit. Durable run directories and audit logs
remain available.

Owner IDs are intentionally opaque hashes. User-facing recovery therefore reports source
and validation paths alongside IDs and supplies `status --all` and `--run` for discovery.

## Alternatives

- **Keep one clone-wide pointer.** Rejected because unrelated PR work and abandoned runs
  remain mutually blocking.
- **Key active runs by branch.** Rejected because branches move and can be renamed, while
  isolated validation uses temporary `ap/<run-id>` branches.
- **Require a run ID on every command.** Rejected because it makes the common one-agent,
  one-worktree workflow noisy and makes copied recovery commands easier to misuse.
- **Expire leases after a timeout.** Rejected because inactivity does not distinguish an
  abandoned run from one waiting legitimately, and automatic expiry could strand or
  destroy unique repair work.
