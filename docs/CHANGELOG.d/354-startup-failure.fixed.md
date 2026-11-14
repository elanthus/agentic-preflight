- `telemetry sync-hosted` now counts a check run with the conclusion `startup_failure` as
  failed. A workflow that could not start used to count toward a `success` outcome.
