- List the distributions installed in the pinned contract-check image in one bounded,
  snapshot-free container per image digest and deliver them to the contract planner as
  `contract_checks.execution.installed_packages`, cached under the run's
  `contracts/images/` directory. Unavailable Docker, images, or listings are explicit
  entries with a reason, never an empty list. The listing states availability only. The
  worked planner prompt now prefers a listed real consumer over a substitute
  implementation and records a limitation when the consumer is absent.
