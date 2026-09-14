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
cross-version evidence remain errors.

## Decision

Keep one normalized model for callers. Move version-specific validation, wire
encoding/decoding, and producer schema selection into `wire_schema.py`.
Shared stage validation stays on the model. This avoids three largely duplicated
models and keeps existing construction and error handling compatible.

Evidence refresh is available whenever a run has complete stage provenance. CI
delegation remains independently controlled by protected CI policy, proposed-policy
agreement, and hosted authority checks. Producers read that policy from the specified
protected-base revision, never from the proposed checkout or user overrides.

This change does not upgrade stored notes, combine publication readiness with
completed tests, or add signing. Those remain separate lifecycle and trust concerns.
