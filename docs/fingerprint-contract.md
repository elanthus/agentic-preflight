# Fingerprint contract for reusable preflight evidence

Tracks [issue #85](https://github.com/elanthus/agentic-preflight/issues/85). This
document is the fingerprint contract and derivation model that issue asks to write
down before implementation, and it describes the first landed slice:
`agentic_preflight/fingerprints.py`, which classifies whether a prior green
**review** or **docs** result remains applicable to a new commit.

## Status: library only, not yet wired in

This slice adds pure, unit-tested classification functions. It does **not** change
what `agentic-preflight start` does, does not add fields to the version 4
attestation schema, and does not let a reused stage attach to a new commit. Today,
any rebase that produces a new SHA still requires a fresh review, docs, lint, and
test run — see `docs/limits.md`. The remaining requirements from issue #85 —
shell-stage input contracts, derived-attestation issuance, and state-machine
auto-discovery of reusable evidence — are follow-up work, tracked as separate pull
requests so each is reviewable on its own and so the attestation schema is not
changed until a verifier that understands it can be installed first (this
repository installs its verifier from the protected PR base — see
`docs/attestations-and-ci.md`).

## Why content, not identity

The current `attestation.reuse_exact` requires the *exact* attested commit SHA to
still exist and be merge-equivalent to a fresh base. That is correct but stricter
than necessary: rebasing or restacking a branch changes every downstream commit
SHA even when no downstream tree changed at all. This contract asks a narrower
question per stage: *did the content this stage's result actually depended on
change?* A fingerprint is therefore built from tree SHAs and content digests, never
from commit SHAs, commit counts, or reflog position.

This is deliberately conservative. Matching trees prove the stage's declared
inputs are byte-identical; they do not prove semantic equivalence of anything not
captured in the fingerprint. Upstream code the current patch depends on but does
not touch is exactly the base-tree input this contract binds to, which is why a
base-tree change invalidates review even when the patch text is unchanged.

## The three-value disposition

Every classification produces one `Disposition`:

- **`reusable`** — every declared input matches. The prior result may be carried
  forward as-is.
- **`invalid`** — at least one declared input changed. Reasons name every field
  that differed, not just the first.
- **`unknown`** — there is no prior fingerprint to compare (an older attestation
  predates this contract, or was produced by a version of it this code no longer
  understands), or the fingerprint version itself differs. Unknown always means
  rerun; it is never treated as reusable. This is what keeps the contract safe to
  extend later: a field this code cannot yet compare must not be silently ignored.

## Per-stage input contracts (v1)

### Review (`ReviewFingerprint`)

| Field | Source | Why it must match |
| --- | --- | --- |
| `base_tree_sha` | `git rev-parse <base>^{tree}` | Upstream content the patch was written against. |
| `head_tree_sha` | `git rev-parse <head>^{tree}` | The reviewed content itself. |
| `diff_sha256` | `ReviewManifest.diff_sha256` | The exact included diff bytes, after exclusions. |
| `excluded_files` | `ReviewManifest.excluded_files` | Which files review coverage does *not* claim to have seen. |
| `grounding_sha256` | `ReviewManifest.grounding_sha256` | CODEOWNERS, docs, conventions, and prior-finding context delivered with the diff (issue #80). |
| `intent_sha256` | `attestation.intent_digest(run.intent)` | The objective the review was scoped against. |
| `executor` | resolved `in_harness` / `command` | A policy-escalated command review is not equivalent to an in-harness one. |
| `config_sha256` | `config_digest(review_relevant_config(snapshot))` | Only the `[general]`, `[review]`, `[policy]`, `[context]`, `[diff]`, and `[stage]` sections — see below. |

### Docs (`DocsFingerprint`)

| Field | Source | Why it must match |
| --- | --- | --- |
| `base_tree_sha` | same as review | Same upstream-content argument. |
| `head_tree_sha` | same as review | The reviewed content itself. |
| `doc_surface_sha256` | digest of `stages.docs.build_inventory(...)` | The documentation inventory (paths, existence, size, touched-by-diff) the docs stage was actually shown, not only its path list. |
| `config_sha256` | `config_digest(docs_relevant_config(snapshot))` | Only `[general]`, `[docs]`, and `[diff]`. |

### Scoped configuration, not the whole digest

`RunDoc.config_digest` is the full effective-configuration digest and stays as the
audit binding it already is. A fingerprint instead hashes only the configuration
subset declared relevant to that stage (`review_relevant_config` /
`docs_relevant_config`). This is requirement 1 from issue #85: a `[commands] test`
change must not discard review or docs evidence that never read it. An
undeclared configuration dependency is a bug in the relevant-config function, not
something this contract can detect — the scope is deliberately narrow and
enumerated rather than inferred.

`[stage]` is declared relevant to review, not because review reads it directly,
but because the command executor does: `review_executor.py` runs the configured
review command under `[stage] timeout_seconds`, and `review_retry.py` bounds its
retries with `[stage] max_attempts`. A shorter timeout or a smaller retry bound
can change whether the same command still produces the same green result, so
this is an execution dependency of the *executor*, not an unrelated section —
even though an in-harness review never reads `[stage]` at all. Scoping stays
conservative here: including `[stage]` unconditionally (rather than only when
`executor == "command"`) means an in-harness review's fingerprint moves on a
`[stage]` change too, which is over-invalidation, not under-invalidation — the
safe direction when a dependency is real for only one of the two executors.

Shell stages (lint, test) are out of scope for this slice. Their input contract
must additionally declare which command/dependency/toolchain/environment/copied
inputs are supported for cross-commit reuse at all, per issue #85's safety rule
that undeclared or unverifiable shell dependencies must prevent reuse — that is
follow-up work, not an extension of `ReviewFingerprint`/`DocsFingerprint`.

## Versioning

`FINGERPRINT_VERSION` is a single integer bumped whenever a fingerprint's field
set or comparison semantics change. `classify_review`/`classify_docs` treat a
version mismatch identically to a missing fingerprint: `unknown`, not `invalid`
and never `reusable`. This is what lets the contract evolve without a stale
consumer ever misreading a newer fingerprint shape as a match.

## What derivation will need (not yet built)

Issuing a derived attestation for a new exact commit — requirement 5 of issue
#85 — will need to record, per stage: the classification and its reasons, the
original run/commit/evidence identity being derived from, the original execution
timestamps, and a derivation reason and refresh time distinct from the original
`green_at`. It must not mutate an existing note's SHA, and it must not claim a
reused stage ran again. That work depends on this contract but is not part of it;
this document will be extended alongside that implementation rather than
guessing its shape now.
