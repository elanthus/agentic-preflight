- A low- or medium-severity `auto_fix` finding left open at a green review or docs stage
  no longer lets the run continue to lint, test, and `mergeback`. There, `respond` was no
  longer legal and `mergeback` failed with an internal error, leaving the run in
  `MERGEBACK_PENDING` with a retry hint that could never succeed. `context --section docs`
  and `stage run lint` now refuse with exit 3 and `open_findings`, naming the findings and
  the `respond` command. `mergeback` refuses such a run from an earlier release the same
  way, before it changes anything, and points to `abort`.
