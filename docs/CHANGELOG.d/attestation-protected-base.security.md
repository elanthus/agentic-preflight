- `verify`, `approval-check`, `push`, and the pre-push hook take attestation policy from the
  `.agentic-preflight.toml` committed at the trusted base instead of the note's own
  configuration snapshot. When the caller passes the protected base with `--base`, as
  `approval-check` and the hosted workflow do, a branch can no longer skip docs, swap a
  configured lint, test, or review command, widen review exclusions, or lower the review
  executor for itself. `push` and the pre-push hook have no `--base`, so they trust the
  branch the note names; a forged note can still name a branch with a weaker policy there.
- Verification recomputes the merge base against the trusted base instead of trusting the
  note's `merge_base_sha`, and rejects a mismatch or an ambiguous merge base with the new
  `base_mismatch` reason. `verify` gains `--base REV`, and the hosted attestation workflow
  passes the pull request's base SHA.
- The README and SECURITY.md now state that attestations are unsigned records, not proof that
  the stages ran.
