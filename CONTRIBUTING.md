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

## Changelog fragments

For each behavior or configuration change, add a unique file under
`docs/CHANGELOG.d/`, named `<issue-or-slug>.<category>.md`, such as
`198.removed.md` or `changelog-fragments.changed.md`. Categories are `added`,
`changed`, `deprecated`, `removed`, `fixed`, and `security`. Write Markdown bullets
without headings. Use separate files for separate categories, and a descriptive
suffix when multiple PRs address one issue.

Routine PRs leave `CHANGELOG.md` alone. Review fragments with their change. Existing
Unreleased entries remain until the next release. The release command combines them
with fragments and removes consumed files. Commit the assembled changelog and
deletions together; see [Releasing](docs/releasing.md#cutting-a-release).
