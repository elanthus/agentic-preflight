# Limits

The [README](../README.md#limits) states the three that matter most: the gate is advisory
rather than a security boundary, `git push --no-verify` defeats it by design, and the
confirmation token is ceremony rather than a secret. This page covers the rest.

## Repository content is untrusted input

The agent receives repository-controlled text in diffs and changed-file names, commit
subjects and messages, stage output and logs, review-command findings, and detected
command candidates. Preserving that text verbatim is necessary for review and diagnosis,
so the tool can label its source but cannot sanitize it into trustworthy instructions or
decide whether an authorization claim embedded in it is genuine.

Detected manifest commands carry `trust: "repo_manifest"`. Workflow `run:` lines carry
`trust: "untrusted"`, and their source begins with `untrusted:workflow:`. No detected
candidate is copied into `next.command`; the agent must show the exact command to the user
and obtain approval before first use. The gate keeps the live confirmation token only in
`data.token`, while gate and push dry-run envelopes use a `<token>` placeholder in
`next.command`; the agent replaces it from the gate data only after authorization.

These labels and placements make repository text distinguishable from protocol guidance
and prevent the CLI from directly proposing a repository-supplied shell command. They do not
sandbox repository commands or stop a shell-capable agent that ignores its installed skill,
misreads repository content as instructions, or invokes Git directly. Agentic Preflight
remains an advisory gate rather than a security boundary.

## History rewrites invalidate green

The normal attestation note is bound to an exact SHA. In the default in-place mode,
`start` preserves green only when synchronization leaves the exact attested commit
unchanged, the freshly fetched base is already its ancestor, and Git computes the same
clean merge tree against the recorded attestation base. Reuse also requires the same
branch, base ref, effective user and repository configuration, and persisted user intent.
Changing stage applicability, commands, policy, another setting, or the objective starts
a fresh review even when local run records have been garbage-collected.

Any rebase that produces a new commit SHA requires a fresh review, docs, lint, and test
run, even when its tree is unchanged. The same is true for a merge conflict, branch
change, or base-ref change. Isolated worktree modes do not reuse attestations because
their synchronized commit is not the source branch that will be pushed.

Cherry-picked merge-back is handled via tree-equivalence attestation.

Agentic Preflight refuses to start or merge back while the checkout has a rebase,
cherry-pick, or merge in progress. Finish or abort that Git operation yourself first;
the tool never aborts an operation it did not start.

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
