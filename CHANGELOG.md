# Changelog

All notable changes to Agentic Preflight are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- Tag-triggered release workflow that publishes to PyPI using Trusted Publishing, so
  no long-lived API token is stored in the repository or in GitHub secrets. The upload
  is bound to a `pypi` GitHub Environment and waits for reviewer approval, and the
  build refuses to proceed when the pushed tag does not match the project version.
- `docs/RELEASING.md`, covering the one-time PyPI publisher registration, the GitHub
  environment setup, and the per-release checklist.

### Changed

- Documentation-only and CI-configuration-only changes now record the software test
  stage as explicitly skipped instead of requiring a test command to run.

### Fixed

- The source distribution is now built from an explicit allowlist. It previously
  included everything not matched by the root `.gitignore`, which made the published
  artifact depend on the contents of the local working tree and could fail the build
  outright when a stray virtual environment was present.

## [0.2.0] - 2026-07-23

First tagged pre-release.

### Added

- Deterministic review, test, documentation, lint, merge-back, and push workflow.
- Advisory pre-push hook keyed to the exact verified commit.
- Manual gate mode for workflows where only a person may perform the final push.
- Codex and Claude Code skill installation.
- In-place, reusable, and strict validation checkout modes.
- GitHub pull request publishing and CI monitoring through the `gh` CLI.
- Explicit CI coverage for Python 3.11 through 3.13 on macOS and Linux.

### Changed

- Renamed every pre-release product surface from `agentic-cli` to
  `agentic-preflight`, including the Python package, executable, skill, configuration,
  state directories, and internal validation branch prefix.
- Reframed the product as an advisory guard against accidental unverified pushes.
- Added a 60-second README walkthrough with command output captured from a local demo.

### Supported platforms

- macOS
- Linux

Windows is not supported because the implementation requires `fcntl`, Bash, and POSIX
process groups.

[0.2.0]: https://github.com/elanthus/agentic-preflight/releases/tag/v0.2.0
