- Add `agentic-preflight telemetry sync-hosted`, which joins GitHub pull-request and
  check outcomes onto the pushed `DONE` runs of the clone. It is the only telemetry
  command that talks to a network service, and it only reads: `gh api` GET requests for
  the pull requests and the check runs of each pushed commit. It writes `hosted.json` in
  each run directory, then regenerates the summary and the mirror row of that run. The
  summary's `hosted` object gets `pr_number`, `pr_state`, `merged`, `checks_conclusion`,
  and `fetched_at`. `--limit` (default 50), `--run`, `--refresh`, and `--dry-run` select
  the runs. A missing or logged-out `gh`, an `origin` that is not on github.com, a rate
  limit, or a network failure stops the command with an error code in the envelope, and
  the runs synced before it keep their outcomes. A failure that concerns one commit is
  listed in `data.errors`, and the older runs are still read. No workflow command calls
  it.

- `agentic-preflight report` adds synced hosted outcomes, overall and for each
  repository: synced runs, "preflight green, hosted checks failed" over synced runs with
  settled checks, pull requests found, the merged share over synced runs with a pull
  request, and the check conclusions. Two new limits say that a failed hosted check is
  not proof of an escaped defect, and that only synced runs count.

- Add a `telemetry sync-hosted` step to the "Summarize preflight history" playbook, for
  when the user wants hosted outcomes.
