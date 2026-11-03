- `start` holds the new run's operation lock until the run is ready for review. It used to
  release it right after creating the record, so a concurrent `start --replace`, `gc`, or
  `abort` could orphan the run while its checkout was still being provisioned.
