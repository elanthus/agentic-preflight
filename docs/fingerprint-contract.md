# Fingerprint contract for reusable preflight evidence

After a history-only rebase or restack, `start` discovers applicable local evidence
and returns the next required stage. `status` resumes after interruption. Review
and docs can transfer when their current fingerprints match. Lint and test evidence
is never reused in a new run, including commits with identical trees.

## Applicability and audit identity

Fingerprint version 2 describes content inputs. Original commit, base, branch,
configuration digest, run, worktree ownership, execution time, findings, and
coverage remain separate audit identity. A new commit always needs its own note.

Matching patches alone are insufficient. Every stage binds both base and head
trees. Upstream content changes invalidate review even with an unchanged patch.
Retargeting checks the new base's content; current risk,
executor, and hosted approval policy still apply.

| Disposition | Meaning |
| --- | --- |
| `reusable` | All inputs required by the supported contract match. |
| `invalid` | Known inputs changed or stage reuse is disabled; `reasons` identifies the categories. |
| `unknown` | Provenance, inputs, or configuration dependencies cannot be established. Rerun. |

`data.applicability` describes candidate evidence, not the outcome of a stage
that ran freshly. Decisions and later-stage candidates persist on the run. A
repair or changed input triggers another check before import or merge-back.
Unresolved actionable or blocking findings cannot advance through reuse.

## Review and documentation inputs

Review binds base/head trees, included diff bytes, exclusions, delivered grounding
(including conventions and prior findings), intent, effective executor, and the
`[general]`, `[review]`, `[policy]`, `[context]`, `[diff]`, and `[stage]` sections.
The last section governs command-review timeouts and retries.

Intent and shell-command digests hash the exact UTF-8 text bytes without
normalizing whitespace or Unicode. Configuration and documentation-surface
digests hash JSON with sorted keys and compact separators.

Docs binds base/head trees, intent, grounding, documentation content, and
`[general]`, `[docs]`, `[diff]`, `[context]`, and `[policy]`. Its digest includes
every inventory entry's complete bytes, path, existence, size, and whether the
diff touches it. Same-length edits to ignored docs invalidate evidence. Changed
docs between context and submission require fresh context.

An unrelated user-level test-command change leaves review configuration unchanged.
A committed configuration edit changes the head tree and must itself be reviewed.
Unknown configuration sections prevent reuse even when their values match.

Review derivation recomputes both Git manifests, validates original coverage, and
compares every original/current unit and exclusion. Only then can it replace the
current coverage's commit and manifest identity. Original dispositions are retained.

## Shell execution evidence

Lint and test results remain bound to their original execution and run. New
runs execute applicable shell stages again; `shell_reuse_disabled`
explains their applicability decision. Refreshed shell evidence is rejected by
the verifier, including derivations between identical trees.

Same-run tree-equivalent merge-back keeps its existing publication behavior:
validated results can attest the tree-equivalent cherry-picked source commit
without creating refreshed evidence. This does not enable reuse in a new run.

The retired `[reuse]` declarations no longer trigger file, toolchain, environment,
or platform capture. Original same-commit notes remain readable, including their
old declarations and recorded input digests. Fingerprint version 2 and the
version 7 wire shape remain unchanged for compatibility. The typed models and
their remaining safety bindings stay supported. Local `stage run --command` overrides remain
valid fresh execution evidence, bound to their resolved command.

## Provenance, ownership, and recovery

Discovery uses finalized local execution records from the same source-worktree
identity and branch. Other linked worktrees' records are not candidates. All
three modes use the same classification/import path. A fresh clone with only
portable notes needs a local run; the tool does not reconstruct local
execution/ownership records from portable notes.

Origin and configuration snapshot digests are checked when evidence is admitted for
reuse or archived, and when an attestation is verified. Loading a run record performs
structural validation without rehashing its stage origins. Corrupt active evidence
is reported as `stage_evidence_invalid` before merge-back changes the source branch.
Status remains inspectable and recommends inspecting the record, then aborting without
`--force` before starting fresh; ordinary abort still protects unmerged work.

An attestation embeds one original execution per stage, its content digest, and
the current fingerprint. Derivation adds refresh time and `equivalent_inputs`, keeping
original timestamps, process hashes, findings, fixes, and coverage. Provenance is
flattened to one origin: nested chains and unsupported fields/versions are rejected.
The consumer recomputes available Git, manifest, configuration, executor, and
applicability bindings and checks finding dispositions. It cannot independently
measure the past local environment or authenticate an unsigned note.

Original Git commits are retained under `refs/agentic-preflight/evidence/<SHA>`.
Publication sends those refs first, then atomically pushes the branch and notes.
Failure to publish an original stops before branch publication; an interrupted
publication can leave harmless retained evidence refs on the remote. Hosted consumers
fetch missing originals from the contributor's evidence refs before verification;
fetching a notes blob alone cannot retrieve commits named inside its JSON. These
refs survive normal run/worktree cleanup and must remain while any published note
depends on them. Keep the local publisher, hook, and protected verifier on the same
release. Missing originals cannot be reconstructed from hashes.

Clean-checkout, synchronization, merge-back, publication authorization, and atomic
branch/notes push rules still apply. Refresh authorizes no force-push, merge, or
cleanup.
