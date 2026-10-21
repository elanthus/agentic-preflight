# Contributing

Thank you for your help with Agentic Preflight.
Bug reports, focused feature proposals, documentation fixes, and code contributions are welcome.

## Before opening a change

- Search the existing issues and pull requests to prevent duplicate work.
- Open an issue before a large behavioral change, so the approach can be agreed first.
- Report vulnerabilities in private, as described in [SECURITY.md](SECURITY.md). Do not use a public issue.

## Development setup

Agentic Preflight supports the Python and operating-system combinations in [COMPATIBILITY.md](COMPATIBILITY.md).
It also requires Git 2.38 or newer and `uv`.

```bash
git clone https://github.com/elanthus/agentic-preflight.git
cd agentic-preflight
uv sync --group dev
```

Run the same checks that [AGENTS.md](AGENTS.md) lists:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
```

CI also enforces an 85% coverage floor.
To check it locally, run `uv run pytest --cov=agentic_preflight --cov-report=term-missing`.

The test suite uses temporary real Git repositories.
Tests that exercise pushes and worktrees can take longer than ordinary unit tests.
They require a working Git binary.

## Pull requests

This repository uses Agentic Preflight on its own changes.
Install it as described in [docs/installation.md](docs/installation.md).
Run `agentic-preflight init` in your clone to install the pre-push hook. The command keeps the committed configuration.
Complete a run before you push.
The push publishes the attestation with your branch.
The `trusted preflight attestation` CI job verifies the attestation on the pull-request head.
See [Portable attestations and CI enforcement](docs/attestations-and-ci.md#required-github-check).
An ordinary contributor branch that is pushed without a completed run fails that check.
Eligible Dependabot Python patch and minor updates use the documented hosted exception.
All verified Dependabot updates, including major versions and GitHub Actions, are exempt from human approval.
Updates outside the narrow Python exception still need preflight attestations and every required CI check.
See [bot provenance verification](docs/attestations-and-ci.md#dependabot-and-other-bot-authored-pull-requests).

Keep changes focused. Include tests for observable behavior.
Update user-facing documentation when a reader who follows the current instructions would otherwise be wrong.
In the pull request, explain the problem, the selected approach, and the checks that you ran.
All CI checks must pass before merge. This includes the 85% coverage floor and the built-wheel smoke test.

See the [validation tiers](COMPATIBILITY.md#validation-tiers) for CI coverage and frequency.

When you submit a contribution, you agree that it is licensed under the repository's Apache License 2.0.

## Changelog fragments

For each behavior or configuration change, add a unique file under `docs/CHANGELOG.d/`.
Name it `<issue-or-slug>.<category>.md`, for example `198.removed.md` or `changelog-fragments.changed.md`.
The categories are `added`, `changed`, `deprecated`, `removed`, `fixed`, and `security`.
Write Markdown bullets. Do not write headings or standalone prose.
Indent continuation lines by at least two spaces. Blank lines between bullets are permitted.
Use separate files for separate categories.
Use a descriptive suffix when several PRs address one issue.

Routine PRs do not edit `CHANGELOG.md`.
Review fragments with their change.
Existing Unreleased entries stay until the next release.
The release command combines them with the fragments and removes the consumed files.
Commit the assembled changelog and the deletions together.
See [Releasing](docs/RELEASING.md#cutting-a-release).

## Risk and human review

This repository classifies ordinary package code, tests, and skill documentation as medium risk.
Those changes still receive the normal review, documentation, lint, test, and attestation checks.
Another rule can still classify them as human review, for example a high or critical review finding, even after the finding is fixed.
The current branch rules do not make human approval a merge requirement.

The explicit `human_review_paths` in `.agentic-preflight.toml` cover the decision points:
policy and ownership, CI workflows, merge approval, attestation verification, the pre-push hook, publication gates, and security guidance.
Dependency manifest and lockfile changes rely on the required automated dependency audit.
Keep `.github/CODEOWNERS` aligned with that list.
A blanket package owner would restore mandatory human review when Code Owner review is enforced.
New code that decides whether a change can merge or be published, or whether an attestation is accepted, needs an explicit entry in both files.
Code that decides only whether earlier evidence can be reused, such as fingerprints and refresh validation, is medium risk.
So are other shared helpers, including configuration and data models.
This is a review-prioritization policy. It is not an exhaustive security boundary over every dependency.

Routine documentation edits, including skill instructions and compatibility notes, receive the normal review and validation.
They do not require owner approval because of their path alone.
Corrections and examples that accurately describe already reviewed implementation are routine.
`skill/SKILL.md` stays the authority for agent approval, review, recovery, and cleanup rules.
Keep its routing links intact.

Some changes require the configured owner's approval of the final PR content:
changes that grant or expand authorization, changes to approval enforcement, and changes that introduce or change bypass rules.
This applies when the change edits only instructions.
That approval is a review of the final head.
It can also be a review of an earlier head that the hosted approval check reports as carried (`approval_carried`).
A carried approval requires that only a clean merge from the base or a clean rebase followed the earlier head.
Identify this requirement in the PR. Wait for that approval before merge.
For a batch of PRs, select the merge order and complete the preceding merges first.
Then refresh the next PR to the latest base, and request approval only when it needs no further base update.
Do not request approval on every intermediate push.
Path classification cannot understand prose.
This contributor checkpoint applies even when the automated risk verdict is `clear`.
Reviewers must also report unsafe instruction changes through the normal findings flow.
High or critical findings keep human review even after they are fixed.

The hosted Dependabot provenance exemption is separate from these human-contributor review rules.
It verifies signed bot commits and clean protected-base merges instead of a human approval.
An unverified or human-modified bot PR falls back to the ordinary policy.
A change to this exemption itself requires owner approval.

The hosted approval check reads the policy from the protected base.
A PR that changes this policy is evaluated with the configured review policy.
The narrower rules take effect for subsequent PRs after the change merges.
CODEOWNERS routes review requests.
The current branch rules require automated CI and audits. They do not require human approval.
