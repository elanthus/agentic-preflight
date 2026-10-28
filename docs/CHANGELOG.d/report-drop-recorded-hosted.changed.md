- Run summaries and `agentic-preflight report` drop the recorded hosted outcome, the
  `hosted.last_ci_outcome` and `hosted.pr_url_recorded` fields and the "Hosted outcomes
  recorded" report section. No current version recorded them. A summary's `hosted` object
  now holds only the fields that `telemetry sync-hosted` joins. The "Hosted outcomes
  synced" section and the two `data.limits` entries about hosted checks appear only when
  at least one run in the report was synced.
