# Changelog

All notable changes to Agentic Preflight are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- Worktree-scoped active-run ownership. Multiple agents can now run independent gates
  concurrently from linked worktrees in one clone. `status --all` inventories the clone,
  and the global `--run RUN_ID` selector provides explicit recovery without making the
  caller's unrelated checkout the mutation target.

### Changed

- Repeating `start` with matching inputs now resumes the active worktree run. A moved
  source head is preserved as `ORPHANED` and replaced automatically; a different intent
  on an unchanged head requires `start --replace`. Terminal, dangling, and abandoned
  ownership pointers no longer block later work, and elapsed time is never used as proof
  of abandonment.
- Clone-wide synchronization is limited to the resources that are actually shared: base
  synchronization, the single reusable runner, and Git-note publication. Per-run commands
  and source-worktree ownership no longer serialize unrelated PR preparation.

## [0.5.0] - 2026-08-27

### Added

- Native Windows support. Windows 10 or newer joins macOS and Linux as a supported
  platform, is covered by the pull-request CI matrix, and installs with the new
  `install.ps1` / `uninstall.ps1` scripts. WSL is no longer required.

- `install.ps1` and `uninstall.ps1`, PowerShell counterparts to the bash installers
  with identical behaviour and ordering, including the deliberate pause before
  uninstalling so repository state can be cleaned up while the skill still exists.

### Changed

- Stage, review, and setup commands are now executed directly as a program and its
  arguments when they contain no shell grammar. A shell is used only for commands
  that need one: pipes, `&&`, redirection, globs, expansions, variable assignments,
  or a program that does not resolve. This removes the hard dependency on a POSIX
  shell for the common case, and takes the shell out of the injection surface of the
  one code path that runs repository-controlled strings.

  **A consequence worth reading before upgrading.** A directly executed command does
  not source your login shell profile, where `bash -lc` did. Where a version manager
  — `nvm`, `pyenv`, `rbenv`, `mise`, `asdf` — puts its shims on `PATH` from that
  profile, a stage can now run a *different build of the same program*. Which case
  you are in depends on whether the program resolves without the profile:

  - **Not on `PATH` without it.** Resolution fails, the command falls back to a
    shell, the profile is sourced, and nothing changes.
  - **On `PATH` without it, and the same program.** Nothing changes.
  - **On `PATH` without it, but the profile would have selected a different one.**
    The system build now runs instead of the managed one, and says nothing about it.
    A suite can go green or red against an interpreter version you did not intend.

  Only the third case is a behaviour change, and it is silent. If a stage depends on
  a version manager, select the interpreter in the command itself — `uv run pytest`,
  `mise exec -- pytest`, or an absolute path — rather than relying on the profile.
  That also makes the stage behave the same way in CI, where no profile is sourced
  either.

  Program *resolution* is likewise now PATH-only: a bare command name is never
  looked up in a working directory, so a repository cannot supply the tool that
  validates it. This closes a Windows-specific hole, where `shutil.which` searches
  the calling process's current directory — the repository under validation — before
  `PATH`.

  On Windows, the shell fallback is the one Git for Windows installs, located through
  the Git installation rather than `PATH`, because `bash.exe` on `PATH` is normally
  the WSL launcher and would run stages against a different filesystem.

- All file and subprocess text is now read and written as UTF-8 explicitly rather
  than in the platform's default encoding, and generated files use Unix line endings.
  Under the Windows default of `cp1252`, a non-ASCII path or review finding could
  previously corrupt git output or raise `UnicodeEncodeError`.

### Fixed

- Restored attestation reuse on Git 2.30 through 2.37. Those releases predate
  `merge-tree --write-tree` and report the unknown flag on stderr *while exiting
  zero*, so the fallback written for them was never reached: `merge_tree` returned
  "no clean merge" for every comparison, and `start` silently reopened review instead
  of reusing a still-valid green attestation after a base synchronization. The
  interface is now chosen from the reported Git version rather than by recognising an
  error message, which also stops the detection breaking under a non-English locale
  where Git's messages are translated.

- Copied `[worktree] copy_files` entries are now restricted to their owner on Windows
  using an ACL, as `os.chmod` there does not affect permissions. A copy that cannot be
  restricted is deleted and refused rather than left readable, so the guarantee that
  makes copying a local `.env` acceptable is never silently unmet.

- Replacing a run document no longer fails when another process briefly holds it open,
  which POSIX `rename` permits but Windows does not. The replace is retried with
  backoff and still raises if the file stays held, so a lost write cannot pass as a
  recorded state transition.

- Git output is now decoded with `backslashreplace`, so one changed file in a legacy
  encoding renders as visible `\xe9`-style escapes in the diff instead of failing the
  whole run with `UnicodeDecodeError`. Patch identities are exempt from decoding:
  the patch travels between `git show` and `git patch-id` as raw bytes, so two
  byte-distinct changes cannot share an identity through escape-text folding.

- Paths changed by a single commit are read NUL-delimited, so a non-ASCII filename is
  reported as itself rather than C-quoted under git's default `core.quotePath` — a
  form that names a file that exists nowhere.

- The git executable itself is resolved on PATH once per process. Handed a bare name,
  Windows' `CreateProcess` searches the parent's current directory first — the
  repository under validation — so a repo-committed `git.exe` could previously have
  become the git that validated the repository shipping it. A git missing from PATH
  entirely now raises an explicit `FileNotFoundError` rather than falling back to the
  bare name, which would have reopened the same search.

## [0.4.0] - 2026-08-13

### Changed

- Batched per-file review diff collection into bounded Git invocations instead of
  launching one process for every retained file. Review bundles keep the same exact
  per-file patches and handle renames, binary changes, file-type changes, non-ASCII
  names, and literal pathspec characters.

- Removed automatic runtime-manager discovery and command wrapping. Setup, review,
  lint, and test commands now run directly in the configured environment, and the
  `[runtime]` configuration section is no longer supported.

- Replaced Node-specific automatic npm/pnpm installation and reusable dependency
  fingerprinting with the language-neutral `[worktree] setup_command`. Setup now fails
  explicitly on a nonzero exit, including in baseline scratch worktrees, where an
  installation failure can no longer be mistaken for a pre-existing red base. The run
  persists setup failure details so `status` returns either the required abort or the
  exact baseline-aware stage retry instead of a dead-end state or nonexistent log.

- Simplified the review and documentation findings sub-machines. Clean and
  blocking submissions now transition directly to each stage's green or blocked
  state, and response handling remains in that single blocked state until every
  blocking finding is resolved. The transient `*_SUBMITTED` states and the
  behaviorally identical `*_AWAITING_RESPONSES`/`*_FIXING` pairs were removed.

- Moved the skill's ten failure playbooks out of `SKILL.md` into
  `reference/playbooks.md`, leaving a symptom-to-playbook index in their place.
  The playbooks are recovery detail an agent needs only after a run stops, and they
  were being loaded into context on every invocation instead. The universal
  `exit 3 → status → next` rule and the merge-back non-negotiable stay in the skill
  body, and a test keeps the index covering every playbook.

- Corrected the README's descriptions of explicit stage skips, observable gaps,
  hosted merge-approval enforcement, and manual gate boundaries. Expanded the
  `no-mistakes` comparison against v1.48.0 to cover review completeness, portable
  evidence, deterministic risk, publication approval, and local architecture.

## [0.3.0] - 2026-08-12

### Added

- Supply-chain maintenance and verification: Dependabot updates Python and GitHub
  Actions dependencies, locked runtime dependencies are audited for published
  vulnerabilities, CodeQL performs Python static analysis, and release artifacts ship
  with a CycloneDX SBOM plus signed build-provenance and SBOM attestations.
- A compatibility policy and Monday/Thursday regression coverage for the oldest
  supported boundary, macOS 15 with Python 3.11. Pull-request CI remains the fast
  Ubuntu/Python 3.13 combination, while manual and release runs retain the broad
  six-way matrix.

- Configurable, attestable review independence. `[review] executor = "command"` runs an
  external reviewer over the same complete bundle returned by `context`, while
  `require_command_for` can require that executor for selected risk levels. Command
  failures have bounded, restart-safe retries, and attestation schema v3 records the
  executor plus redacted command evidence.

- Snapshot-bound review coverage. `context` inventories every changed hunk plus
  non-textual file changes; review submissions must assert examination of that exact
  manifest, findings cite units, and code derives a complete cited/clean receipt.
  Any repair commit invalidates coverage and reopens review before validation continues.

- Conservative rebase tolerance for in-place runs. `start` preserves a prior green
  attestation only when the rewritten commit has an identical complete tree, effective
  configuration, and clean Git merge result against the freshly synchronized base.
- Managed Agent Skill integrations for Cursor, opencode, and Amp, alongside Codex and
  Claude Code.

- An idempotent `install.sh` for installing or updating the CLI and all managed agent
  skill copies directly from a source checkout.
- A conservative `uninstall.sh` that pauses for per-repository
  `agentic-preflight:uninstall` cleanup before removing managed user-scoped skills and
  the CLI. Project cleanup removes configuration and managed hook logic while preserving
  unrelated hooks, run history, and attestations.
- `[pr] mode = "auto" | "manual"`, defaulting to automatic pull-request creation after
  the user approves the push gate. Manual PR mode leaves creation to the user and reports
  a compare URL instead.
- `[approval] mode = "manual_merge" | "environment" | "peer_review"` for high-risk
  pull requests. Manual merge is the default, Environment mode uses a configurable
  GitHub Environment, and peer review retains the exact-head eligible-reviewer policy.
  The trusted check rejects GitHub auto-merge while manual-merge mode is active.
- Deterministic path-based risk classification with separate low, medium, and high
  levels. High risk invokes the configured merge-approval mode, while publication still
  uses the normal confirmation-token gate. Risk remains separate from the diff byte
  budget.
- A pull-request attestation job that runs the verifier from the protected base commit
  and fetches attestations from same-repository branches or public forks. Governance
  surfaces now have a checked-in CODEOWNERS policy.
- A trusted high-risk approval check that dispatches manual-merge, Environment, and peer
  review modes. Peer review rejects bot, unaffiliated, self, stale-head, dismissed, and
  superseded approvals.
- Green records are now portable Git-note attestations in
  `refs/notes/agentic-preflight`. Lint and test evidence includes the exact command,
  exit code, and SHA-256 of redacted captured output, and the normal push path sends
  the branch and attestation ref atomically.
- `agentic-preflight verify <sha>` provides a fail-closed CI check for the note's
  schema, exact commit/tree binding, complete stages, and shell-stage evidence.

### Changed

- CI and release workflows now use `actions/checkout` 7.0.1,
  `actions/upload-artifact` 7.0.1, `actions/download-artifact` 8.0.1, and
  `pypa/gh-action-pypi-publish` 1.14.2. Development dependency floors now require
  Hypothesis 6.165.2 and Ruff 0.16.2, with the lockfile updated accordingly.

- Dependabot version updates are grouped into one weekly Python dependency pull
  request and one weekly GitHub Actions pull request, reducing preflight review and
  attestation overhead without exempting bot-authored commits from the gate. The
  attestation and CI guide documents how to attest the exact bot-authored commit and
  re-run its hosted checks.

- The README now matches the 0.3.0 release surface, uses release-stable links when
  rendered on PyPI, and keeps only the quickstart and core safety contract. Detailed
  hook behavior, portable attestations and CI, configuration, and extended limitations
  live in focused linked guides that are included in the source distribution.
- An explicit request to push, publish, or create/open a pull request now authorizes the
  matching post-verification push. The gate still displays the exact remote, branch,
  commits, and risk summary, but no longer forces a duplicate confirmation unless that
  summary differs materially from what the user authorized.
- Code-owned mechanical findings now block regardless of reviewer severity policy, so
  `[docs] require_changelog = true` cannot be silently weakened by excluding `high`
  from `[docs] blocking_severities`.
- Local validation now runs review → docs → lint → test, leaving the potentially
  expensive test command until all review, documentation, and mechanical lint repairs
  are committed. A lint-only repair therefore cannot force an otherwise unnecessary
  second test run; committed test repairs still invalidate docs and lint before retry.
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
  the complete configuration example stays in `docs/configuration.md`, where the test
  suite requires every config section to appear.
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

- The `ap` console-script alias. The supported executable is `agentic-preflight`.
- Built-in GitHub pull-request creation, CI monitoring, and destructive post-merge
  cleanup. The skill now hands these host-level tasks to `gh`; the core retains the
  quality gate, atomic branch-and-attestation push, `finish`, and `gc`.
- The `pr`, `ci`, and `cleanup` commands and their `[publish]` and `[ci]`
  configuration sections.
- `docs/plans/` and the design document it held. It described the architecture as it was
  being decided rather than as it now is, and the parts still worth reading have since
  been said in the README and the `docs/` pages. It remains in git history.

### Fixed

- Cross-stage lint/test repair cycles preserve each stage's failed-attempt counter, so
  alternating repairs cannot reset and evade the configured `max_attempts` stop.
- Attestation reuse now names both mandatory terminal outcomes: lint must be green and
  test must be green or explicitly skipped.

- Synchronizing attestations now preserves locally-ahead and disjoint remote note
  histories. A valid local attestation no longer makes the next preflight start fail
  with a non-fast-forward fetch; conflicting notes for the same commit still fail loud.
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

[0.5.0]: https://github.com/elanthus/agentic-preflight/releases/tag/v0.5.0
[0.4.0]: https://github.com/elanthus/agentic-preflight/releases/tag/v0.4.0
[0.3.0]: https://github.com/elanthus/agentic-preflight/releases/tag/v0.3.0
[0.2.1]: https://github.com/elanthus/agentic-preflight/releases/tag/v0.2.1
[0.2.0]: https://github.com/elanthus/agentic-preflight/releases/tag/v0.2.0
