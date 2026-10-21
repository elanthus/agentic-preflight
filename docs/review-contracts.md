# Contract checks during review

Contract checks add executable evidence to review, including code that did not
produce an initial finding. The phase is opt-in. It does not call a model by itself:
an in-harness agent supplies a plan, or an explicitly configured planner command
produces one before the command reviewer runs.

The reviewer identifies the input producer, supported consumer and runtime,
behavioral contract, and an independent oracle. Each applicable check has separate
ordinary, boundary and composition probes. Expectations must follow the contract or
a supported reference consumer, rather than copying the implementation under review.
The product has no built-in examples, target functions or defect-specific generators.

## Enable the phase

Commit configuration before starting a run:

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

Replace the commented image placeholder with a real digest before executing probes.
The runtime must already be available to a Linux Docker engine and contain Python 3
and the dependencies needed by the probes. Images are never pulled. The container
receives no host environment variables, home directory, Docker socket or credentials.
Docker configuration is an operator responsibility; use a trusted image and engine.

With an in-harness reviewer:

1. Run `agentic-preflight context`. The response includes `contract_checks.plan_schema`,
   the current review manifest, unit IDs and the configured check limit.
2. Write a plan conforming to that schema. Account for every delivered unit exactly
   once, including units that appear correct. Use `checks` with referenced check IDs,
   `not_applicable` with a concrete reason, or `unverified` with the missing information.
3. Run `agentic-preflight review contracts --file contracts.json`.
4. Fetch context again, inspect the execution evidence and its full receipt, then
   submit the normal strict findings payload.

The plan has `schema_version: "1"`, the delivered `manifest`, `units`, and `checks`.
Each unit has `unit`, `disposition`, `checks` and `reason`. Each check has an ID such
as `C001`, `contract`, `producer`, `consumer`, `oracle`, and exactly three `probes`.
Each probe has `kind` (`ordinary`, `boundary`, or `composition`) and Python `code`.
Code imports the snapshot from `/workspace`, which is on its module search path,
asserts the declared property, and exits nonzero when that property fails. Tests may
use other runtimes already installed in the selected image, through a Python driver.

No finding is required for every unit. An explicit gap is preferable to an invented
oracle. A valid plan and receipt are required before findings can be submitted when
the phase is enabled; missing, incomplete or stale plans cannot silently skip it.

## Command reviewers

The worked Claude and Codex wrappers support both planning and final review. To use
one, set `planner_command` to the same installed wrapper as `[review] command`:

```toml
[review.contracts]
enabled = true
planner_command = "python3 -B docs/examples/reviewers/claude_review.py"
# image = "registry.example/review-runtime@sha256:<64 lowercase hex characters>"
```

On the first `review run` for a snapshot, the product sends a canonical context with
`section: "contracts"` to the planner. The wrapper asks for unit dispositions and
checks, and copies the manifest itself. The product validates the whole plan, runs
its probes, and sends `contract_checks` evidence to the final reviewer. Planning
does not receive a first reviewer's finding list.

An enabled command review without `planner_command` asks for a locally submitted
plan instead. An existing matching receipt avoids another planner call on retry.
Invalid or failing planner commands use the normal durable review-attempt limit.
The planner runs with the privileges and stage timeout of other configured host
commands; it is **not** the sandbox for untrusted probe code.

Enabling a model-backed planner can add one paid wrapper invocation before final
review. Configure and authorize it explicitly. Wrapper invocation counts are not
provider request or token totals. This feature does not run paid evaluations, select
a more expensive model or change the repository's reviewer configuration by default.

## Execution boundary and evidence

The product exports committed Git blobs into a disposable snapshot. It excludes the
Git directory, ignored files, untracked files and copied working-tree secrets.
Dirty worktrees are refused. Source is mounted read-only; an in-container check of
the exact file set and SHA-256 digests rejects missing, additional or changed files
before any probe executes. Symlinks, submodules, nonportable/case-colliding paths,
snapshots larger than 32 MiB or 4,096 files are currently unsupported and recorded
as unverified. A remote engine cannot substitute a different snapshot unnoticed.

Code-owned container settings enforce:

- No network, image pulls, writable root filesystem or host credentials.
- One CPU, 256 MiB memory without extra swap, 64 PIDs and 256 open files.
- A 64 MiB temporary filesystem, with executable mounts and setuid disabled there.
- An unprivileged probe user. A separate trusted root watchdog is PID 1 in the
  container's private PID namespace. Its deadline terminates the whole namespace,
  including descendants that created their own process groups. An independent
  duration cap prevents engine clock differences from extending that deadline.
- Per-probe and total deadlines, plus at most two seconds for each attempted
  container cleanup. Both output streams share the configured byte limit.

The watchdog alone retains the capabilities needed to drop the probe's user/group
and supervise it. The probe cannot kill that watchdog or regain privileges.
The image and worker are execution dependencies, not sources of expected answers.

Results distinguish `passed`, `failed`, `unverified` and `not_applicable`. A probe's
zero exit code records that its assertions passed. A nonzero check is a candidate
counterexample, not an automatically confirmed defect. Timeouts, output/resource
limits, missing dependencies or an unavailable engine/image remain unverified.
There is no fallback that executes a probe in the host Python interpreter.

Receipts retain the plan, code, declared consumer/oracle, per-probe output and
status, container cleanup result, image identity and snapshot digest. Immutable
receipt files live under the run's `contracts/` directory, with a pointer to the
current receipt. They bind the plan and execution configuration to the current
review manifest and head. Rerunning retains prior receipts. Review coverage adds
the selected receipt digest as `contracts_sha256`, so a receipt cannot be replaced
without changing the accepted review evidence.

The final context includes a bounded summary and the full receipt's path. It lists
all check statuses even when output details are omitted. The reviewer must read
relevant omitted evidence and validate the oracle before reporting a defect. A
new run does not automatically import contract-enabled review evidence; it needs
its own phase. Lint and test stages continue to run normally.

## What the phase establishes

The phase enforces a complete unit-disposition plan and records observable execution.
It does not prove that every declared contract or oracle is correct, that a
`not_applicable` disposition was justified, or that all input classes were explored.
Unit delivery coverage remains separate from behavioral verification. In particular,
`examined: "all"` is not a proof of comprehension.

Execution results are evidence for the final review, not a new automatic blocking
policy. Unverified checks stay visible as gaps; the final review's supported findings
and the existing finding policy determine progression. A passing receipt does not
establish complete defect recall, and an unavailable runtime must not be described
as successful verification.

Validate improvements on fresh, independently curated cases and measure exact
mechanism detection, false claims, unresolved checks and execution/model cost before
changing defaults. The development tests use generic contracts and fake reviewers;
they do not establish a model-quality improvement.
