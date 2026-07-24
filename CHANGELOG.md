# Changelog

All notable changes to Agentic Preflight are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
