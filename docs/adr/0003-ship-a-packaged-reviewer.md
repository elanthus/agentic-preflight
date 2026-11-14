# ADR 0003: Ship a packaged command reviewer that shells out to provider CLIs

- Status: Accepted
- Date: 2026-11-13

## Context

Review has two executors.
With `executor = "in_harness"`, the coding agent reviews the change in its own session.
With `executor = "command"`, `review run` starts a command that the repository configures.
That command reads the review context on stdin and prints a strict submission on stdout.

Until this decision, the only model-calling code was a set of example wrappers in `docs/examples/reviewers/`.
Users copied them into their repositories.
A copied wrapper cannot be tested, versioned, or improved in one place.
A quality claim about the reviewer cannot be tied to the code that a user actually runs.

The README said: "The core CLI calls no model."
That statement had two parts.
The workflow commands do not call a model.
The package does not contain code that calls a model.

Issue #364 proposes a supported reviewer with its own tests, a versioned prompt, and published precision and recall.
The first step moves the wrappers into the package without a change in behavior.

## Decision

Ship the reviewer in the package as `agentic_preflight/reviewers/`.
Expose it as `agentic-preflight reviewer run --provider claude|codex`.
A repository selects it with `[review] executor = "command"` and `command = "agentic-preflight reviewer run --provider claude"`.

Keep this dependency policy:

- The core dependencies do not include a model SDK.
- The reviewer shells out to the provider's own CLI (`claude` or `codex`), as the wrappers did.
  The user installs, signs in to, and pays for that CLI.
- A later change can put an SDK behind an optional extra. It must not add one to the core dependencies.

Keep the meaning of "the core CLI calls no model" for the workflow:

- No workflow command calls a model.
- `review run` calls a model only when the repository configures a command reviewer that does so.
- `reviewer run` calls a model through the provider CLI only when `review run` (or a user) starts it.

`reviewer run` prints no JSON envelope.
`review run` reads its stdout as the strict submission, and its stderr as the failure diagnostic.
It is not an agent-facing command, in the same way as `hook-check`.
A usage error keeps Click's plain output and exit code 2.

Keep `docs/examples/reviewers/` as thin pointers to the package for one release.
The removal is listed in the [compatibility removal plan](../maintainers/compatibility-removal-plan.md).

The first step changes no behavior.
The prompts, the provider CLI arguments, the environment variables, the defaults, the events file, and the output are the same as in the wrappers.
Commands in the `reviewer` group do not check the Git version, because the reviewer does not run Git.
It reads and writes its standard streams as UTF-8, as every other command does; the wrappers used the locale encoding.

## Consequences

The reviewer can have unit tests that use a fake provider CLI, without a network or a model.
CI still runs no model.
A real-mode evaluation still requires `AP_EVAL_AUTHORIZED=1`.

The package now contains code that starts a model through a provider CLI.
The README says this explicitly instead of saying that the package calls no model.

A change to the reviewer prompt now changes the reviewer for every user who upgrades.
Later steps of #364 record the reviewer version and prompt digest in the review stage evidence, so that an attestation identifies the reviewer.
That step must first check [the fingerprint contract](../fingerprint-contract.md).

A repository that runs the packaged reviewer gets it from the installed package, not from its own tree.
Its `human_review_paths` therefore cannot cover a change to the reviewer.
The installed version of Agentic Preflight decides which reviewer runs.
