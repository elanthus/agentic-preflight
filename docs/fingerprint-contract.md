# Fingerprint contract for reusable preflight evidence

After a history-only rebase or restack, `start` discovers applicable local evidence.
It returns the next required stage.
`status` resumes after an interruption.
Review and docs evidence can transfer when their current fingerprints match.
Lint and test evidence is never reused in a new run. This includes commits with identical trees.

## Applicability and audit identity

Fingerprint version 2 describes content inputs.
The original commit, base, branch, configuration digest, run, worktree ownership, execution time, findings, and coverage stay separate audit identity.
A new commit always needs its own note.

Matching patches alone are not sufficient.
Every stage binds both the base tree and the head tree.
Upstream content changes invalidate review even with an unchanged patch.
Retargeting checks the content of the new base.
The current risk, executor, and hosted approval policy still apply.

| Disposition | Meaning |
| --- | --- |
| `reusable` | All inputs that the supported contract requires match. |
| `invalid` | Known inputs changed, or stage reuse is disabled. `reasons` identifies the categories. |
| `unknown` | Provenance, inputs, or configuration dependencies cannot be established. Run the stage again. |

`data.applicability` describes candidate evidence. It does not describe the outcome of a stage that ran fresh.
Decisions and later-stage candidates persist on the run.
A repair or a changed input triggers another check before import or merge-back.
Unresolved actionable or blocking findings cannot advance through reuse.

## Review and documentation inputs

Review binds these inputs:

- The base and head trees
- The included diff bytes and the exclusions
- The delivered grounding, including conventions and prior findings
- The intent
- The effective executor
- The `[general]`, `[review]`, `[policy]`, `[context]`, `[diff]`, and `[stage]` sections

The `[stage]` section governs command-review timeouts and retries.

Intent and shell-command digests hash the exact UTF-8 text bytes.
They do not normalize whitespace or Unicode.
Configuration and documentation-surface digests hash JSON with sorted keys and compact separators.

Docs binds the base and head trees, the intent, the grounding, the documentation content, and the `[general]`, `[docs]`, `[diff]`, `[context]`, and `[policy]` sections.
Its digest includes every inventory entry's complete bytes, path, existence, size, and whether the diff touches it.
A same-length edit to an ignored document invalidates the evidence.
Changed docs between context and submission require fresh context.

An unrelated user-level test-command change leaves the review configuration unchanged.
A committed configuration edit changes the head tree. The edit itself must be reviewed.
Unknown configuration sections prevent reuse even when their values match.

Review derivation recomputes both Git manifests.
It validates the original coverage.
It compares every original and current unit and exclusion.
Only then can it replace the current coverage's commit and manifest identity.
The original dispositions are retained.

## Shell execution evidence

Lint and test results stay bound to their original execution and run.
New runs execute applicable shell stages again.
`shell_reuse_disabled` explains their applicability decision.
The verifier rejects refreshed shell evidence. This includes derivations between identical trees.

Same-run tree-equivalent merge-back keeps its existing publication behavior.
Validated results can attest the tree-equivalent cherry-picked source commit without refreshed evidence.
This does not enable reuse in a new run.

The retired `[reuse]` declarations no longer trigger file, toolchain, environment, or platform capture.
Original same-commit notes stay readable, including their old declarations and recorded input digests.
Fingerprint version 2 and the version 7 wire shape are unchanged for compatibility.
The typed models and their remaining safety bindings stay supported.
A local `stage run --command` override is valid fresh execution evidence. It is bound to its resolved command.

## Provenance, ownership, and recovery

Discovery uses finalized local execution records from the same source-worktree identity and branch.
Records of other linked worktrees are not candidates.
All three modes use the same classification and import path.
A fresh clone that has only portable notes needs a local run.
The tool does not reconstruct local execution or ownership records from portable notes.

The tool checks origin and configuration snapshot digests at three points.
These are evidence admission for reuse, evidence archival, and attestation verification.
A run-record load performs structural validation. It does not rehash the stage origins.
Corrupt active evidence is reported as `stage_evidence_invalid` before merge-back changes the source branch.
`status` stays available.
It recommends that you inspect the record, abort without `--force`, and then start fresh.
An ordinary abort still protects unmerged work.

An attestation embeds one original execution per stage, its content digest, and the current fingerprint.
Derivation adds a refresh time and `equivalent_inputs`.
It keeps the original timestamps, process hashes, findings, fixes, and coverage.
Provenance is flattened to one origin. Nested chains and unsupported fields or versions are rejected.
The consumer recomputes the available Git, manifest, configuration, executor, and applicability bindings.
It checks the finding dispositions.
It cannot measure the past local environment. It cannot authenticate an unsigned note.

Original Git commits are retained under `refs/agentic-preflight/evidence/<SHA>`.
Publication sends those refs first. Then it pushes the branch and the notes atomically.
If the publication of an original fails, the branch publication does not start.
An interrupted publication can leave harmless retained evidence refs on the remote.
Hosted consumers fetch missing originals from the contributor's evidence refs before verification.
A fetch of the notes blob alone cannot retrieve the commits that its JSON names.
These refs survive normal run and worktree cleanup.
They must stay while any published note depends on them.
Keep the local publisher, the hook, and the protected verifier on the same release.
Missing originals cannot be reconstructed from hashes.

The clean-checkout, synchronization, merge-back, publication authorization, and atomic branch and notes push rules still apply.
A refresh authorizes no force-push, merge, or cleanup.
