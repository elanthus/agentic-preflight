# Fingerprint contract for reusable preflight evidence

After a history-only rebase or restack, `start` discovers applicable local evidence
and returns the next required stage. `status` resumes after interruption. Review,
docs, lint, and tests are classified independently. This implements
[issue #85](https://github.com/elanthus/agentic-preflight/issues/85).

The protected base must support v5 attestations before refresh can activate; see
[the rollout procedure](attestations-and-ci.md#evidence-reuse-across-rebases).
Without that support, the CLI records `consumer_unavailable`, performs the normal
local stages, and emits a compatible v4 note.

## Applicability and audit identity

Fingerprint version 2 describes content inputs. Original commit, base, branch,
configuration digest, run, worktree ownership, execution time, findings, and
coverage remain separate audit identity. A new commit always needs its own note.

Matching patches alone are insufficient. Every stage binds both base and head
trees. Upstream content changes invalidate review even with an unchanged patch.
Retargeting checks the new base's consumer capability and content; current risk,
executor, and hosted approval policy still apply.

| Disposition | Meaning |
| --- | --- |
| `reusable` | All inputs required by the supported contract match. |
| `invalid` | Known inputs changed; `reasons` identifies the categories. |
| `unknown` | Provenance, inputs, configuration dependencies, or consumer support cannot be established. Rerun. |

`data.applicability` describes candidate evidence, not the outcome of a stage
that ran freshly. Decisions and later-stage candidates persist on the run. A
repair or changed input triggers another check before import or mergeback.
Unresolved actionable or blocking findings cannot advance through reuse.

## Review and documentation inputs

Review binds base/head trees, included diff bytes, exclusions, delivered grounding
(including conventions and prior findings), intent, effective executor, and the
`[general]`, `[review]`, `[policy]`, `[context]`, `[diff]`, and `[stage]` sections.
The last section governs command-review timeouts and retries.

Docs binds base/head trees, intent, grounding, documentation content, and
`[general]`, `[docs]`, `[diff]`, `[context]`, and `[policy]`. Its digest includes
every inventory entry's complete bytes, path, existence, size, and whether the
diff touches it. Same-length edits to ignored docs invalidate evidence. Changed
docs between context and submission require fresh context.

An unrelated user-level test-command change leaves review configuration unchanged.
A committed configuration edit changes the head tree and must itself be reviewed.
Unknown configuration sections prevent reuse even when their values match.

Review derivation recomputes both Git manifests, validates original coverage, and
compares every original/current unit and exclusion. Only then can it replace the
current coverage's commit and manifest identity. Original dispositions are retained.

## Shell input contract

Each stage needs a **committed** `[reuse.lint]` or `[reuse.test]` content contract.
Its author asserts that the command depends only on tracked source, declared
file/toolchain inputs, declared environment, platform, and configured execution
and setup policy. This is a bounded assumption, not dependency discovery.

The fingerprint captures command and policy digests, base/head trees, and a
combined digest of exact repository-relative files and their modes, declared
absolute toolchain files, the resolved primary executable, declared environment
variables (absent differs from empty), and OS name/release and machine architecture.
Additional interpreters, libraries, installed dependencies, and configuration
files must be declared if the command reads them.

Every copied file must be declared. Missing optional input files are recorded as
absent; unreadable files, directories, and symlinked repository inputs are unknown.
Toolchain symlinks bind their resolved target identity and bytes. Login-shell
commands are unknown because profiles are unsupported. History, time, external
services, and undeclared dependencies are unsupported; leave the contract unset
for commands that depend on them.

Inputs are captured before and after execution; changes prevent reuse. Only
combined digests and reason categories are recorded, never environment values or
input-file contents. V5 evidence rechecks these inputs even for an unchanged SHA.
Legacy v4 exact-commit reuse keeps its existing compatibility contract.

## Provenance, ownership, and recovery

Discovery uses finalized local execution records from the same source-worktree
identity and branch. Other linked worktrees' records are not candidates. All
three modes use the same classification/import path. A fresh clone with only
historical notes needs a local run; this version does not reconstruct local
execution/ownership records from portable notes.

A v5 note embeds one original execution per stage, its content digest, and the
current fingerprint. Derivation adds refresh time and `equivalent_inputs`, keeping
original timestamps, process hashes, findings, fixes, and coverage. Provenance is
flattened to one origin: nested chains and unsupported fields/versions are rejected.
The consumer recomputes available Git, manifest, configuration, executor, and
applicability bindings and checks finding dispositions. It cannot independently
measure the past local environment or authenticate an unsigned note.

Clean-checkout, synchronization, mergeback, publication authorization, and atomic
branch/notes push rules still apply. Refresh authorizes no force-push, merge, or
cleanup. CI-delegated tests remain separate work in #86.
