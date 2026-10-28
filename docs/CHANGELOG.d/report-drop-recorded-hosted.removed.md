- Run summaries and `agentic-preflight report` drop the recorded hosted outcome: the
  `hosted.last_ci_outcome` and `hosted.pr_url_recorded` summary fields, the "Hosted
  outcomes recorded" report section, and the `outcome_recorded`, `outcomes`,
  `pr_url_recorded`, `pr_url_excluded_runs`, and `note` keys of `data.overall.hosted`.
  The "Hosted outcomes synced" section and the two `data.limits` entries about hosted
  checks appear only when at least one run in the report was synced.
