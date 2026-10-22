- Keep runs and hook decisions of temporary repositories out of the user-level telemetry
  directory. A repository is temporary when it is under the system temporary directory
  and has no network remote: no `origin`, a local path such as a bare repository, or a
  loopback host. Test and evaluation harnesses that drove the CLI against fixture
  repositories appended their runs to the real `runs.jsonl` mirror. A scratch clone under
  the temporary directory that pushes to a host keeps its telemetry. A run of a temporary
  repository still writes `summary.json` in its run directory, but appends no mirror row, and
  `hook-check` appends no line to `hook-decisions.jsonl`. `data.telemetry` and the
  `summary_written` event report `reason: "temporary_repository"`. `telemetry rebuild`
  and `telemetry sync-hosted` in such a clone leave the mirror unchanged, and in other
  clones they leave out runs of temporary repositories; their `mirror` object adds
  `reason` and `temporary_runs`. `report` counts those runs in
  `data.source.clone.temporary_runs` and no longer suggests a rebuild that cannot add
  them. An explicit `[telemetry] path` is an opt-in and still receives every row.

- The regression eval runner gives every CLI subprocess its own `XDG_DATA_HOME` and
  `XDG_CACHE_HOME` in the eval workspace.
