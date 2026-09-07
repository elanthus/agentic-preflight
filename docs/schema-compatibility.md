# Attestation compatibility boundary

Issue #101 keeps the public `Attestation` model and existing wire formats. This
decision records the migration scope established before implementation. Maintainers changing
note formats or consumer rollout should use this contract with
[fingerprint bindings](fingerprint-contract.md) and
[CI authority](ci-test-authority-design.md).

## Supported records and digests

| Schema | Meaning | Additional fields | Encoding |
| --- | --- | --- | --- |
| 4 | Completed local validation; exact-commit reuse | No refresh evidence or configuration snapshot | Omits refresh and CI fields |
| 5 | Completed local validation with per-stage provenance | Evidence for all four stages and its configuration snapshot | Omits CI fields |
| 6 | Publication ready with tests pending in trusted CI | Local review/docs/lint evidence, configuration snapshot, delegation, publication time | Null `green_at`; no synthetic test execution |

Decoding preserves the recorded version. Unknown versions, extra fields and
cross-version evidence remain errors. Configuration snapshots retain their exact
digest contract: default `[ci]` is omitted; non-default CI policy is included.
No new configuration defaults or snapshot fields are introduced by this refactor.

## Decision

Keep one normalized model for callers. Move version-specific validation, wire
encoding/decoding, and producer schema selection into `attestation_schema.py`.
Shared stage validation stays on the model. This avoids three largely duplicated
models and keeps existing construction and error handling compatible. A small
configuration compatibility helper owns omission of default CI snapshots.

Use the existing committed declarations as the intended consumer interface:
`[reuse] attestation_schema = 5` for refresh and `[ci] consumer_schema = 6` for
CI-aware snapshots. Read them from the specified protected-base revision, never
the proposed checkout or user overrides. An explicit unsupported declaration or
malformed policy means unavailable; source markers cannot override it. Declaring
schema 4 explicitly disables refresh even if consumer source is present.

CI capability does not enable delegation by itself. Protected CI policy, proposed
policy agreement, and hosted authority checks still apply. Producers with
non-default CI snapshots require a CI-aware consumer before publishing v5. A
producer-only rollout falls back to complete local validation and compatible v4.
This repository now commits both declarations for its installed consumers while
retaining the default local test authority. This deliberately changes its own
effective snapshot; default configuration serialization for other repositories
remains unchanged.

## Legacy transition

When a capability key is absent, `consumer_capabilities.py` alone may recognize
the historical refresh constant or `TestDelegation` class marker in committed
protected-base source. Existing consumers using those markers remain supported.
Explicit declarations require no Python source inspection for that capability.

Before retiring marker fallback, supported protected bases must have committed
the declarations and installed the corresponding consumers; the minimum supported
consumer release must require those declarations, and a breaking-release migration
notice must announce removal. Until then, keep absence-only fallback and its tests.
Moving or reformatting legacy source without first declaring capability disables
that fallback conservatively. Declarations assert installed support; they do not
install a consumer or weaken protected-base trust.

This change does not upgrade stored notes, combine publication readiness with
completed tests, or add signing. Those remain separate lifecycle and trust concerns.
