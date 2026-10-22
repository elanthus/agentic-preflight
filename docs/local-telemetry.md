# Local run telemetry

This page describes the summary that the CLI keeps for each finished run.
Use the summaries to see how you use Agentic Preflight across your repositories.

Telemetry is local only.
The CLI makes no network request for it, and no summary leaves your machine unless you copy it.

## When a summary is written

A run is finished when it reaches `DONE`, `ABORTED`, or `ORPHANED`.
The CLI writes the summary one time, after the terminal state is written to the run record.
If the process stops before that write, the run has no summary until `telemetry rebuild` writes it.
All paths to a terminal state use this step:

- `finish` after a push.
- `abort`.
- `start` when it orphans the previous run of the worktree, or aborts the run that it was creating.
- `gc` when it orphans an abandoned run.

The summary goes to two places:

- `summary.json` in the run directory, `$GIT_COMMON_DIR/agentic-preflight/runs/<run_id>/`.
- One JSON line, with sorted keys, at the end of the user-level mirror, `runs.jsonl`.

The run's event log records the result as `summary_written` or `summary_failed`.
`finish` and `abort` also report it in `data.telemetry`. `gc` reports a list there.

A telemetry failure never fails the command.
The CLI does not roll back the run. It records the error in the `summary_failed` or `summary_written` event.
`finish`, `abort`, and `gc` also report it in `data.telemetry`. The `start` envelope does not, so read the event log of the run that `start` ended.

## Where the mirror is

The mirror directory is the first of these locations that applies:

1. `[telemetry] path` in the configuration. The value must be absolute or start with `~`.
2. `$XDG_DATA_HOME/agentic-preflight/telemetry`, when `XDG_DATA_HOME` is an absolute path.
3. `~/.local/share/agentic-preflight/telemetry`.

Set `[telemetry] mirror = false` to keep summaries in the run directories only.
Put the `[telemetry]` section in your user file, `~/.config/agentic-preflight/config.toml`.
A `[telemetry]` section in the repository file replaces your section, the same as every other section.
See [the configuration reference](configuration.md#local-run-telemetry-telemetry).

Run snapshots do not contain `[telemetry]`.
Thus, a telemetry setting cannot change a configuration digest, stage evidence, or an attestation.
The files use your default file permissions.

Uninstalling the CLI does not delete the telemetry directory or the `summary.json` files.
You can delete them at any time. They are not evidence, and the next finished run starts a new mirror.

## What a summary contains

A summary has `kind = "agentic-preflight-run-summary"` and `schema_version = 1`.
Code derives every field from the run record, the findings file, and the event log.
An unknown value is `null`. It is never `0`. Older run records do not have many fields.

| Field | Value |
|---|---|
| `written_at`, `run_id`, `state` | When the summary was written, the run ID, and the terminal state. |
| `record_schema_version` | The `schema_version` of `run.json`, 1 or 2. |
| `repo` | `root_commit`, the smallest root commit of `HEAD`. It is the same in every clone. `remote`, the `origin` URL as host and path, without the scheme, credentials, port, or `.git`. `name`, the last part of `remote`, or the clone directory name. `source_worktree_path`. |
| `branch`, `base_ref`, `head_sha`, `pushed_sha` | The validated branch, its base, the validated head, and the pushed commit. |
| `intent_sha256`, `intent_source` | The SHA-256 digest of the intent, and where the intent came from. |
| `created_at`, `finished_at`, `duration_seconds` | The start time, the terminal commit time, and the time between them. |
| `producer` | The tool and agent identity that `start` recorded, when the record has one. |
| `risk` | `level`, `verdict`, and `requires_human_review`. |
| `review` | `total_units`, `cited_count`, `clean_count`, `excluded_count`, and `executor`. |
| `stages` | For each stage: `status`, `attempts`, `finished_at`, `reused`, and `reason`. `reused` is true when the stage evidence was carried from an equivalent earlier run. `reason` is the recorded reason for a skipped stage only. |
| `findings` | `total`, counts `by_stage`, `by_severity`, `by_status`, and `by_action`, and `blocking`. `blocking` counts the findings at a blocking severity for their stage, whatever their status. A severity that is not text counts as `unknown` in `by_severity` and never as blocking. These values are `null` when the run has no findings file, or when the file is not a list of objects. |
| `fix_commits`, `validation_restarts` | The number of repair commits, and the number of returns to review. |
| `event_counts` | The number of events of each name. An event line that is not an object with a text name is not counted. The `finished`, `aborted`, and `orphaned` markers repeat `state`, and the summary events describe the summary itself. These are not counted. |
| `terminal` | `aborted_forced` for an `abort`, and `orphaned_reason`. |
| `hosted` | `last_ci_outcome`, from the last recorded hosted check, and `pr_url_recorded`. Only older records contain hosted checks or pull-request URLs. Current versions record neither, so a new run always has `null` and `false`. Do not use these fields to count pull requests. |
| `worktree_mode`, `pr_mode`, `config_digest` | From the run's configuration snapshot. |

## What a summary does not contain

A summary does not contain the intent text.
It does not contain diffs, file contents, finding titles or details, logs, or command output.
Failure reasons of stages are not copied, because they can quote command output.
The pull-request URL is not copied. Only the fact that one was recorded is kept.

## Summaries for earlier runs

Run `agentic-preflight telemetry rebuild` in a clone.
The command writes a new `summary.json` for every finished run record in that clone, of run schema 1 or 2.
It then replaces this repository's rows in the mirror.
The mirror keeps one row for each repository and run ID: the row with the newest `written_at`.
The repository is identified by `repo.root_commit`, or by `repo.source_worktree_path` when the root commit is unknown.
A run can be mirrored once with a root commit and once without, for example when Git was not available.
Rows with the same `run_id`, `created_at`, and `repo.source_worktree_path` are thus also kept as one row, the newest.
Rows of other repositories keep their exact text.
The command holds the mirror lock while it writes, and it replaces the file in one step.

A record that the command cannot read is reported in `data.unreadable_runs`.
Its earlier summary and mirror row stay. The command does not treat that record as absent.
Runs that are not finished are counted as `skipped`.
`summary.json` is derived data. You can delete it, and `rebuild` writes it again.

## Read the report

`agentic-preflight report` counts your finished runs from the summaries.
It reads files only. It writes nothing, and it makes no network request.

To see all your repositories as Markdown tables, run these commands in a clone:

```bash
agentic-preflight telemetry rebuild
agentic-preflight report --all-repos --format markdown
```

Run `telemetry rebuild` first when the clone has runs from before mirroring.
The output is one JSON envelope. The Markdown is in `data.markdown`, and the same statistics are in `data.overall`.
To read only the Markdown, pipe the output to `jq -r .data.markdown`.

The command selects rows as follows:

- Without options, it reads the mirror rows of the current repository, from all clones of that repository on this machine.
- When the mirror has no rows for the current repository, it summarizes the clone's finished run records in memory. `data.source.used` is then `clone_records`.
- `--all-repos` reads every mirror row.
- `--repo NAME_OR_REMOTE` reads every mirror row and keeps the repositories whose name or remote matches. Two repositories can have the same name.
- `--since` and `--until` keep runs by their `finished_at` time. `--since` includes the time. `--until` does not.
- `--state` keeps `DONE`, `ABORTED`, or `ORPHANED` runs.
- `--rows` adds the selected summary rows to `data.rows`.

For example, this command reports the runs of one repository that ended in October 2026:

```bash
agentic-preflight report --repo github.com/owner/widget --since 2026-10-01 --until 2026-11-01 --format markdown
```

The report gives the statistics for all runs, for each repository, for each client, and for each reported model.
A run without a recorded client or model is in the group `unknown`.
Each ratio names its denominator.
A null value is left out of each statistic, and the report counts the runs that it left out.
A total is null when no run in the group has the value. The Markdown shows it as `n/a`, not `0`.
The median and p90 duration are not computed when fewer than 5 runs have a duration.

The report tells you about every gap in its data:

- `data.source.mirror.malformed` counts the mirror lines that are not readable summaries. `malformed_lines` lists the first 20 by line number.
- `data.source.clone.unreadable_runs` lists the run records of the clone that could not be read or summarized.
- `data.source.clone.unmirrored_terminal_runs` counts the finished runs of the clone that are not in the mirror. `telemetry rebuild` adds them.
- `data.filtered_out` counts the rows that each filter left out.

Some values need care:

- A summary counts blocking findings and finding statuses separately. Thus, the number of fixed blocking findings is a range, from `min` to `max`.
- A stage's `attempts` counts its failed attempts. "Failed attempts: more than one" counts runs where a stage failed more than one time.
- Current versions record no hosted outcomes. The hosted counts include only older runs that observed an outcome. Do not use them to count pull requests.

Every report ends with `data.limits`. The Markdown repeats them.

## Limits of the data

The counts show how you use the tool and how findings were resolved.
They do not measure reviewer recall.
A run with no findings can be a clean change or a missed defect. The summary cannot tell these apart.
A run that a later `start` orphaned can be a replaced attempt, not a failure.
The mirror contains only runs from this machine.
It contains runs that ended before this version only after `telemetry rebuild` reads them.
Older records do not have some fields. Those values are `null`, so count them as unknown, not as zero.

## Hook decisions

The pre-push hook runs `agentic-preflight hook-check`.
For each decision, `hook-check` appends one JSON line, with sorted keys, to `hook-decisions.jsonl` in the telemetry directory.
That file is in the same directory as `runs.jsonl`. See [Where the mirror is](#where-the-mirror-is).
The log is local only. The hook makes no network request, and it writes nothing in the repository.

`hook-check` decides its exit code and its message before it writes the line.
The log cannot change either of them.

### What a decision line contains

| Field | Value |
|---|---|
| `at` | The time of the decision, in UTC, to the second. |
| `repo` | The repository identity, with the same fields as `repo` in a summary. `source_worktree_path` is the worktree that ran the hook. |
| `remote` | The remote name that Git gave the hook, for example `origin`. When a push names a URL instead of a remote, the URL is recorded as host and path, without credentials. The value is `null` when the hook did not get a name. |
| `allowed` | `true` when the hook allowed the push, `false` when it blocked the push. |
| `reason` | The reason code. The next table lists the codes. |
| `updates` | One item for each ref update that Git sent: `local_ref`, `remote_ref`, `sha`, and `force`. `sha` is the first seven characters of the pushed commit. `force` is `true` when the update rewrites history that the remote has. `force` is `null` when the hook did not examine the update. |

| Reason | Allowed | Meaning |
|---|---|---|
| `attested` | yes | Each update that needs an attestation has a valid one. |
| `already_on_remote` | yes | No update needs an attestation. At least one update moves a ref to a commit that the remote already has. |
| `deletion` | yes | Each update deletes a ref. |
| `exempt` | yes | Each update is a tag, the attestation notes ref, an evidence ref, or a deletion. Not all of them are deletions. |
| `no_updates` | yes | Git sent no update. Git runs the hook this way when the remote is already up to date. |
| `config_unavailable` | yes | The hook could not load the repository or its configuration. It allowed the push without a check. |
| `force_push_refused` | no | An update rewrites history that the remote has, and `[hook] allow_force_push` is false. |
| `missing_attestation` | no | The new tip of an update has no valid attestation note for publication. |

The first update that blocks the push sets the reason.
The hook does not examine the updates after it, so their `force` is `null`.
The hook also does not examine an exempt update, so its `force` is `null`.
A line never contains the block message, a full commit SHA, credentials, or command output.

### Turn the log off

Set `[telemetry] hook_log = false` in your user file to stop the log.
See [the configuration reference](configuration.md#local-run-telemetry-telemetry).
`[telemetry] path` moves the log together with the mirror.

When the configuration cannot be loaded, the hook reads only the `[telemetry]` section of each configuration file.
A repository section replaces your section, as usual.
If a configuration file is not valid TOML, or if the section is not valid, the hook does not know your preference.
It then logs nothing.

### When a line is not written

The log is best effort.
After the decision, the hook waits for the log for 2 seconds at most.
That time includes the repository identity, the lock, and the write. A write that takes longer is abandoned.
The hook ignores every log failure, for example a directory that it cannot write.
The push then continues with the same exit code and the same message.
Writers hold `hook-decisions.jsonl.lock` in the same directory while they append.

These pushes leave no line:

- A push with `git push --no-verify`, because Git does not run the hook.
- A push that the installed hook allowed because `agentic-preflight` is not on `PATH`. The hook script stops before it runs `hook-check`.
- A push whose line could not be written in time, or at all.

A hook that an earlier version installed does not give `hook-check` the remote name, so its lines have `remote = null`.
Run `agentic-preflight init` to replace that hook. See [the pre-push hook](pre-push-hook.md#hook-decision-log).

### Count the decisions

`agentic-preflight report` reads the log and counts the decisions in `data.hook_decisions`.
The Markdown shows them in the "Hook decisions" section, before the limits.

The report selects decisions as it selects runs:

- Without options, it counts the decisions of the current repository. A decision belongs to the repository by its `repo.root_commit`, or by its worktree path when the root commit is unknown.
- `--all-repos` counts the decisions of all repositories.
- `--repo` keeps the repositories whose name or remote matches.
- `--since` and `--until` use the `at` time of the decision.
- `--state` does not apply to decisions.

`data.hook_decisions` contains these items:

- `source`: `path`, `enabled`, `exists`, `error`, `lines`, `rows`, `malformed`, `malformed_by_reason`, and `malformed_lines`. A malformed line is counted, and `malformed_lines` lists the first 20 by line number.
- `filtered_out`: the decisions that `scope`, `repo`, `since`, `until`, and `time_unknown` left out, and their `total`.
- `overall`, and `metrics` for each item of `by_repo`: `decisions`, `allowed`, `blocked`, `blocked_share`, `allowed_by_reason`, and `blocked_by_reason`. `blocked_share` names its denominator, "hook decisions".

When the log exists but cannot be read, `source.error` gives the error, and `overall` is `null`. The report does not fail.
A decision is not a push. Git can still fail a push that the hook allowed, and an agent often retries a blocked push.
