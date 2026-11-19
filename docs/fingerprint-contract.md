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
Neither fingerprint includes `[general] network_timeout_seconds`, which bounds only fetches and pushes.

The included diff bytes are the patch text that Git prints for the merge base and the head.
The tool fixes the `core.*` and `diff.*` settings that shape that text, removes `GIT_DIFF_OPTS`, ignores replacement objects, and each patch names full object IDs.
Thus, two machines whose Git configuration differs in those settings build the same bytes from the same commits.
Git attributes do not shape the text either.
Git runs without `.gitattributes` files, the index, `attr.tree`, `GIT_ATTR_SOURCE`, and the user and system attributes files.
Text files are diffed with `--text`.
A file is binary when either side is larger than 512 MiB or has a NUL byte in its first 8,000 bytes, which is Git's own default rule.
Its patch is Git's `Binary files ... differ` line, and its content is never printed.
Hunk headers use Git's built-in rule; `diff.default.xfuncname` or `diff.default.funcname`, which would replace it for every file, stops the command with `review_diff_attribute`, as does a hunk-header pattern for a driver named `set`, `unset` or `unspecified`. Such a driver with `binary` set to false is refused the same way, because it would print a binary file as text.
Git always reads `$GIT_DIR/info/attributes`.
`-diff` there has no effect.
A diff driver there with a hunk-header pattern, or `diff` set on a binary file, stops the command with `review_diff_attribute`, because the text would differ on this machine only.
Notes from 0.8.1 and earlier hash patches with abbreviated object IDs, whose length depends on the clone, and whose text followed the checkout's attributes.
Git picks the default length from the clone's object count, so such a note can fail in a clone whose count gives a different length, even when its producer used every default.
A verifier builds that text only when the current text does not reproduce a note's digest.
It fixes the other settings to Git's default for that text too, so a note whose producer changed one of them may not verify, if the change altered the patch.
A note for a commit whose committed `.gitmodules` hid a changed submodule pointer never verifies, because both texts now list that pointer.
In that text a file-level patch, such as a binary change, names its blobs only by abbreviated object IDs, which a clone that holds both blobs disambiguates.
It rejects the note when that text marks a file whose content is text as binary.
See [the compatibility entry](../COMPATIBILITY.md#review-patches-no-longer-depend-on-git-configuration)
and [the attributes entry](../COMPATIBILITY.md#review-patches-no-longer-depend-on-git-attributes).

Configuration digests cover the published configuration snapshot, because a verifier recomputes them from the note.
That snapshot leaves out sections from the user configuration file, except `[diff]`.
Local discovery also compares those user-level sections in the prior run record, so a change to one still prevents reuse.
A shell-command digest covers the command as the note publishes it.
Every command from a user-level `[commands]` section therefore has the same digest.
This does not weaken freshness: shell fingerprints are compared only for the same recorded command, and shell evidence is never reused across runs.

Intent and shell-command digests hash the exact UTF-8 text bytes.
They do not normalize whitespace or Unicode.
Configuration and documentation-surface digests hash JSON with sorted keys and compact separators.

Docs binds the base and head trees, the intent, the grounding, the documentation content, and the `[general]`, `[docs]`, `[diff]`, `[context]`, and `[policy]` sections.
Its digest includes every inventory entry's complete bytes, path, existence, size, and whether the diff touches it.
The inventory lists only blobs committed at the head commit, so an ignored or untracked document does not enter the digest.
Existence, size, and content come from those blobs, including outside a sparse checkout.
The caller pins HEAD to the expected commit; checkout presence and content do not affect the surface.
A symlink contributes its link-text blob, including dangling or directory-target links; its target is never followed.
LFS pointers and normalized line endings contribute their committed bytes, without smudge filters or checkout conversion.
Changes to committed docs between context and submission require fresh context.

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
Fingerprint version 2 and the stage evidence wire shape are unchanged for compatibility, so stage evidence that an earlier run recorded keeps its old declarations and recorded input digests and can still be reused.
A note is a different matter. Attestation schema 8 adds note fields outside the stage evidence, and this release reads schema 8 only: a schema 7 note fails verification with `incompatible_schema`, and its commit must be validated again, which replaces the note.
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

## Documentation blob migration

The migration from checkout bytes to head-commit blobs keeps fingerprint version 2
and the existing canonical JSON fields and ordering. We considered a versioned
digest, which would invalidate all docs evidence, and dual legacy/new digests,
which would retain checkout-dependent semantics. We instead accept a one-time
invalidation only where the computed surface changes: missing sparse files,
symlink targets, smudged LFS content, or converted line endings. Ordinary files
whose checkout bytes equal their blobs keep their surface digest.

Existing run records and original attestations remain readable; their recorded
digests are not rewritten. When local evidence discovery compares an old surface
with a different blob-based surface, `doc_surface_changed` requires a fresh docs
stage. No fallback to checkout bytes is used for evidence reuse. The base/head
tree, grounding, and other fingerprint bindings continue to apply independently.

## Packaged reviewer orchestration telemetry

Retries, verification decisions, and per-call usage are local reviewer events,
not stage evidence fields. Flags in the configured command remain part of its
existing executor binding. No metadata enters attestations and fingerprint
version 2 is unchanged. Events do not establish reviewer identity or quality.
