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
| `timing` | The wall-clock time in each state, from the `state_transition` events. See [Stage and state timing](#stage-and-state-timing). `null` when the event log has no transitions. |
| `producer` | The tool and agent identity that `start` recorded, when the record has one. |
| `risk` | `level`, `verdict`, and `requires_human_review`. |
| `review` | `total_units`, `cited_count`, `clean_count`, `excluded_count`, and `executor`. |
| `stages` | For each stage: `status`, `attempts`, `started_at`, `finished_at`, `duration_seconds`, `reused`, and `reason`. `started_at` is when the stage last began. `duration_seconds` is the time from `started_at` to `finished_at`. `reused` is true when the stage evidence was carried from an equivalent earlier run. `reason` is the recorded reason for a skipped stage only. |
| `findings` | `total`, counts `by_stage`, `by_severity`, `by_status`, `by_action`, and `by_response_reason`, and `blocking`. `by_response_reason` counts the reasons of the `dismissed` and `accepted` findings. A finding without a reason counts as `unspecified`. `blocking` counts the findings at a blocking severity for their stage, whatever their status. A severity that is not text counts as `unknown` in `by_severity` and never as blocking. These values are `null` when the run has no findings file, or when the file is not a list of objects. |
| `fix_commits`, `validation_restarts` | The number of repair commits, and the number of returns to review. |
| `event_counts` | The number of events of each name. An event line that is not an object with a text name is not counted. The `finished`, `aborted`, and `orphaned` markers repeat `state`, and the summary events describe the summary itself. These are not counted. |
| `terminal` | For an aborted run: `aborted_forced`, `abort_reason`, and `aborted_from_state`, the state that the run left. For an orphaned run: `orphaned_reason`. |
| `hosted` | `last_ci_outcome`, from the last recorded hosted check, and `pr_url_recorded`. Only older records contain hosted checks or pull-request URLs. Current versions record neither, so a new run always has `null` and `false`. Do not use these fields to count pull requests. |
| `worktree_mode`, `pr_mode`, `config_digest` | From the run's configuration snapshot. |

## Reasons for responses and aborts

`respond --action dismissed`, `respond --action accepted`, and `abort` take `--reason`.
Each reason is one term from a closed list:

| Command | Reasons |
|---|---|
| `respond` | `false_positive`, `out_of_scope`, `duplicate`, `already_fixed`, `accepted_risk`, `deferred`, `other` |
| `abort` | `user_cancelled`, `superseded`, `wrong_base`, `environment_failure`, `tool_error`, `no_longer_needed`, `other` |

The agent chooses the reason. It is a label, not a verified cause.
The reason is optional in this version, so older installed skills keep working.
A response or an abort without a reason is `null` in the record and `unspecified` in the counts.
`respond --action fixed` refuses `--reason`.

The finding keeps the reason as `response_reason` in `findings.json`. The `finding_resolved` event repeats it as `reason`.
A finding without a reason has no `response_reason` key.
The `aborted` event records `reason` and `aborted_from_state`.
Code takes `aborted_from_state` from the transition, not from the agent.

`start` can abort the run that it was creating. It writes no `aborted` event, so `abort_reason` and `aborted_forced` are `null` for that run.
For that run, and for records from before this version, `aborted_from_state` is the `from` of the transition to `ABORTED`.
It is `null` when the event log has no such transition.

Stage evidence and attestations do not contain reasons.
Thus, a finding that a run copies from reused evidence has no reason.

## Stage and state timing

Each stage record has `started_at`, the time of the transition that began the stage:

| Stage | Begins at |
|---|---|
| `review` | `BEGIN_REVIEW`. Each return to review, `INVALIDATE_REVIEW`, `LINT_FIX_RESTART`, or `TEST_FIX_RESTART`, sets the time again. |
| `docs` | `BEGIN_DOCS`. |
| `lint` | `RUN_LINT`. A retry after a failure keeps the time. |
| `test` | `RUN_TEST`. A retry after a failure keeps the time. |

`stages.<name>.duration_seconds` is the time from `started_at` to `finished_at`.
It is `null` when either time is unknown, when `finished_at` is earlier than `started_at`, and for a reused or skipped stage.
Thus, the duration of a stage that failed and was retried includes the failed attempts.
`started_at` is not in the stage evidence, the fingerprints, or the attestation.

`timing` comes from the `state_transition` events.
It uses their wall-clock `at` values, which have one-second granularity: each is the whole second in which the transition was made.
It does not use `monotonic_ns`, because that clock is valid only within one boot of one host.

| Key | Value |
|---|---|
| `complete` | `true` when the transitions start at creation, each `from` is the previous `to`, no `at` is earlier than the previous one, and the last `to` is the terminal state. |
| `states` | For each state that the run entered: `visits`, and the total `seconds` in that state. The terminal state is not included. |
| `review_wall_seconds` | The seconds in the `REVIEW_*` and `DOCS_*` states. `null` unless `complete` is `true`. |
| `shell_wall_seconds` | The seconds in the `LINT_*` and `TEST_*` states. `null` unless `complete` is `true`. |

When the transitions do not connect, `complete` is `false` and the count stops at the gap.
A visit whose end is unknown has `null` seconds, and its state then has `null` seconds.
A state that the run entered and left in the same second has `0` seconds. That is a measured value.
`timing` is `null` when the event log is missing or has no transitions.
Records from before transition events have no transitions.

Times include the agent's working time and any wait for the user.

## What a summary does not contain

A summary does not contain the intent text.
It does not contain diffs, file contents, finding titles or details, response notes, logs, or command output.
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
- "Finding responses" counts the reasons of dismissed and accepted findings, for all runs and for each repository.
  The perceived false-positive share is the findings dismissed or accepted as `false_positive`, over the findings with a recorded response: `fixed`, `dismissed`, or `accepted`.
  A response without a reason can be either, so the share is a range from `value` to `max` while any response is `unspecified`.
  Summaries written before this version have no reason counts and are left out.
- "Abort reasons" counts aborted runs by reason and by `aborted_from_state`.
- The stages table gives the median and p90 duration of each stage. They are not computed when fewer than 5 runs have a duration for that stage.
- The duration section gives the share of run time in the review and docs states, and in the lint and test states, for all runs and for each repository.
  The denominator is the time in every state of the runs with `complete` timing. Other runs are left out.
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
Response and abort reasons are labels that the agent chose. They tell you what the agent judged, not what was true.
The perceived false-positive share is not a measured false-positive rate.
Stage and state times include the agent's working time and any wait for the user.
