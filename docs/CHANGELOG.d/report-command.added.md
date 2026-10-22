- Add `agentic-preflight report`, which counts finished runs from the local run
  summaries for this repository, or for every repository with `--all-repos`. It reports
  run states, completion, findings, blocking findings fixed, repair commits, validation
  restarts, stage retries and reuse, median and p90 duration, and selected events,
  overall and for each repository, client, and reported model. Filters are `--since`,
  `--until`, `--repo`, and `--state`. Each ratio names its denominator, null values are
  counted as excluded, and malformed mirror lines and unreadable run records are
  reported. When the mirror has no rows for the clone, the report summarizes the
  clone's run records in memory. `--format markdown` adds compact tables in
  `data.markdown`. Every report lists its limits. The command writes nothing and makes
  no network request.

- Add the "Summarize preflight history" playbook to the skill: rebuild stale mirrors,
  run `report --all-repos --format markdown`, and show the result with its limits.
