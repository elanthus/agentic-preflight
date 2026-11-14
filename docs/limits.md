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

### Committed symlinks on the documentation surface

The documentation inventory follows a committed symlink that matches a documentation pattern.
The symlink is on the surface when its target is a file, even a file outside the repository.
Its `size` and the docs fingerprint come from the target's bytes.
A docs finding can name the symlink, and an agent that edits that path writes to the target.
Grounding, by contrast, reads only regular files from the head commit and skips symlinks.
Review a committed symlink under a documentation path before you run the docs stage on it.

## SHA-256 repositories are not supported

Run records and attestation notes hold only SHA-1 object IDs.
In a repository that uses the SHA-256 object format, a run fails before it is green, and no commit can have a valid attestation.
The pre-push hook blocks a branch push there with a message that says SHA-256 repositories are not supported.
See [COMPATIBILITY.md](../COMPATIBILITY.md).

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

## What a published note reveals

`push` publishes the attestation notes of the commits it pushes to the remote with the branch.
[Portable attestations](attestations-and-ci.md) lists every field.
The note includes the text of every finding and response note, after copied-file values are redacted, and the commands that ran.
It includes the configuration from the repository file, which the repository already publishes.
It leaves out the sections of the user configuration file, except `[diff]`.
It records `[redacted user-level command]` in place of a command that depends on a user-level `[review]` or `[commands]` section.

Redaction narrows what the note can reveal. It is not a secret scanner:

- Finding text and response notes are redacted only for exact values from `[worktree] copy_files` files.
  A reviewer that paraphrases, encodes, or splits a value, or that quotes another file, can still place it in the note.
- Short copied values are redacted too. A `.env` line such as `DEBUG=1` replaces every `1` in finding text with `[redacted]`.
  The note is public, so the tool redacts too much rather than publish a short credential.
  Keep unrelated short values out of `copy_files` files to keep findings readable.
- A command in the repository file and a `stage run --command` override when the repository file sets `[commands]`, or when neither file sets it, are published as written.
- The source worktree ID is an unsalted SHA-256 digest of the local path of the worktree's Git directory.
  A reader who guesses the path, for example from a user name and a common layout, can confirm the guess.
- The local run record keeps the full user configuration, because the run executes it.

Notes that earlier releases wrote keep whatever they contained.
A run that an earlier release started cannot publish: `mergeback`, `gate`, and `push` refuse it because the run did not record which sections came from the user file. Abort it and start a fresh run.
Other notes in the local notes ref, such as one an earlier release wrote for a run you later aborted, are published only when `push` publishes their commit, or when someone pushes the whole ref by hand, as the next section describes. Remove them before then if they may hold user-level configuration.
Evidence reuse refuses evidence from a record without user-level sections, such as one that an earlier release wrote, unless its configuration snapshot matches what the current run publishes.
Rewriting a published note does not remove it from clones or forks that already fetched it.

## Published notes are visible to everyone who can fetch them

A note holds the branch name, the finding titles and details, the file paths, and the published configuration snapshot described above.
`push` publishes the notes of the commits it pushes, and the gate lists them in `published_notes`.
Notes on commits that were never pushed stay in the local `refs/notes/agentic-preflight`.
The scope is per commit, not per branch. A pushed commit's note is published even when a run on another branch wrote it, for example an aborted branch that pointed at the same commit, and that note names the other branch.
This scope covers `push` and the manual gate command only.
A hand-run `git push origin refs/notes/agentic-preflight` sends every local note, including those of unpushed commits.
Releases before this scoping pushed the whole local notes ref, so the remote may already hold such notes.
Removing them requires rewriting the remote notes ref.

## Attestations are not signatures

Anyone with permission to update `refs/notes/agentic-preflight` can replace an attestation.
Protect the notes ref on the remote if the forge supports ref-level policy.
Treat `verify` as enforcement that a structurally valid, commit-bound record exists.
It is not proof that the agent's review judgment was good.

`verify` does not take its merge base or its policy from the note.
The trusted base is the `--base` revision when the caller passes one, as both hosted workflows do.
Otherwise it is the note's `base_ref`, resolved to `refs/remotes/origin/<branch>` or `refs/heads/<branch>`.
A fully qualified `base_ref` must already be under one of those namespaces. A tag fails with `base_mismatch`.
The note's `merge_base_sha` must be the only merge base of that base and the head.
Policy comes from the `.agentic-preflight.toml` committed at the trusted base.
The note's configuration snapshot records the published part of what ran. It does not decide what was required.

Thus, with a trusted base, a branch cannot relax the policy that judges it.
It cannot skip docs that the base enables, or replace a configured lint, test, or review command.
It cannot exclude review files that the base includes, or avoid a command review that the base requires.
A run can do more than the base requires. For example, it can run docs that the base disables.

These checks narrow what a forged note can claim. They do not authenticate the note.
A writer of the notes ref can still record stage results that never ran, if those results satisfy the base policy.
Without `--base`, the note also chooses which existing branch it names as its base.
`push` and the pre-push hook verify without `--base`, so a forged note can name the pushed branch itself, or any branch it can create, and be judged by that branch's policy.
Only a caller that passes the protected base, as the hosted workflows do, closes that gap.

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

## Approval checks cannot tell a person from an agent

The hosted approval check counts a GitHub review or an Environment approval by the account that submitted it.
GitHub cannot tell whether a person or a program with their credentials submitted it.
An agent that runs with the approving account's `gh` login or token can satisfy the check itself.

`[approval] mode = "owner_review"` is the most exposed mode.
On their own pull request, the configured reviewer approves with a Comment review whose body is exactly `approved`.
An agent often runs with the owner's credentials and opens the pull request as the owner.
That agent can post the `approved` Comment review, and the check counts it as the owner's approval.
The only control is the instruction in the skill and in the repository's agent instructions that tells the agent never to submit it.
An `environment` approval by the same account has the same limit.

If the approval must come from a person the agent cannot impersonate, keep the approving account's credentials away from the agent:

- Use `mode = "peer_review"`. A person other than the pull-request author must approve.
- Use `mode = "environment"` with **Prevent self-review** enabled on the Environment. The account that triggered the run cannot approve it. When the agent pushes with the owner's credentials, the owner cannot approve either, so the Environment needs a required reviewer other than the account the agent uses.
- Run the agent under its own GitHub account, so that it opens pull requests as that account and never holds the reviewer's token. The reviewer then submits an **Approve** review on another author's pull request.

Each choice protects the approval only while the agent cannot reach the approving account's credentials.

## The Dependabot exemption trusts GitHub's event sender

This repository's hosted workflows waive owner review for verified Dependabot pull requests, and auto-merge a narrower class of them.
See [Dependabot and other bot-authored pull requests](attestations-and-ci.md#dependabot-and-other-bot-authored-pull-requests).
That exemption rests on two checks of GitHub-supplied data:

- Every non-merge commit is attributed to `dependabot[bot]`, committed by `web-flow`, and has a valid signature.
  This shows that GitHub created the commit. It does not show that Dependabot wrote its content.
- The event that set the current head was an `opened` or `synchronize` event whose sender is the `dependabot[bot]` Bot account (login, type, and account id `49699333`), and a `synchronize` event reports that exact head as the result of its push.
  This shows that Dependabot, not a person, placed the commit at the head.
  The pull request must also target the default branch.

The second check exists because a person with write access might obtain a GitHub-signed commit attributed to Dependabot, for example from a squash merge of a Dependabot pull request that carries their own commits, and push it to another Dependabot branch.
Whether GitHub attributes such a squash commit to Dependabot was not tested; the check does not depend on the answer.
A head pushed by a person, including a clean merge from **Update branch**, does not receive the exemption, and neither does a head whose actor the event cannot establish.
`reopened`, `ready_for_review`, and auto-merge events name whoever performed that action, and a branch can change while its pull request is closed, so those runs fail closed.
Such a pull request follows the ordinary policy: it needs a preflight attestation for its head and owner approval where the approval policy requires it.
Alternatively, ask Dependabot to rebase or recreate the pull request, which restores the exemption with a new Dependabot-set head.
A re-run replays the original event, so a review-triggered re-run keeps the original sender.

The result is bound to the base named by the event that produced it.
Retargeting a pull request does not re-run the workflow, because an `edited` event cannot establish who set the head and would fail every edit closed.
Requiring the default branch means a run judged against another base cannot be carried to the default branch by a retarget.
A pull request retargeted away from the default branch keeps its earlier result; protect other branches with their own rules if that matters.

The guarantee is therefore: GitHub reported that Dependabot set this head on a pull request into the default branch, and every commit since the base is either GitHub-signed bot work or a merge whose tree equals Git's clean merge of its parents, with the second parent in the protected base.
The event check covers only the account that set the current head. It does not establish who pushed an earlier clean base merge in the history below that head; such a merge adds no content beyond the base and the bot commits.
It is not a review of the content, and it trusts GitHub's attribution of both the commits and the event.
For an `opened` event, it also trusts that the branch still held Dependabot's push when Dependabot opened the pull request.
The exemption still applies to every file that Dependabot changes, including GitHub Actions workflows.
