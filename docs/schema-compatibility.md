# Saved-data schema boundary

Persisted run records and portable attestations require an explicit `schema_version`
encoded as a JSON integer. Missing, null, boolean, floating-point, string, older, and
future values are rejected; they are never inferred as the current format. Maintainers
changing saved formats or consumer rollout should use this contract with
[fingerprint bindings](fingerprint-contract.md) and
[CI authority](ci-test-authority-design.md).

## Supported records and digests

| Record | Schema | Meaning |
| --- | --- | --- |
| Local run | 2 | Current state, ownership, configuration, and stage evidence |
| Attestation | 7 | Completed local validation or publication-ready evidence with tests pending in trusted CI |

Only these current versions are decoded. Invalid saved data is retained with its
associated work and ownership pointers; recovery requires a compatible tool or manual
inspection, not migration or inferred versioning. Extra fields and cross-version
evidence remain errors.

## Decision

Keep the version constraints on the persisted models so direct validation, nested
journal recovery, and wire decoding enforce the same boundary. Attestation wire
encoding and decoding remain in `wire_schema.py`; shared stage validation stays on the
model.

Evidence refresh is available whenever a run has complete stage provenance. CI
delegation remains independently controlled by protected CI policy, proposed-policy
agreement, and hosted authority checks. Producers read that policy from the specified
protected-base revision, never from the proposed checkout or user overrides.

This boundary does not upgrade stored records, combine publication readiness with
completed tests, or add signing. Those remain separate lifecycle and trust concerns.
