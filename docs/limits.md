# Limits

The [README](../README.md#limits) states the three that matter most: the gate is advisory
rather than a security boundary, `git push --no-verify` defeats it by design, and the
confirmation token is ceremony rather than a secret. This page covers the rest.

## Most history rewrites invalidate green

The normal attestation note is bound to an exact SHA. In the default in-place mode,
`start` can preserve green across a history-only rebase, but only when the complete Git
tree and effective preflight configuration are identical and `git merge-tree` produces
the same clean result for the old and new commits against the freshly synchronized base.
The merge check matters because identical snapshots with different parents can merge
differently. The config check includes the resolved user and repository configuration,
so changing stage applicability, commands, policy, or another setting forces a fresh
run. Reuse also requires the portable attestation to carry the same persisted user-intent
digest; a new objective always starts a fresh review, even after local run records have
been garbage-collected.

Any content or effective-config change, merge conflict, different merge result, branch
change, or base-ref change forces a fresh review, docs, lint, and test run. Isolated
worktree modes do not reuse attestations because their synchronized commit is not the
source branch that will be pushed.

Cherry-picked merge-back is handled via tree-equivalence attestation.

## Isolated worktrees can differ from your environment

In-place mode deliberately uses the checkout's existing dependencies and ignored files. An
isolated runner does not inherit the source checkout's `.venv`, `node_modules`, or `.env`.

- Configure `[worktree] setup_command` to install dependencies or prepare ignored build
  inputs. Agentic Preflight does not select a package manager or install automatically.
- Configure `copy_files` for ignored files such as `.env`.

The setup command runs before review and in the scratch worktree used by `--baseline`.
A nonzero exit is reported as a setup failure.

Use `--baseline` so a pre-existing failure is reported rather than blamed on your diff.

## What a green run does and does not prove

It proves what the gate reported: that the configured in-harness or command executor
accounted for every included review unit in a snapshot-bound diff manifest, that the
configured commands exited zero against that exact SHA, and which judgment calls were
recorded along the way. Command review additionally carries its command, zero exit code,
and redacted output digest. Excluded files remain explicitly outside that coverage.

It does not prove the review was good or that the agent understood every unit it marked
clean. The same diff reviewed twice can yield different findings. Treat the Git note as
an audit trail, and note that it substitutes for neither CI nor a human reviewer. It is
not a signature: anyone allowed to update the notes ref can replace it.

## Attestations are not signatures

Anyone allowed to update `refs/notes/agentic-preflight` can replace an attestation.
Protect the notes ref on the remote if the forge supports ref-level policy, and treat
`verify` as enforcement that a structurally valid, commit-bound record exists—not proof
that the agent's review judgment was good.

Cryptographic unforgeability requires a signing authority whose key and execution path
the evaluated agent cannot reach. Putting an agent-accessible key around the current
note would add ceremony, not a security boundary. The threat model, key lifecycle,
replay protection, and transparency-ledger design are tracked in
[issue #25](https://github.com/elanthus/agentic-preflight/issues/25).
