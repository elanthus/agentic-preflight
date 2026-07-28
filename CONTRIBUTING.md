# Contributing

Thanks for helping improve Agentic Preflight. Bug reports, focused feature proposals,
documentation fixes, and code contributions are welcome.

## Before opening a change

- Search existing issues and pull requests to avoid duplicate work.
- Open an issue before a large behavioral change so the approach can be agreed first.
- Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md), not in
  a public issue.

## Development setup

Agentic Preflight supports macOS and Linux with Python 3.11 or newer, Git 2.30 or
newer, and `uv`.

```bash
git clone https://github.com/elanthus/agentic-preflight.git
cd agentic-preflight
uv sync --group dev
```

Run the same core checks used by CI:

```bash
uv run ruff check agentic_preflight tests
uv run mypy agentic_preflight
uv run pytest --cov=agentic_preflight --cov-report=term-missing
```

The test suite uses temporary real Git repositories. Tests that exercise pushes and
worktrees can take longer than ordinary unit tests and require a working Git binary.

## Pull requests

Keep changes focused and include tests for observable behavior. Update user-facing
documentation whenever a reader following the current instructions would otherwise be
wrong. In the pull request, explain the problem, the chosen approach, and the checks
you ran. All CI checks, including the 85% coverage floor and built-wheel smoke test,
must pass before merge.

By submitting a contribution, you agree that it is licensed under the repository's
Apache License 2.0.
