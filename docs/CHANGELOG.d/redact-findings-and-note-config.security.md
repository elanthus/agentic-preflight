- Finding titles, details, suggestions, and `respond --note` text are now redacted for
  `[worktree] copy_files` values before they are saved, as stage logs already were. A
  reviewer that quotes a copied secret no longer places it in `findings.json` or in the
  published attestation note (GHSA-8c37-fpgq-vp36).
- The published attestation note now leaves out configuration sections from the user
  configuration file, except `[diff]`, and records `[redacted user-level command]` in
  place of a command that depends on such a section. Evidence reuse refuses earlier
  evidence that would carry withheld configuration or unredacted finding text into a
  note. Attestation schema 7 is unchanged. See `COMPATIBILITY.md`, and
  `docs/attestations-and-ci.md` for exactly what a note contains.
- `review compare` redacts copied-file values in compared findings before it writes its
  summary and event. Its refusals no longer quote rejected values and are redacted for
  copied-file values.
- `mergeback`, `gate`, and `push` refuse a run that an earlier release started (exit 4,
  `needs_human`, `data.reason` `unknown_config_provenance`), because such a run did not
  record which configuration came from the user file. Abort it and start a fresh run. Run
  records now always carry `user_config_sections`, so earlier releases cannot read new run
  records.
