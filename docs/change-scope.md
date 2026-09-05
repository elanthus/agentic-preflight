# Change scope and the test-skip exception

When every changed file in a diff is documentation or standard CI configuration, the gate
does not run the software test command after lint. It takes an explicit `SKIP_TEST`
transition through `TEST_GREEN` and records the test stage as `skipped` with its reason,
so the exception stays visible in `status` and the commit's attestation note rather
than looking like a pass.

**Any source or otherwise unclassified file keeps tests mandatory.** The exception applies
only when the whole diff qualifies; one unclassified file is enough to require them.

## What counts as documentation

- Markdown (`.md`), reStructuredText (`.rst`), and AsciiDoc (`.adoc`) files
- Extensionless root `README`, `CONTRIBUTING`, and `CHANGELOG` files
- Plain `.txt` files on the standard documentation surface or selected by `[docs] paths`

The documentation review surface is broader than this exception. Executable examples,
MDX components, and unknown file types keep tests mandatory even under `docs/**` or a
configured `[docs] paths` glob. A name such as `README.sh` is still software.

## What counts as CI configuration

Recognized YAML paths for GitHub Actions, CircleCI, GitLab CI, Azure Pipelines,
Bitbucket Pipelines, Buildkite, Travis CI, and AppVeyor. Scripts in these directories
keep tests mandatory. Jenkinsfiles also keep tests mandatory because they contain
executable pipeline code.

## Why the skip is recorded rather than silent

A skipped stage and a passed stage are different facts, and an attestation that renders
them identically cannot be audited. `status` and the Git note both carry the `skipped`
status and its reason, so a reader can tell which checks actually executed against a
given SHA.
