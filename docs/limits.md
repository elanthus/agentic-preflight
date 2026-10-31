# Limits

The [README](../README.md#limits) introduces the main limits.
Agentic Preflight is advisory. It is not a security boundary.
`git push --no-verify` bypasses it by design.
The confirmation token prevents accidental pushes. It is not a secret.
This page covers the other limits.

## Repository content is untrusted input

The agent receives repository-controlled text in these places:

- Diffs and changed-file names
- Commit subjects and messages
- Stage output and logs
- Review-command findings
- Detected command candidates
- The `excerpt`, `content`, and `title` fields in `data.grounding`

Review and diagnosis need that text verbatim.
The tool can label its source. It cannot sanitize the text into trustworthy instructions.
It cannot decide whether an authorization claim in that text is genuine.

Detected commands include their source.
The CLI never copies a detected candidate into `next.command`.
The agent must show the exact command to the user and obtain approval before the first use.
The gate keeps the live confirmation token only in `data.token`.
Gate and push dry-run envelopes use a `<token>` placeholder in `next.command`.
The agent replaces the placeholder from the gate data only after authorization.

These labels and placements separate repository text from protocol guidance.
They prevent the CLI from proposing a repository-supplied shell command directly.
They do not sandbox repository commands.
They do not stop a shell-capable agent that ignores its installed skill, reads repository content as instructions, or invokes Git directly.
Agentic Preflight stays advisory. It is not a security boundary.

## History rewrites require evidence refresh

The attestation note is bound to an exact SHA.
A new `start`, including one on the same unchanged commit, classifies each stage again through its recorded fingerprint.
Review and docs require equivalent content, intent, grounding, and relevant policy.
Applicable shell stages run again in each new run. Their results never transfer to a new run.
Unknown or changed review or docs inputs require a fresh stage. The CLI does not import a green result for them.
A change to stage applicability, commands, policy, another setting, or the objective starts a fresh review.
This applies even when the local run records have been garbage-collected.

With sufficient local execution records, `start` can reuse equivalent evidence after a rebase in all three worktree modes.
It compares base and head trees and the stage-specific context and policy.
Changed upstream content invalidates review even if the patch is unchanged.
Other source-worktree identities and other branches are not candidates.

Tree-equivalence attestation handles cherry-picked merge-back.

Shell input declarations are retired.
Lint and test evidence stays valid for its original execution. It is never reused in a new run, even for identical trees.
Same-run tree-equivalent merge-back publication is still supported.
See the [fingerprint contract](fingerprint-contract.md).

Agentic Preflight refuses to start or merge back while the checkout has a rebase, cherry-pick, or merge in progress.
Finish or abort that Git operation yourself first.
The tool never aborts an operation that it did not start.

## Isolated validation worktrees can differ from your environment

In-place mode uses the checkout's existing dependencies and ignored files.
An isolated validation worktree does not inherit the source checkout's `.venv`, `node_modules`, or `.env`.

- Configure `[worktree] setup_command` to install dependencies or prepare ignored build inputs.
  Agentic Preflight does not select a package manager. It does not install automatically.
- Configure `copy_files` for ignored files such as `.env`.

The setup command runs before review. It also runs in the scratch worktree that `--baseline` uses.
A nonzero exit is reported as a setup failure.

Use `--baseline` so that a pre-existing failure is reported and not attributed to your diff.

## What a green run does and does not prove

A green run proves what the stages reported:

- The configured in-harness or command executor accounted for every included review unit in a snapshot-bound diff manifest.
- The executed green commands exited zero against their recorded execution commits.
- The run recorded its judgment calls.

Command review also carries its command, its zero exit code, and its redacted output digest.
Derived stages preserve those original executions. They record why their evidence applies to the new exact commit.
Excluded files stay explicitly outside that coverage.
A skipped stage records a reason, not a successful execution.

A green run does not prove that the review was good.
It does not prove that the agent understood every unit that it marked clean.
The same diff reviewed twice can yield different findings.
Treat the attestation as an audit trail.
It does not replace CI or a human reviewer.
It is not a signature. Anyone with permission to update the notes ref can replace it.

## Attestations are not signatures

Anyone with permission to update `refs/notes/agentic-preflight` can replace an attestation.
Protect the notes ref on the remote if the forge supports ref-level policy.
Treat `verify` as enforcement that a structurally valid, commit-bound record exists.
It is not proof that the agent's review judgment was good.

`verify` does not take its merge base or its policy from the note.
The trusted base is the `--base` revision when the caller passes one, as both hosted workflows do.
Otherwise it is the note's `base_ref`, resolved to `refs/remotes/origin/<branch>` or `refs/heads/<branch>`.
The note's `merge_base_sha` must be the only merge base of that base and the head.
Policy comes from the `.agentic-preflight.toml` committed at the trusted base.
The note's configuration snapshot records what ran. It does not decide what was required.

Thus, a branch cannot relax the policy that judges it.
It cannot skip docs that the base enables, or replace a configured lint, test, or review command.
It cannot exclude review files that the base includes, or avoid a command review that the base requires.
A run can do more than the base requires. For example, it can run docs that the base disables.

These checks narrow what a forged note can claim. They do not authenticate the note.
A writer of the notes ref can still record stage results that never ran, if those results satisfy the base policy.
Without `--base`, the note also chooses which existing branch it names as its base.

## A branch that changes the policy fails verification until it merges

A branch can change a configured command, turn off docs, or widen `[diff] exclude` in `.agentic-preflight.toml`.
Its run then attests results that the protected base policy rejects.
The local hook blocks the push, and the hosted attestation check fails.
A human pushes such a change and merges it after review.
Later branches are verified under the new policy.

Cryptographic unforgeability requires a signing authority whose key and execution path the evaluated agent cannot reach.
An agent-accessible key around the current note would add steps without a security boundary.
[Issue #25](https://github.com/elanthus/agentic-preflight/issues/25) proposed a signed ledger.
It was closed as not planned on 2026-09-07. Attestations stay unsigned.
