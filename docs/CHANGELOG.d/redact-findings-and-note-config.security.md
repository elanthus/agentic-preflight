- Finding titles, details, suggestions, and `respond --note` text are now redacted for
  `[worktree] copy_files` values before they are saved, as stage logs already were. A
  reviewer that quotes a copied secret no longer places it in `findings.json` or in the
  published attestation note (GHSA-8c37-fpgq-vp36).
- The published attestation note now leaves out configuration sections from the user
  configuration file, except `[diff]`, and redacts `NAME=value` assignment values in a
  command that depends on such a section. Attestation schema 7 is unchanged. See
  `COMPATIBILITY.md`, and `docs/attestations-and-ci.md` for exactly what a note contains.
