- `telemetry rebuild`, `telemetry sync-hosted`, and `report` now ignore an individual
  configuration key that a later release removed, such as `[hook] enabled`, with a
  warning on stderr that names the file and the key. These commands only read derived
  data, so a stale key in a repository file no longer keeps the user from their own
  telemetry. Every other command still rejects the key, and every command still rejects
  a key the tool never knew.
