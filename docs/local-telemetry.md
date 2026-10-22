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

## Limits of the data

The counts show how you use the tool and how findings were resolved.
They do not measure reviewer recall.
A run with no findings can be a clean change or a missed defect. The summary cannot tell these apart.
A run that a later `start` orphaned can be a replaced attempt, not a failure.
The mirror contains only runs from this machine.
It contains runs that ended before this version only after `telemetry rebuild` reads them.
Older records do not have some fields. Those values are `null`, so count them as unknown, not as zero.
