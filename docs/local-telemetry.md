# Local run telemetry

This page describes the summary that the CLI keeps for each finished run.
Use the summaries to see how you use Agentic Preflight across your repositories.

Telemetry is local only.
No summary leaves your machine unless you copy it.
One command, `telemetry sync-hosted`, reads pull-request and check outcomes from GitHub, and only when you run it.
See [Join hosted outcomes](#join-hosted-outcomes).

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
  A run in a repository without a network remote gets no line.
  See [Repositories without a network remote](#repositories-without-a-network-remote).

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

## Repositories without a network remote

A preflight run gates a push. A repository that cannot push is a test fixture, not work.
By default, the CLI keeps the runs and hook decisions of such a repository out of your telemetry directory, wherever the repository lives.

A repository has no network remote when its `origin` is missing, is a local path, for example a bare repository on this machine, or names a loopback host (`localhost`, `127.x.x.x`, or `::1`).
The remote is a network remote when its normalized form starts with a host name, `host/path`.
A normalized remote that starts with `/`, `\`, `.`, `~`, or a drive letter is a local path.

The location of the worktree does not matter.
A fixture under your home directory, for example one that an evaluation harness creates under `~/dev/my-evals/artifacts/`, is excluded the same as one under `/tmp`.
A clone whose `origin` is on a host, for example `github.com/owner/widget`, is recorded as usual, even under the system temporary directory.
Thus, real work in a scratch clone keeps its telemetry, and hundreds of one-run fixtures cannot bury your real repositories in `report`.

The CLI applies the rule as follows:

- For a run, it uses the remote of its summary, `repo.remote`.
- For `hook-check`, it uses the `origin` of the worktree that runs the hook. When the repository has no `origin`, the remote that Git gives the hook decides: a remote name is looked up, and a URL is used as it is.

For a repository without a network remote, the CLI summarizes the runs, but it does not write to your telemetry directory:

- `summary.json` is written in the run directory, the same as for every run.
- No line is appended to `runs.jsonl`.
  The `summary_written` event and `data.telemetry` have `mirrored: false` and `reason: "no_network_remote"`.
- `telemetry rebuild` and `telemetry sync-hosted` in such a clone write the summaries, but they do not change the mirror.
  Their `mirror` object has `reason: "no_network_remote"`.
- In a clone with a network remote, these commands leave out each summary whose `repo.remote` is not a network remote, for example a run recorded before `origin` was added. `mirror.excluded_runs` counts them.
- `report` counts these runs in `data.source.clone.excluded_runs`, and does not tell you to run `telemetry rebuild` for them.
- `hook-check` writes no line to `hook-decisions.jsonl`.

An explicit `[telemetry] path` is an opt-in. It receives the rows of repositories without a network remote too.
The rule protects only the default locations, `$XDG_DATA_HOME/agentic-preflight/telemetry` and `~/.local/share/agentic-preflight/telemetry`.
To record such a repository, set `path` in your user file. You can set it to the default location.
`mirror = false` and `hook_log = false` still turn off the files.

Rows that are already in the mirror stay there. Delete them yourself if you do not want them.
Rows of a repository that has lost its remote also stay. `telemetry rebuild` in that clone does not remove them.

### Isolate a test harness

A test or evaluation harness that runs `agentic-preflight` against fixture repositories must not write your telemetry.
Give the fixture repositories no network remote, and also do one of these:

- Set `XDG_DATA_HOME` to a scratch directory in the environment of every CLI subprocess. Set `XDG_CACHE_HOME` too, because isolated worktrees go there.
- Put a `[telemetry]` section with `mirror = false` and `hook_log = false` in the `.agentic-preflight.toml` of each fixture repository.
  A repository section replaces the user section, so this also overrides a `[telemetry] path` in the user file of the person who runs the harness.

A `[telemetry] path` in the user file is an opt-in, so the first option alone does not override it.
Use the second option, or give the harness its own home directory, when the harness must not depend on the user file.
The regression eval runner in this repository sets both variables. See [the eval README](../evals/README.md).

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
| `hosted` | `last_ci_outcome`, from the last recorded hosted check, and `pr_url_recorded`. Only older records contain hosted checks or pull-request URLs. Current versions record neither, so a new run always has `null` and `false`. Do not use these fields to count pull requests. `pr_number`, `pr_state`, `merged`, `checks_conclusion`, and `fetched_at` come from `telemetry sync-hosted`. They are `null` until a sync joins the run. See [Join hosted outcomes](#join-hosted-outcomes). |
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
In a repository without a network remote, the command does not change the mirror. See [Repositories without a network remote](#repositories-without-a-network-remote).

A record that the command cannot read is reported in `data.unreadable_runs`.
Its earlier summary and mirror row stay. The command does not treat that record as absent.
Runs that are not finished are counted as `skipped`.
`summary.json` is derived data. You can delete it, and `rebuild` writes it again.

## Join hosted outcomes

`agentic-preflight telemetry sync-hosted` adds the GitHub outcome of each pushed run to its summary.
The outcome is the pull request that contains the pushed commit, and the result of the commit's check runs.

This is the only telemetry command that talks to a network service. It only reads.
It sends `GET` requests to the GitHub API through the GitHub CLI, `gh`.
It writes files on your machine only.
No workflow command calls it. Run it yourself when you want hosted outcomes.

Before you run it:

- Install the [GitHub CLI](https://cli.github.com), and log in with `gh auth login`. The command uses the account of `gh`. It never reads or shows your token.
- Run it in a clone whose `origin` is on `github.com`. Other hosts, including GitHub Enterprise Server, are not supported.

To see which runs it will read, read them, and then show them in the report, run these commands in the clone:

```bash
agentic-preflight telemetry sync-hosted --dry-run
agentic-preflight telemetry sync-hosted
agentic-preflight report --format markdown
```

The command selects runs as follows:

- It reads only the `DONE` runs of this clone that have a `pushed_sha`.
- It reads the newest runs first, by `created_at`. `--limit` sets the maximum number of runs that it reads. The default is 50.
- It skips a run that already has `hosted.json` for its pushed commit. `--refresh` reads the run again, for example after its checks finished or its pull request merged.
- `--run RUN_ID` selects one run.
- `--dry-run` lists the runs that it would read in `data.runs`. It contacts nothing and writes nothing.

For each run, it reads the pull requests that GitHub associates with the pushed commit, and the check runs of the commit.
When there is more than one pull request, it uses the one whose head is the commit.
If there is none, it uses the one whose head branch is the run's branch, else the first one.
GitHub does not associate a commit with a pull request that was closed without merging.
When GitHub associates no pull request with the commit, the command thus also reads the pull requests of the run's branch, and uses the one whose head is the commit.
A pull request that was closed without merging is found only when the pushed commit is its last commit.
The check runs give one conclusion:

| Conclusion | Meaning |
|---|---|
| `failure` | One or more check runs failed, were cancelled, or timed out. |
| `pending` | No check run failed, and one or more check runs are not complete. |
| `success` | All check runs are complete, and none failed. Only the conclusions `failure`, `cancelled`, and `timed_out` are failures. For example, `skipped`, `neutral`, `action_required`, and `stale` are not. |
| `none` | The commit has no check runs. |

The command does not read commit statuses from the older status API.

The command writes `hosted.json` in the run directory:

```json
{
  "fetched_at": "2026-10-21T09:30:00+00:00",
  "sha": "<the pushed commit>",
  "pr": {"number": 295, "url": "https://github.com/owner/widget/pull/295", "state": "closed", "merged_at": "2026-10-21T09:00:00Z"},
  "checks": {"conclusion": "failure", "total": 14, "failed": ["test / test (ubuntu-latest, py3.13)"]},
  "missing": false,
  "source": "gh"
}
```

`pr` is `null` when GitHub associates no pull request with the commit.
`missing` is `true` when GitHub does not know the commit, for example when a force push removed it.
Then `pr` and `checks` are `null`, and the next sync does not ask for that commit again.

Then the command writes the `summary.json` of each run again, and replaces its mirror row.
It uses the same code as `telemetry rebuild`. The `hosted` object of the summary gets these keys:

| Key | Value |
|---|---|
| `pr_number`, `pr_state` | The number of the pull request, and `open` or `closed`. |
| `merged` | `true` when the pull request merged, `false` when it did not. `null` without a pull request. |
| `checks_conclusion` | `failure`, `pending`, `success`, or `none`. |
| `fetched_at` | When the command read the outcome. |

All five keys are `null` for a run that was not synced.
For a commit that GitHub does not know, only `fetched_at` has a value.

If a sync stops before it writes the summaries, the next sync writes them, without a request to GitHub.
`data.stale_summaries` counts these runs.

The command changes no run record and adds no events.
`hosted.json` is derived data. It is not evidence, and no fingerprint, evidence digest, or attestation reads it.
`telemetry rebuild` reads `hosted.json` too, so a rebuild keeps the outcomes.
If you delete `hosted.json`, the next sync reads that run again.

Some failures concern only one commit: an HTTP error that is not in the list below, or an answer that the command cannot read.
The command lists that commit in `data.errors`, and continues with the older runs.
Other failures stop the command. `data.stopped` names the failure.
The runs that the command synced before the failure keep their outcomes.
Each item in `data.errors` and `data.stopped` gives the reason, the run, the HTTP status, and a message.
The message never contains the output of `gh` or a token.
The error code tells you what to do:

- `gh_unavailable`: `gh` is not installed, is not logged in, or GitHub refused its token. Install `gh`, and log in with `gh auth login`.
- `unsupported_remote`: `origin` is not on `github.com`. The command read nothing.
- `hosted_rate_limited`: GitHub's rate limit stopped the command. Wait until the limit resets, then run the command again. It skips the runs that it already synced.
- `hosted_network_error`: GitHub did not answer. Check your network connection.
- `hosted_request_failed`: GitHub refused the request with HTTP 403 or 404, for example because the account of `gh` cannot read the repository. Or `data.errors` lists commits that the command could not read. Run the command again later.
- `telemetry_write_failed`: a file on your machine could not be written.

An outcome is a snapshot at `fetched_at`.
A check that runs again, or a pull request that merges later, changes the outcome only after `--refresh`.
For example, `agentic-preflight telemetry sync-hosted --refresh --limit 10` reads the 10 newest runs again.

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
- `data.source.clone.excluded_runs` counts the finished runs of the clone that are not in the mirror and that `telemetry rebuild` does not add. See [Repositories without a network remote](#repositories-without-a-network-remote).
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
- Recorded hosted outcomes come only from older runs that observed an outcome. Do not use them to count pull requests.
- Synced hosted outcomes come from `telemetry sync-hosted`. Each of their shares counts synced runs only.
  "Preflight green, hosted checks failed" counts the synced `DONE` runs whose checks concluded `failure`, over the synced runs with settled checks.
  A run whose checks were pending, or whose commit GitHub did not know, is left out and counted as excluded.
  The merged share counts the synced runs that have a pull request.
- A failed hosted check is not proof of an escaped defect. The check can be flaky, or fail for a reason that is not related to the change.

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
- A push from a repository without a network remote, unless `[telemetry] path` is set. See [Repositories without a network remote](#repositories-without-a-network-remote).

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
