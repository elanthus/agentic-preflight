# Contract checks during review

Contract checks add executable evidence to review.
The evidence includes code that did not produce an initial finding.
The phase is opt-in.
The phase does not call a model by itself.
An in-harness agent supplies a plan, or an explicitly configured planner command produces one before the command reviewer runs.

The reviewer identifies the input producer, the supported consumer and runtime, the behavioral contract, and an independent oracle.
Each applicable check has separate ordinary, boundary, and composition probes.
Expectations must follow the contract or a supported reference consumer.
Expectations must not copy the implementation under review.
Agentic Preflight has no built-in examples, target functions, or defect-specific generators.

## Enable the phase

Commit the configuration before you start a run:

```toml
[review.contracts]
enabled = true
# Select an existing, trusted image by its immutable ID or repository digest.
# image = "registry.example/review-runtime@sha256:<64 lowercase hex characters>"
max_checks = 8
timeout_seconds = 10
total_timeout_seconds = 60
max_output_bytes = 16384
```

Replace the commented image placeholder with a real digest before probes run.
The runtime must already be available to a Linux Docker engine.
It must contain Python 3 and the dependencies that the probes need.
Agentic Preflight never pulls images.
The container receives no host environment variables, home directory, Docker socket, or credentials.
Docker configuration is an operator responsibility.
Use a trusted image and engine.

With an in-harness reviewer:

1. Run `agentic-preflight context`.
   The response includes `contract_checks.plan_schema`, the current review manifest, the unit IDs, the configured check limit, and `contract_checks.execution`.
2. Write a plan that conforms to that schema.
   Account for every delivered unit exactly once, including units that appear correct.
   Use `checks` with referenced check IDs, `not_applicable` with a concrete reason, or `unverified` with the missing information.
3. Run `agentic-preflight review contracts --file contracts.json`.
4. Fetch context again.
   Inspect the execution evidence and its full receipt.
   Then submit the normal strict findings payload.

The plan has `schema_version: "1"`, the delivered `manifest`, `units`, and `checks`.
Each unit has `unit`, `disposition`, `checks`, and `reason`.
Each check has an ID such as `C001`, a `contract`, a `producer`, a `consumer`, an `oracle`, and exactly three `probes`.
Each probe has a `kind` and Python `code`.
The `kind` is `ordinary`, `boundary`, or `composition`.
The code imports the snapshot from `/workspace`, which is on its module search path.
The code asserts the declared property and exits nonzero when that property fails.
Probes can use other runtimes that are already installed in the selected image, through a Python driver.

A finding is not required for every unit.
An explicit gap is better than an invented oracle.
When the phase is enabled, Agentic Preflight requires a valid plan and receipt before it accepts findings.
It rejects a missing, incomplete, or stale plan instead of skipping the phase silently.

## Installed packages in the image

The planning context includes `contract_checks.execution`.
It has the configured image identity, the execution limits, and `installed_packages`.
`installed_packages` is a code-owned listing of the distributions installed in the pinned image.
Agentic Preflight runs one bounded offline container for each image digest.
The container lists the distributions with `importlib.metadata` and the same interpreter flags as a probe.
It uses the probe restrictions: no network, a read-only root, resource limits, the unprivileged probe user, a deadline, and an output cap.
No snapshot is mounted.
No host interpreter is used instead.
The listing is cached under the run's `contracts/images/` directory, keyed by the image digest.
Repeated `context` calls do not start another container.
A new run lists the image again.

When Docker, the image, or the container is unavailable, the entry has `status: "unavailable"` and a `reason`.
It is never an empty list.
The delivered list is bounded.
`packages_omitted` counts the entries that only the full listing at `path` contains.

The listing states availability only.
It does not assert which consumer contracts the repository supports.
Prefer a listed real consumer to a substitute implementation.
When the real consumer is absent, record that limitation in the check and in the unit reason.
Keep the unit unverified when no independent oracle remains.

## Command reviewers

The worked Claude and Codex wrappers support both planning and final review.
To use one, set `planner_command` to the same installed wrapper as `[review] command`:

```toml
[review.contracts]
enabled = true
planner_command = "python3 -B docs/examples/reviewers/claude_review.py"
# image = "registry.example/review-runtime@sha256:<64 lowercase hex characters>"
```

On the first `review run` for a snapshot, Agentic Preflight sends a canonical context with `section: "contracts"` to the planner.
The wrapper requests only unit dispositions and checks.
It copies the manifest itself.
Agentic Preflight validates the whole plan, runs its probes, and sends `contract_checks` evidence to the final reviewer.
Planning does not receive a first reviewer's finding list.

An enabled command review without `planner_command` asks for a locally submitted plan instead.
An existing matching receipt prevents another planner call on retry.
An invalid or failing planner command counts against the normal durable review-attempt limit.
The planner runs with the privileges and stage timeout of other configured host commands.
It is **not** the sandbox for untrusted probe code.

A model-backed planner can add one paid wrapper invocation before the final review.
Configure and authorize it explicitly.
Wrapper invocation counts are not provider request or token totals.
By default, this phase does not run paid evaluations, select a more expensive model, or change the repository's reviewer configuration.

## Execution boundary and evidence

Agentic Preflight exports committed Git blobs into a disposable snapshot.
The snapshot excludes the Git directory, ignored files, untracked files, and copied working-tree secrets.
Agentic Preflight refuses a dirty worktree.
The container receives the snapshot as a read-only mount.
Before any probe runs, an in-container check compares the exact file set and SHA-256 digests.
The check rejects missing, additional, or changed files.
Symlinks, submodules, and nonportable or case-colliding paths are currently unsupported.
Snapshots larger than 32 MiB or with more than 4,096 files are also unsupported.
Agentic Preflight records unsupported snapshots as unverified.
A remote engine cannot substitute a different snapshot without detection.

Code-owned container settings enforce these limits:

- No network, no image pulls, no writable root filesystem, and no host credentials.
- One CPU, 256 MiB of memory without extra swap, 64 PIDs, and 256 open files.
- A 64 MiB temporary filesystem that does not permit execution or setuid.
- An unprivileged probe user.
  A separate trusted root watchdog is PID 1 in the container's private PID namespace.
  Its deadline terminates the whole namespace, including descendants that created their own process groups.
  An independent duration cap prevents engine clock differences from extending that deadline.
- Per-probe and total deadlines, plus at most two seconds for each attempted container cleanup.
  Both output streams share the configured byte limit.

Only the watchdog keeps the capabilities needed to start the probe as an unprivileged user and to supervise it.
The probe cannot kill that watchdog or regain privileges.
The image and worker are execution dependencies, not sources of expected answers.

Results distinguish `passed`, `failed`, `unverified`, and `not_applicable`.
A probe's zero exit code records that its assertions passed.
A nonzero check is a candidate counterexample, not an automatically confirmed defect.
Timeouts, output or resource limits, missing dependencies, and an unavailable engine or image remain unverified.
No fallback runs a probe in the host Python interpreter.

Receipts retain the plan, the code, and the declared consumer and oracle.
They also retain each probe's output and status, the container cleanup result, the image identity, and the snapshot digest.
Immutable receipt files live under the run's `contracts/` directory, with a pointer to the current receipt.
A receipt binds the plan and execution configuration to the current review manifest and head.
A rerun retains prior receipts.
Review coverage adds the selected receipt digest as `contracts_sha256`.
Thus, a receipt cannot be replaced without a change to the accepted review evidence.

The final context includes a bounded summary and the path of the full receipt.
It lists all check statuses even when output details are omitted.
The reviewer must read relevant omitted evidence and validate the oracle before it reports a defect.
A new run does not automatically import contract-enabled review evidence.
It needs its own phase.
Lint and test stages continue to run normally.

## What the phase establishes

The phase enforces a complete unit-disposition plan and records observable execution.
It does not prove that every declared contract or oracle is correct.
It does not prove that a `not_applicable` disposition was justified.
It does not prove that all input classes were explored.
Unit delivery coverage remains separate from behavioral verification.
In particular, `examined: "all"` is not a proof of comprehension.

Execution results are evidence for the final review, not a new automatic blocking policy.
Unverified checks stay visible as gaps.
The final review's supported findings and the existing finding policy determine progression.
A passing receipt does not establish complete defect recall.
Do not describe an unavailable runtime as successful verification.

Validate improvements on fresh, independently curated cases before you change defaults.
Measure exact mechanism detection, false claims, unresolved checks, and execution and model cost.
The development tests use generic contracts and fake reviewers.
They do not establish a model-quality improvement.
