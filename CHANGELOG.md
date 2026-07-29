# Changelog

All notable changes to Agentic Preflight are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- Green records are now portable Git-note attestations in
  `refs/notes/agentic-preflight`. Lint and test evidence includes the exact command,
  exit code, and SHA-256 of redacted captured output, and the normal push path sends
  the branch and attestation ref atomically.
- `agentic-preflight verify <sha>` provides a fail-closed CI check for the note's
  schema, exact commit/tree binding, complete stages, and shell-stage evidence.

### Changed

- Pull requests and pushes to `main` now run one test combination, `ubuntu-latest` on
  Python 3.13, rather than the full six-way matrix. The matrix of Ubuntu and macOS
  against Python 3.11, 3.12, and 3.13 still runs on release tags, where it now gates
  the PyPI upload, and on demand by running the CI workflow manually from the Actions
  tab. Supported platforms and Python versions are unchanged; only the point at which
  every combination is exercised has moved.
- The test matrix now lives in a single reusable workflow that both the CI and release
  workflows call, rather than being copied into each. Contributors will see the test
  checks reported under a nested name, `test / test (ubuntu-latest, py3.13)`.
- The packaged description now states what the tool does rather than what it omits. The
  previous summary led with "Calls no LLM, ever", which is true of this package and
  misleading about a run, in which every judgment is produced by a model. PyPI renders
  that field as the project summary with none of the surrounding explanation, so it has
  to stand on its own. The README intro keeps the underlying point in the form that
  survives being read alone: no API key and no second model.
- The README now leads with what the tool does and defers the detail to linked pages.
  Reference material that a first-time reader does not need moved into `docs/`:
  `installation.md`, `change-scope.md`, `worktree-modes.md`, `configuration.md`, and
  `limits.md`. The prior-art comparison moved below the technical sections, since it
  answers a question a reader has not yet asked. No documented behaviour changed, and
  the configuration example stays in the README, where the test suite requires every
  config section to appear.
- The `[diff] exclude` example now says that setting the key replaces the eight built-in
  globs rather than extending them. The example lists four of the eight, so copying it
  verbatim silently dropped `**/*.min.css`, `**/__snapshots__/**`, `**/*.pb.go`, and
  `**/*_pb2.py`. The full default list is now in `docs/configuration.md`.
- The README now opens with a recorded demonstration of a run: a push blocked by the
  pre-push hook, a review that catches an unguarded division by zero, the fix verified,
  and the gate stopping to ask before pushing. The recording is real CLI output, and the
  VHS tape that produces it is committed as `docs/demo.tape`, alongside
  `docs/demo-fixture.sh`, which builds the throwaway repository the tape records
  against. `./docs/demo-fixture.sh && vhs docs/demo.tape` regenerates the recording from
  nothing, so the demonstration of a tool that verifies claims can itself be verified
  rather than taken on trust.

### Removed

- Built-in GitHub pull-request creation, CI monitoring, and destructive post-merge
  cleanup. The skill now hands these host-level tasks to `gh`; the core retains the
  quality gate, atomic branch-and-attestation push, `finish`, and `gc`.
- The `pr`, `ci`, and `cleanup` commands and their `[publish]` and `[ci]`
  configuration sections.
- `docs/plans/` and the design document it held. It described the architecture as it was
  being decided rather than as it now is, and the parts still worth reading have since
  been said in the README and the `docs/` pages. It remains in git history.

### Fixed

- Manual CI runs are no longer cancelled by other CI activity. Every run on a ref
  shared one concurrency group with `cancel-in-progress`, so a manual run of the full
  matrix could be superseded by a push to `main` or by a second manual run. That is
  the run the release process uses to check macOS and Python 3.11 and 3.12 before a
  tag exists, and a cancelled run reports neither pass nor fail. Manual runs now get
  a group of their own; pushes and pull requests still supersede older runs on the
  same ref.

## [0.2.1] - 2026-07-28

First release published to PyPI.

### Added

- Tag-triggered release workflow that publishes to PyPI using Trusted Publishing, so
  no long-lived API token is stored in the repository or in GitHub secrets. The upload
  is bound to a `pypi` GitHub Environment and waits for reviewer approval, and the
  build refuses to proceed when the pushed tag does not match the project version.
- `docs/RELEASING.md`, covering the one-time PyPI publisher registration, the GitHub
  environment setup, and the per-release checklist.
- Community health files: `CONTRIBUTING.md`, `SECURITY.md`, `SUPPORT.md`, issue
  templates, and a pull request template.
- Inline type information: the package now ships `py.typed`, so type checkers read its
  annotations directly.
- A coverage badge produced and published by CI without a third-party service, and a
  prior art section in the README.

### Changed

- Documentation-only and CI-configuration-only changes now record the software test
  stage as explicitly skipped instead of requiring a test command to run.
- The skill now instructs a `git pull --ff-only` on the base branch after a confirmed
  cleanup, so the source checkout matches the merged result.
- Attribution moved from the top of the README to a Credits section beside the license,
  and the package author is now the GitHub handle rather than a legal name.

### Fixed

- Skipped GitHub checks are no longer treated as failures. A pull request whose
  workflow correctly skips a job was previously reported as a CI failure.
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

[0.2.1]: https://github.com/elanthus/agentic-preflight/releases/tag/v0.2.1
[0.2.0]: https://github.com/elanthus/agentic-preflight/releases/tag/v0.2.0
