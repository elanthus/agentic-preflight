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

See the [validation tiers](COMPATIBILITY.md#validation-tiers) for CI coverage and frequency.

By submitting a contribution, you agree that it is licensed under the repository's
Apache License 2.0.

## Changelog fragments

For each behavior or configuration change, add a unique file under
`docs/CHANGELOG.d/`, named `<issue-or-slug>.<category>.md`, such as
`198.removed.md` or `changelog-fragments.changed.md`. Categories are `added`,
`changed`, `deprecated`, `removed`, `fixed`, and `security`. Write Markdown bullets
without headings or standalone prose. Indent continuation lines by at least two
spaces; blank lines between bullets are allowed. Use separate files for separate categories, and a descriptive
suffix when multiple PRs address one issue.

Routine PRs leave `CHANGELOG.md` alone. Review fragments with their change. Existing
Unreleased entries remain until the next release. The release command combines them
with fragments and removes consumed files. Commit the assembled changelog and
deletions together; see [Releasing](docs/RELEASING.md#cutting-a-release).

## Risk and human review

This repository classifies ordinary package code, tests, and skill documentation as
medium risk. Those changes still run the normal review, documentation, lint, test, and
attestation checks. Another rule can still classify them as requiring human review,
such as a high or critical review finding (even after it is fixed); the current branch
rules do not make human approval a merge requirement.

The explicit `human_review_paths` in `.agentic-preflight.toml` cover decision points:
policy and ownership, CI workflows, merge approval, attestation verification, the
pre-push hook, publication gates, and security guidance. Dependency manifest and
lockfile changes rely on the required automated dependency audit.
Keep `.github/CODEOWNERS` aligned with that list: a blanket package owner would restore
mandatory human review when Code Owner review is enforced. New code that decides whether
a change may merge or be published, or whether an attestation is accepted, needs an
explicit entry in both files. Code that decides only whether earlier evidence can be
reused, such as fingerprints and refresh validation, is medium risk like other shared
helpers, including configuration and data models. This is a review-prioritization policy,
not an exhaustive security boundary over every dependency.

Routine documentation edits, including skill instructions and compatibility notes,
receive the normal review and validation without requiring owner approval solely because
of their path. Corrections and examples that accurately describe already reviewed
implementation are routine. `skill/SKILL.md` remains the authority for agent approval,
review, recovery, and cleanup rules; keep its routing links intact.

Changes that grant or expand authorization, alter approval enforcement, or introduce or
change bypass rules require the configured owner's approval of the final PR content,
including when the change only edits instructions. That approval is a review of the final
head, or of an earlier head that the hosted approval check reports as carried
(`approval_carried`) because only a clean merge from the base or a clean rebase followed. Identify this requirement in the PR
and wait for that approval before merging. Path classification cannot understand prose:
this contributor checkpoint applies even when the automated risk verdict is `clear`.
Reviewers must also report unsafe instruction changes through the normal findings flow;
high or critical findings retain human review even after they are fixed.

The hosted approval check reads the policy from the protected base. A PR changing this
policy is evaluated using the configured review policy; the narrower rules take effect
for subsequent PRs after it merges. CODEOWNERS routes review requests. The current branch
rules require automated CI and audits, without mandatory human approval.
