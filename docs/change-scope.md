# Change scope and the test-skip exception

Agentic Preflight does not run the software test command when every changed file is documentation or standard CI configuration.
In that case, lint runs and then the run takes an explicit `SKIP_TEST` transition to `TEST_GREEN`.
The run records the test stage as `skipped` with its reason.
`status` and the commit's attestation note show the skip. The skip does not look like a pass.

**Any source file or unclassified file keeps tests mandatory.**
The exception applies only when the whole diff qualifies.
One unclassified file is sufficient to require tests.
Attestation verification also checks the skip.
It classifies every path that differs between the attested base and Git's merge of the head into that base.
Thus, criss-cross history cannot hide a source change from that check.

## What counts as documentation

- Markdown (`.md`), reStructuredText (`.rst`), and AsciiDoc (`.adoc`) files in any directory
- The root `README`, `CONTRIBUTING`, and `CHANGELOG` files with no extension
- Plain `.txt` files on the standard documentation surface or in `[docs] paths`

The documentation review surface is larger than this exception.
Executable examples, MDX components, and unknown file types keep tests mandatory.
This applies also under `docs/**` or a configured `[docs] paths` glob.
A name such as `README.sh` is software.

## What counts as CI configuration

The recognized paths are the YAML files of these systems: GitHub Actions, CircleCI, GitLab CI, Azure Pipelines, Bitbucket Pipelines, Buildkite, Travis CI, and AppVeyor.
Scripts in these directories keep tests mandatory.
A Jenkinsfile also keeps tests mandatory because it contains executable pipeline code.

## Why the skip is recorded rather than silent

A skipped stage and a passed stage are different facts.
An attestation that shows them in the same way cannot be audited.
`status` and the Git note both carry the `skipped` status and its reason.
A reader can see which checks executed against a given SHA.
