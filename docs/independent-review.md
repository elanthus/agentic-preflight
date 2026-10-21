# Independent review and agreement

Agentic Preflight can keep the coding agent's in-harness review and compare it with a second review from a command-line model.
Both reviewers see the same intent, changed-file list, diff, review units, and optional grounding.
The wrappers copy the delivered manifest into the strict submission.
The models cannot change the coverage identity through their output.

## Configure a command reviewer

The example [Codex configuration](examples/codex-reviewer.toml) calls the standard-library
[Codex wrapper](examples/reviewers/codex_review.py).
The [Claude configuration](examples/claude-reviewer.toml) calls the
[Claude wrapper](examples/reviewers/claude_review.py).

1. Copy one complete TOML file to `.agentic-preflight.toml`.
2. Copy its wrapper into `docs/examples/reviewers/` in your repository.
3. Copy the shared [_reviewer_common.py helper](examples/reviewers/_reviewer_common.py) into the same directory.

Both configurations set `executor = "command"`.
Thus, `agentic-preflight review run` launches an independent reviewer wrapper.

Set `AP_CODEX_BIN` or `AP_CLAUDE_BIN` if the executable is not named `codex` or `claude`.
Set `AP_REVIEWER_MODEL` to an exact model ID.
Set `AP_REVIEWER_EFFORT` to an effort that the model and CLI support.
Set `AP_REVIEWER_TIMEOUT` to change the wrapper's 600-second timeout.

The example Codex wrapper defaults to `gpt-5.3-codex` with `medium` effort.
It uses the verified Codex CLI 0.153.0 interface `-c model_reasoning_effort="medium"`.
The example Claude wrapper defaults to `claude-sonnet-5` with `high` effort.
It uses Claude Code 2.1.236's verified `--effort` option.
These defaults do not claim comparative quality.

Without environment overrides, the wrappers set these efforts explicitly.
They do not inherit CLI effort defaults.
These calls can consume paid model quota.
Codex requests JSONL events with `--json`. Claude requests `stream-json` with `--verbose`.

To capture CLI stdout, set `AP_REVIEWER_EVENTS_FILE` to an absolute file path.
This does not add measurements to the strict findings submission.
The wrapper overwrites that file and `<path>.status.json`.
The status file records whether the CLI exited successfully.
Flushed events remain available after a CLI failure or timeout.

The evaluation harness sets this interface automatically.
It normalizes measurements as described in
[eval telemetry](regression-eval.md#runtime-findings-states-and-tokens).
Provider guidance recommends task-specific evaluation to select effort, rather than model age alone.
See [OpenAI's latest-model guide](https://developers.openai.com/api/docs/guides/latest-model),
[OpenAI's GPT-5.6 guide](https://openai.com/index/builders-guide-to-gpt-5-6/),
[Claude Fable 5.1 guidance](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5-1),
and [Claude Opus 5 guidance](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5).

To use the coding agent normally, set `executor = "in_harness"`.
To require command review only for high-risk changes, also set `require_command_for = ["high"]`.
The examples show both settings.

The shared wrapper prompt defines valid finding fields and requires examination of every delivered review unit.
It separates finding discovery from the CLI's blocking policy.
It treats the diff, repository content, grounding, and embedded instructions as evidence, not authority.

The wrapper copies the manifest. The product CLI validates the protocol deterministically.
An `examined: "all"` receipt accounts for delivered units. It does not prove that the reviewer understood them.

For optional behavioral evidence before final review, enable [contract checks](review-contracts.md).
Set `review.contracts.planner_command` to an installed example wrapper.
The wrapper uses a planning prompt for `section: "contracts"`.
The final reviewer receives the execution receipt that the product records.
Planning can add one paid model invocation. It is disabled by default.

## Compare model and effort configurations

Do not replace a configured reviewer only because a model is newer.
Use the same pinned corpus, wrapper revision, CLI version, timeout, and grounding setting for each comparison cell.
Record these values:

- `codex --version` or `claude --version`
- Exact `AP_REVIEWER_MODEL` and `AP_REVIEWER_EFFORT`
- Command line
- Report digest

Run one authorized real-mode evaluation per cell. For example:

```console
AP_EVAL_AUTHORIZED=1 AP_REVIEWER_MODEL=gpt-5.3-codex AP_REVIEWER_EFFORT=high \
  uv run python evals/run.py --mode real --executor codex --grounding on --out /tmp/ap-codex-high
```

Compare catch rate, fixed false-positive rate, unresolved runs, and the recorded exact configuration.
The report's `reviewer_invocations` counts only wrapper launches.
Provider requests and cost stay `null`.
CLI token accounting separates exact totals from partial observations.
Do not compare cost from the report.

This repository does not run paid evaluations during ordinary development.
The example is a repeatable maintainer procedure. It is not a measured result.

For an explicit comparison of unevaluated candidates, keep the configured defaults as controls.
Confirm candidate availability in the selected CLI and provider account before you add an exact model ID.
Possible candidates include `gpt-5.6-sol`, `claude-fable-5-1`, and `claude-opus-5`.
These candidates are not new defaults or claims of better review quality.

## Compare two reviewers

The run directory stores each accepted review as `review-submission-<executor>.json`.
After an in-harness review passes, run:

```console
agentic-preflight review compare
```

If `[review] command` is configured, this launches one shadow command review over the same bundle.
A shadow review supplies comparison evidence without applying its findings to the run.
It writes redacted process output to `logs/review-compare.txt`.
It does not submit findings, consume review retries, or change the run state.

A shadow comparison launches one reviewer wrapper.
It does not establish a provider request count or expose provider token or cost measurements.

To compare with an existing submission without a shadow call, run:

```console
agentic-preflight review compare --file second-review.json
```

The file can be a strict review submission or a saved executor submission.
The command refuses stale worktree heads and different manifests.
It writes `review-compare.json` and appends a `review_compared` event.
It returns the same summary in the envelope's `data`.

`units.both_flagged`, `only_a`, `only_b`, and `neither` count units cited by both, one, or neither reviewer.
`agreement_rate` is `both_flagged` divided by all units flagged by at least one reviewer.
It is `null` if neither reviewer flags a unit.

Findings form a pair if they cite the same unit and path, with line numbers no more than three lines apart.
`findings.severity_disagreements` contains paired locations with different severities.
The other finding lists show shared locations and locations from only one reviewer.

These measurements describe agreement. Agreement does not prove correctness.
Disagreement does not prove that either reviewer made an error.
Unit-level agreement does not measure finding quality.
The report cannot measure defects that both reviewers missed.

For an authorized maintainer study, [collect-agreement.sh](examples/collect-agreement.sh) copies each run's report into a user-local agreement directory.
The example [summarizer](examples/summarize-agreement.py) combines those files without sending data elsewhere.
