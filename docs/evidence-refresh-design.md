# Automatic evidence refresh

Implementation design for [issue #85](https://github.com/elanthus/agentic-preflight/issues/85).
This document is for maintainers implementing and reviewing the remaining work.
The implemented input and lifecycle contract is documented in
[Fingerprint contract](fingerprint-contract.md). This design records the approach
written before connecting the existing fingerprint library to the run lifecycle.

## Applicability and identity

Keep exact commit, base, branch, run, configuration, review manifest, findings,
and execution timestamps as audit identity. Compare separately versioned stage
inputs. Equality of base and head trees is mandatory for review and docs; an
unchanged patch alone is insufficient. Recompute manifests and account for every
old and new unit before transferring coverage. Include delivered grounding and
prior findings, intent, effective executor, and applicable policy. Documentation
inputs include the bytes of the documentation surface, including ignored files
the inventory exposes, and the context delivered with the change.

Shell reuse requires an explicit content contract for each command. Its author
asserts that the command depends only on the tracked source trees, declared
dependency/toolchain files, declared environment, platform, and configured setup
and execution policy. History, external services, time, and undeclared inputs
are unsupported. Missing or unreadable inputs produce `unknown`, which means
rerun. Diagnostics contain categories and digests, never input values or file
contents. Capture inputs before execution and check them afterward; a changed
input cannot support a reusable result. This is a repository assumption, not
automatic dependency discovery or a sandbox for arbitrary commands.

Configuration remains strict. Explicitly enumerate each stage's relevant and
irrelevant sections; unfamiliar sections invalidate conservatively. A user-level
test-command change can preserve review. A committed configuration edit still
changes the head tree and therefore requires review of that edit.

## Discovery and transitions

After synchronization and setup, discover evidence belonging to this source
worktree, and validate its original bindings. Never use another linked
worktree's active run merely because its trees match. Persist one classification
per stage, including reason codes and changed input categories, before advancing.

Advance through existing state-machine transitions in order. Import a reusable
stage only when its prerequisites are satisfied; retain candidate evidence for
later stages while earlier stages need work. `status` resumes this advancement
after interruption. Recompute candidate applicability after every repair and
before importing a result. Carry findings and their dispositions with the review;
re-evaluate blockers and risk under current policy. No successful classification
can dispose an unresolved finding.

Use the same mechanism in `in_place`, `reusable`, and `strict` worktrees. Bind the
original execution head independently of any mergeback head. Existing clean-tree,
stale-head, merge-tree-equivalence, ownership, and atomic branch/notes publication
checks remain prerequisites. Unsupported provenance causes a fresh stage.

## Attestation derivation

A new wire version must record each original execution's run, commit, base,
stage result, execution time, configuration binding, fingerprint, and original
coverage. Derived stages reference immutable original evidence by digest and
retain finding/fix provenance. Record refresh time separately from execution
time. Flatten references to original executions and enforce a finite bound;
missing, cyclic, malformed, or unsupported provenance is invalid evidence.

The consumer recomputes available Git, manifest, and policy bindings and checks
the complete stage set. It must never accept a submitted `reusable` assertion as
proof. Local environment assumptions stay explicit: an unsigned note is an audit
record, not cryptographic authentication or a hosted measurement of local inputs.

## Compatible rollout

The protected base supplies the hosted verifier. Land consumer support before
enabling new-format production. If consumer and producer code share a PR, keep
production disabled against bases without that consumer and perform a complete
legacy run to publish that PR. After its merge the consumer is available and
new-format production can activate. Never switch the privileged verifier to PR
code or make an old consumer accept unknown fields.

Historical v4 notes without fingerprints remain valid historical evidence but
cannot acquire reusable fingerprints retrospectively. Existing exact-commit
reuse remains available under its existing contract. Test the producer and both
old and new consumers together, including the bootstrap publication path.

CI-delegated tests are a separate lifecycle change in #86. This work requires
locally completed stages and cannot represent pending CI as green or skipped.
