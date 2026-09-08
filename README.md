# agentic-preflight

[![CI](https://github.com/elanthus/agentic-preflight/actions/workflows/ci.yml/badge.svg)](https://github.com/elanthus/agentic-preflight/actions/workflows/ci.yml)
[![Coverage](https://raw.githubusercontent.com/elanthus/agentic-preflight/badges/coverage.svg)](https://github.com/elanthus/agentic-preflight/tree/badges)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://github.com/elanthus/agentic-preflight/blob/v0.5.3/pyproject.toml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](https://github.com/elanthus/agentic-preflight/blob/v0.5.3/LICENSE)

**Stop your coding agent from pushing unverified work.**

Agentic Preflight guides the coding agent already working in your repository through
review, documentation, lint, and test gates. It records the result against the exact
commit, then uses a pre-push hook to block publication when that evidence is missing or
stale. The core CLI calls no model and needs no model API key.

Interested in the design? See the
[project evidence and engineering notes](docs/portfolio-review.md).

![A push blocked by the pre-push hook, a review that catches an unguarded division by
zero, the fix verified, and the gate stopping to ask before it pushes](docs/demo.gif)

## Quickstart

You need Git 2.30+, Python 3.11 through 3.13, and
[`uv`](https://docs.astral.sh/uv/getting-started/installation/). From any directory:

```bash
uv tool install agentic-preflight
agentic-preflight integrations install codex claude cursor opencode amp
```

Install only the coding-agent integrations you use. Then initialize each repository
you want to protect:

```bash
cd your-repo
agentic-preflight init
git add .agentic-preflight.toml
git commit -m "Configure Agentic Preflight"
```

`init` writes a commented configuration file and installs an advisory pre-push hook.
Review the generated configuration before starting a run because its setup, lint, test,
and optional command-review entries execute with your user privileges.

Make and commit your product change, then ask your coding agent to invoke
`$agentic-preflight` in Codex or `/agentic-preflight` in Claude Code. The installed skill
drives the run, presents any findings, and follows each command returned by the CLI.

The [installation guide](https://github.com/elanthus/agentic-preflight/blob/v0.5.3/docs/installation.md)
covers source installs, upgrades, project-scoped skills, other Agent Skills clients,
and removal.

If you try to push the changed commit before its run is green, the hook stops the push
and tells you to invoke the installed skill. After verification, the agent shows the
target remote, branch, commits, and risk before publication. An explicit request or
applicable standing user instructions can authorize the matching push, including PR
feedback fixes on the existing head branch when those instructions permit it. The agent
asks only when authorization is missing or the scope materially differs. Set `[gate]
mode = "manual"` when only a person should run the final Git command.

## What happens during a run

Agentic Preflight moves the change through one ordered workflow:

```text
review -> documentation -> lint -> test -> merge-back -> gate -> push
```

At the start of a run, the agent supplies your objective and acceptance criteria. When
`origin` exists, Agentic Preflight fetches it and rebases the validation checkout onto
the fresh base before review. Every agent-facing command returns one JSON object with
the single next legal command, so interrupted work can resume from recorded state.

The stages provide different checks:

- **Review** inventories the changed hunks and non-text files, then requires the reviewer
  to account for the complete snapshot. Findings remain recorded with their disposition.
- **Documentation** checks configured documentation for claims made stale by the change.
- **Lint and test** run the commands configured for the repository and record their exit
  codes and output hashes. Documentation- and CI-only changes skip the software test
  command through an explicit recorded transition when test authority is local. With
  opt-in trusted CI test authority, publication proceeds with tests pending and the
  current integration commit must pass protected CI before merge.
- **Merge-back and approval** verify that the reviewed content matches the source branch,
  classify risk from repository policy and findings, and show the target remote, branch,
  and commits before publication.
- **Push** atomically publishes the branch and its Git-note attestation. Automatic PR mode
  then lets the agent create or reuse a GitHub pull request and monitor its checks; manual
  PR mode returns a compare URL instead.

Equivalent-content rebases can reuse stage evidence after a compatible verifier is
installed on the protected base. Shell stages also need committed input contracts;
see the [fingerprint contract](https://github.com/elanthus/agentic-preflight/blob/v0.5.3/docs/fingerprint-contract.md).

Pull-request merge polling and post-merge cleanup are disabled by default. Set
`[pr] automatedCleanup = true` to opt in.

Run `agentic-preflight status` in the source worktree to inspect or resume its run. Use
`agentic-preflight status --all` to inventory runs across linked worktrees.

## Choose a worktree mode

The default `in_place` mode validates in the current checkout. It suits a clean,
dedicated one-agent/one-PR worktree and can reuse that checkout's installed dependencies.

Set `[worktree] mode` to `reusable` or `strict` when validation should leave the source
checkout untouched. Isolated worktrees do not inherit `.venv`, `node_modules`, `.env`,
or other ignored files. Configure `setup_command` for dependencies and `copy_files` for
required ignored files. The
[worktree-modes guide](https://github.com/elanthus/agentic-preflight/blob/v0.5.3/docs/worktree-modes.md)
explains the tradeoffs and recovery behavior.

## Configure repository policy

Commit `.agentic-preflight.toml` at the repository root. It layers over the user-wide
`~/.config/agentic-preflight/config.toml`, with repository sections taking precedence.
Use it to set:

- the protected base branch and lint/test commands;
- blocking finding severities and independent command-review policy;
- documentation paths and generated-file exclusions;
- path-based risk and the required human-approval mode;
- in-place, reusable, or strict validation worktrees; and
- automatic or manual push and pull-request behavior.

Unknown keys are errors. Commit configuration changes before starting the run they
should affect; a run snapshots its resolved configuration. See the
[configuration reference](https://github.com/elanthus/agentic-preflight/blob/v0.5.3/docs/configuration.md)
for every option and a complete example.

> **Warning:** A repository can configure setup, lint, test, and independent-review
> commands that execute with your privileges. Run Agentic Preflight only in repositories
> whose code and build commands you are willing to execute.

## Use attestations in CI

A successful merge-back writes a versioned JSON attestation as a Git note on the exact
commit. The note records review coverage, finding dispositions, stage results, policy,
and the inputs that determine whether evidence still applies after a history rewrite.

The local hook checks the commit being pushed. A protected-base GitHub workflow can also
verify the note and enforce the configured high-risk approval mode before merge. This
requires forge configuration; committing `.agentic-preflight.toml` alone does not change
branch protection. See
[Portable attestations and CI enforcement](https://github.com/elanthus/agentic-preflight/blob/v0.5.3/docs/attestations-and-ci.md)
for setup and verification commands.

## Limits

**Agentic Preflight is an advisory quality gate, not a security boundary.**

- A person can bypass the local hook with `git push --no-verify`.
- The push-confirmation token is deliberate ceremony, not a secret. It prevents an
  accidental tool-driven push but does not stop an agent with shell access from invoking
  Git directly.
- A green record proves what the configured gate reported. It does not prove that the
  reviewer understood the change, and it replaces neither hosted CI nor human review.
- Git notes are mutable. Anyone allowed to update the notes ref can replace an
  attestation.

Set `[gate] mode = "manual"` if the CLI must refuse to perform the final push itself.
Read the [limits guide](https://github.com/elanthus/agentic-preflight/blob/v0.5.3/docs/limits.md)
before relying on attestations or evidence reuse for policy enforcement.

## Requirements

- A supported macOS, Linux, or Windows and Python combination from the
  [compatibility policy](https://github.com/elanthus/agentic-preflight/blob/v0.5.3/COMPATIBILITY.md)
- Git 2.30+
- A POSIX shell when a configured command needs shell interpretation or its program
  cannot be resolved directly; on Windows, Git for Windows provides it
- `gh` when the agent will create pull requests, inspect hosted checks, or verify merges

## Help and development

Use the [support guide](https://github.com/elanthus/agentic-preflight/blob/v0.5.3/SUPPORT.md)
for help and security-reporting routes. Contributors should start with the
[contributor guide](https://github.com/elanthus/agentic-preflight/blob/v0.5.3/CONTRIBUTING.md).

Agentic Preflight is Apache 2.0 licensed. It was created by
[@elanthus](https://github.com/elanthus) with development contributions from OpenAI Codex
and Anthropic Claude.
