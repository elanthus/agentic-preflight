# Contributor guidance

This file describes how to work in the Agentic Preflight repository. Human contributors
should also read [CONTRIBUTING.md](CONTRIBUTING.md).

## Setup

Install `uv` and Git 2.38 or newer, then install the development environment:

```bash
uv sync --group dev
```

## Checks

Run all four before reporting a change as done:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
```

The test suite creates temporary Git repositories, so it needs a working `git` binary and
can take a few minutes.

## Approval readiness

For changes requiring human approval, treat approval of an exact PR head as the final
readiness step. Routine documentation that accurately reflects reviewed implementation
does not require owner approval solely because it edits skill or reference material.
Changes to authorization, approval enforcement, or bypass rules require the configured
owner to approve the exact final head, including instruction-only changes and changes
with a `clear` automated risk verdict. Identify that requirement in the PR and wait for
approval before merging; see CONTRIBUTING.md. For a batch,
choose the merge order, complete preceding merges, and refresh the next PR to the
latest base before requesting approval. Do not request approval on every intermediate
push or batch approval for heads known to need further base updates.

Before requesting approval, complete requested, internal, and automated reviews,
consolidate their fixes, run applicable checks, push the final head, and wait for CI
and expected reviews to finish. A rate-limited review is still pending. Immediately
before the request, check unresolved comments and confirm the current head and base.

After approval, avoid optional changes. If a necessary fix or base change moves the
head, revalidate. A clean merge from the base or a clean rebase keeps the approval when
the hosted check reports `approval_carried`; otherwise explain why fresh review is needed. Never bypass the policy or
approve your own work. Chat authorization to merge permits the operation; it does
not replace the approval required by GitHub policy.

## Layout

- `agentic_preflight/`: the package. Top-level modules hold configuration (`config.py`),
  the state machine (`machine.py`), the run store (`store.py`), Git helpers (`gitx.py`),
  context grounding (`grounding.py`), the pre-push hook (`hook.py`), and the CLI
  (`cli.py` plus the `cli_*.py` command families).
- `agentic_preflight/runs/`: run lifecycle application code, including `start.py`,
  review, stages, merge-back, and publish.
- `agentic_preflight/stages/`: the docs, lint, and test stage implementations and change
  scope detection.
- `agentic_preflight/publish/`: the publish gate.
- `agentic_preflight/templates/`: CI workflow templates written into user repositories.
- `tests/`: pytest suite. `tests/driver.py` and `tests/conftest.py` hold shared fixtures.
- `evals/`: the regression eval runner and its cases; see `evals/README.md`.
- `docs/`: user and design documentation, including ADRs under `docs/adr/`.
- `skill/`: the agent skill (`SKILL.md`) and its reference files under `skill/reference/`.

## Docs that change with code

Update documentation in the same commit as the code it describes:

- Every behaviour or configuration change gets a unique Markdown fragment in
  `docs/CHANGELOG.d/` in the same commit. Use `<issue-or-slug>.<category>.md`
  (for example `198.removed.md`), with category `added`, `changed`, `deprecated`,
  `removed`, `fixed`, or `security`, and Markdown bullets without headings.
  Routine PRs do not edit `CHANGELOG.md`; release assembly consumes the fragments.
  See [CONTRIBUTING.md](CONTRIBUTING.md#changelog-fragments).
- Configuration keys are documented in `docs/configuration.md`.
- CLI commands, flags, envelopes, and exit codes are documented in
  `skill/reference/commands.md`; update `skill/reference/workflow.md` when the walkthrough
  changes. Update `skill/SKILL.md` when approval, review, recovery, or cleanup rules change.
- Schema or configuration compatibility changes are recorded in `COMPATIBILITY.md`.
- `tests/test_docs_match_cli.py` pins the skill, README, and configuration docs against
  the CLI and config model. If it fails, fix the docs rather than the test unless the
  pinned fact itself changed.
