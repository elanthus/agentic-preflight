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
   The response includes `contract_checks.plan_schema`, the current review manifest, the unit IDs, and the configured check limit.
2. Write a plan that conforms to that schema.
   Account for every delivered unit exactly once, including units that appear correct.
   Use `checks` with referenced check IDs, `not_applicable` with a concrete reason, or `unverified` with the missing information.
3. Run `agentic-preflight review contracts --file contracts.json`.
4. Fetch context again.
   Inspect the execution evidence and its full receipt.
   Then submit the normal strict findings payload.

The plan has `schema_version: "2"`, the delivered `manifest`, `units`, and `checks`.
Each unit has `unit`, `disposition`, `checks`, and `reason`.
Each checked unit also has an `inventory` of changed transformations and their input classes.
Each check has an ID such as `C001`, a `contract`, a `producer`, a `consumer`, an `oracle`, `oracle_provenance`, and exactly three `probes`.
Each probe has a `kind` and Python `code`.
The `kind` is `ordinary`, `boundary`, or `composition`.
The code imports the snapshot from `/workspace`, which is on its module search path.
The code asserts the declared property and exits nonzero when that property fails.
Probes can use other runtimes that are already installed in the selected image, through a Python driver.

A finding is not required for every unit.
An explicit gap is better than an invented oracle.
When the phase is enabled, Agentic Preflight requires a valid plan and receipt before it accepts findings.
It rejects a missing, incomplete, or stale plan instead of skipping the phase silently.

## Behavioral inventory and oracle provenance

Build the inventory before you select probes.
Derive the transformations, input forms, representations, and relevant interactions from the reviewed source and its contracts.
Do not use a fixed list of target functions or known defects.

Each entry in a unit's `inventory` has a `transformation` and nonempty `input_classes`.
Each input class has these fields:

- `name`: a distinct input class within that transformation.
- `disposition`: `probes` or `unverified`.
- `probes`: references with a `check` ID and probe `kind`.
  A `probes` disposition requires at least one reference.
  An `unverified` disposition requires an empty list.
- `reason`: why these probes exercise the class, or what evidence is missing.

References must point to checks assigned to that unit.
Every unit check and every probe must have an input-class reference.
A unit marked `unverified` can record known input-class gaps.
A `not_applicable` unit cannot claim inventory coverage.
Transformation names must be distinct within a unit.
These rules validate the declared references.
They do not prove that the inventory is complete or that a probe exercises its claimed class.

Each check's `oracle_provenance` has these fields:

- `kind`: `documented_contract`, `reference_consumer`, or `derived_property`.
- `source`: a repository location, reference consumer, or specification reference.
- `rationale`: why the expectation is independent of the implementation under review.
- `input_domain`: the inputs for which the oracle is supported.
- `runtime`: the relevant runtime and version when known.
- `limitations`: an explicit list of assumptions or missing evidence.
  Use an empty list when none are known.

One runtime does not establish another consumer's semantics.
A handwritten reference implementation also needs a supported domain and recorded limitations.
Keep unsupported requirements and unavailable consumers unverified.
An unverified input class makes the overall receipt `unverified`, even when all executable assertions pass.
This status remains evidence for review, not a new blocking policy.

The planning context includes the configured image identity and execution limits.
It does not assert which packages an image contains or which consumer contracts the repository supports.

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
An existing matching schema-2 receipt prevents another planner call on retry.
Schema-1 receipt files remain readable and unchanged.
Pending reviews need a schema-2 plan before a new findings submission.
Context reports an older receipt under `previous_receipt` and returns the current plan schema.
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
The summary includes the unit inventory and check details within a fixed byte budget.
The inventory takes at most one third of that budget, so check details always have room.
Check details include the producer, consumer, oracle provenance, exact probe code, and execution results.
Details are ordered failed, unverified, then passed, and then by plan order.
A long probe output stream is shortened in the summary with a marker; the full receipt keeps every byte.
`claimed_classes` counts the declared input classes that rely on each probe.
A probe that many classes rely on deserves a closer look.
`inventory_units_omitted` and `details_omitted` report omitted inventory entries and checks separately.
`probe_code_included` reports whether all check details are present.
`handoff_complete` is true only when both the inventory and all check details are included.
Each check's `plan_pointer` locates its plan entry in the full receipt.
The reviewer must read the full receipt when the handoff is incomplete or a needed assertion is absent.
Before judging findings, the reviewer challenges untested classes and consumer semantics, including those associated with passed checks.
The reviewer validates the oracle's provenance, domain, limitations, and actual assertions.
Only supported defects become findings; a generic coverage gap is not a defect by itself.
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
