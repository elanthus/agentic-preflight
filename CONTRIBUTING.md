# Contributing

Thanks for helping improve Agentic Preflight. Bug reports, focused feature proposals,
documentation fixes, and code contributions are welcome.

## Before opening a change

- Search existing issues and pull requests to avoid duplicate work.
- Open an issue before a large behavioral change so the approach can be agreed first.
- Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md), not in
  a public issue.

## Development setup

Agentic Preflight supports the Python and operating-system combinations listed in
[COMPATIBILITY.md](COMPATIBILITY.md), Git 2.38 or newer, and `uv`.

```bash
git clone https://github.com/elanthus/agentic-preflight.git
cd agentic-preflight
uv sync --group dev
```

Run the same checks listed in [AGENTS.md](AGENTS.md):

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
```

CI also enforces an 85% coverage floor. Run
`uv run pytest --cov=agentic_preflight --cov-report=term-missing` to check it locally.

The test suite uses temporary real Git repositories. Tests that exercise pushes and
worktrees can take longer than ordinary unit tests and require a working Git binary.

## Pull requests

This repository uses Agentic Preflight on its own changes. Install it (see
[docs/installation.md](docs/installation.md)), run `agentic-preflight init` in your
clone to install the pre-push hook (it keeps the committed configuration), and complete
a run before you push. The push publishes the attestation with your branch. The
`trusted preflight attestation` CI job verifies it on the pull request head, as described
in
[Portable attestations and CI enforcement](docs/attestations-and-ci.md#required-github-check),
so a branch pushed without a completed run fails that check.

Keep changes focused and include tests for observable behavior. Update user-facing
documentation whenever a reader following the current instructions would otherwise be
wrong. In the pull request, explain the problem, the chosen approach, and the checks
you ran. All CI checks, including the 85% coverage floor and built-wheel smoke test,
must pass before merge.

Pull-request CI intentionally uses only Ubuntu and Python 3.13. The oldest supported
macOS/Python boundary runs every Monday and Thursday, and the full nine-combination matrix runs
manually and on release tags; see [COMPATIBILITY.md](COMPATIBILITY.md).

By submitting a contribution, you agree that it is licensed under the repository's
Apache License 2.0.

## Risk and human review

This repository classifies ordinary package code, tests, and CLI reference material as
medium risk. Those changes still run the normal review, documentation, lint, test, and
attestation checks. They require human approval only if another rule raises their risk,
such as a high or critical review finding (even after it is fixed).

The explicit `human_review_paths` in `.agentic-preflight.toml` cover policy and ownership,
CI workflows and templates, direct approval and evidence-verification decisions,
publication gates, packaging and dependencies, and agent approval/recovery instructions.
Keep `.github/CODEOWNERS` aligned with that list: a blanket package owner would restore
mandatory human review when Code Owner review is enforced. New code that decides whether
evidence is valid, a stage may be skipped, or publication/merge may proceed needs an
explicit entry in both files. Shared implementation helpers are medium risk; this is a
review-prioritization policy, not an exhaustive security boundary over every dependency.

Routine CLI flags, fields, and examples belong in `skill/reference/commands.md` and
`skill/reference/workflow.md`. The protected `skill/SKILL.md` remains the authority for
agent approval, review, recovery, and cleanup rules; its routing links must remain intact.
Update it when those rules change, rather than for every CLI documentation update.

The hosted approval check reads the policy from the protected base. A PR changing this
policy still needs approval through the `high-risk-review` Environment; the narrower
rules take effect for subsequent PRs after it merges. Keep the required CI and Code
Owner checks enabled.
