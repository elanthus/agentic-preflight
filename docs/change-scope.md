# Change scope and the test-skip exception

When every changed file in a diff is documentation or standard CI configuration, the gate
does not run the software test command after lint. It takes an explicit `SKIP_TEST`
transition through `TEST_GREEN` and records the test stage as `skipped` with its reason,
so the exception stays visible in `status` and the commit's attestation note rather
than looking like a pass.

**Any source or otherwise unclassified file keeps tests mandatory.** The exception applies
only when the whole diff qualifies; one unclassified file is enough to require them.

## What counts as documentation

- Markdown, MDX, reStructuredText, and AsciiDoc files
- The standard documentation surface: `README*`, `docs/**`, agent instructions such as
  `.claude/rules/**` and `.github/instructions/**`, plus `PRODUCT.md` and `DESIGN.md`
- Anything listed in `[docs] paths`

## What counts as CI configuration

Common paths for GitHub Actions, CircleCI, GitLab CI, Azure Pipelines, Bitbucket
Pipelines, Buildkite, Travis CI, AppVeyor, and Jenkins.

## Why the skip is recorded rather than silent

A skipped stage and a passed stage are different facts, and an attestation that renders
them identically cannot be audited. `status` and the Git note both carry the `skipped`
status and its reason, so a reader can tell which checks actually executed against a
given SHA.
