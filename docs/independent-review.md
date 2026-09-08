# Independent review and agreement

Agentic Preflight can preserve the coding agent's in-harness review and compare it with a
second review performed by a command-line model. The two reviewers see the same intent,
changed-file list, diff, review units, and optional grounding. The wrappers—not either
model—copy the delivered manifest into the strict submission, so model output cannot alter
the coverage identity.

## Configure a command reviewer

The worked [Codex configuration](examples/codex-reviewer.toml) calls the standard-library
[Codex wrapper](examples/reviewers/codex_review.py). The corresponding
[Claude configuration](examples/claude-reviewer.toml) calls the
[Claude wrapper](examples/reviewers/claude_review.py). Copy one complete TOML file to
`.agentic-preflight.toml`, and copy its wrapper and the shared
[_reviewer_common.py helper](examples/reviewers/_reviewer_common.py) into
`docs/examples/reviewers/` in your repository. Both configurations
set `executor = "command"`, so `agentic-preflight review run` launches an independent
reviewer wrapper.

Set `AP_CODEX_BIN` or `AP_CLAUDE_BIN` when the executable is not named `codex` or `claude`.
Set `AP_REVIEWER_MODEL` to an exact model ID, `AP_REVIEWER_EFFORT` to an effort supported by
that model and CLI, and `AP_REVIEWER_TIMEOUT` to change the wrapper's 600-second timeout.
The worked Codex wrapper defaults to `gpt-5.3-codex` and passes `medium` through the verified
Codex CLI 0.153.0 interface `-c model_reasoning_effort="medium"`. The worked Claude wrapper
defaults to `claude-sonnet-5` and passes `high` through Claude Code 2.1.236's verified
`--effort` option. Those are example configuration defaults, not comparative quality claims;
without either environment variable, `medium` for Codex and `high` for Claude are the wrapper's
effective efforts rather than inherited CLI defaults. These calls may consume paid model quota.
Provider guidance recommends selecting effort with task-specific evaluation rather than model
recency alone: [OpenAI's latest-model guide](https://developers.openai.com/api/docs/guides/latest-model),
[OpenAI's GPT-5.6 guide](https://openai.com/index/builders-guide-to-gpt-5-6/),
[Claude Fable 5.1 guidance](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5-1),
and [Claude Opus 5 guidance](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5).
To use the coding agent normally and require command review only for high-risk changes, set `executor =
"in_harness"` and `require_command_for = ["high"]` as shown in the examples.

The shared wrapper prompt defines the valid finding fields, requires examination of every
delivered review unit, and separates discovering a finding from the CLI's blocking policy.
It also treats the diff, repository content, grounding, and any instructions inside them as
evidence rather than authority. The wrapper copies the manifest itself and the product CLI
validates the resulting protocol deterministically. An `examined: "all"` receipt accounts
for delivered units; it is not evidence that a reviewer comprehended them.

## Compare model and effort configurations

Do not replace a configured reviewer merely because a model is newer. Use the same pinned
corpus, wrapper revision, CLI version, timeout, and grounding setting for every cell. Record
`codex --version` or `claude --version`, the exact `AP_REVIEWER_MODEL`,
`AP_REVIEWER_EFFORT`, command line, and report digest. Then run one authorized real-mode
evaluation per cell, for example:

```console
AP_EVAL_AUTHORIZED=1 AP_REVIEWER_MODEL=gpt-5.3-codex AP_REVIEWER_EFFORT=high \
  uv run python evals/run.py --mode real --executor codex --grounding on --out /tmp/ap-codex-high
```

Compare catch rate, fixed false-positive rate, unresolved runs, and the recorded exact
configuration. The report's `reviewer_invocations` counts wrapper launches only. Provider
requests, tokens, and cost are `null` unless independently measured by provider telemetry,
so do not compare cost from the report. This repository does not run paid evaluations during
ordinary development; the example is a reproducible maintainer procedure, not a result.
For an explicit, unevaluated sweep, retain the configured defaults as controls and add exact
candidate IDs such as `gpt-5.6-sol`, `claude-fable-5-1`, and `claude-opus-5` only after
confirming availability in the selected CLI and provider account. They are comparison
candidates, not new defaults or claims of better review quality.

## Compare two reviewers

Every accepted review is saved in the run directory as
`review-submission-<executor>.json`. After an in-harness review reaches green, run:

```console
agentic-preflight review compare
```

When `[review] command` is configured, this launches one shadow command review over the
same bundle. It writes redacted process output to `logs/review-compare.txt`, but it does
not submit those findings, consume review retries, or change the run state. A shadow
comparison launches one reviewer wrapper; it does not establish a provider request count or
expose provider token or cost telemetry.

Comparison remains available while tests are delegated to CI and after publication
evidence is prepared. It does not mark pending CI tests as passed.

To compare with a submission produced elsewhere, avoid the shadow call:

```console
agentic-preflight review compare --file second-review.json
```

The file may be a strict review submission or a previously persisted executor submission.
The command refuses stale worktree heads and differing manifests. It writes
`review-compare.json`, appends a `review_compared` event, and returns the same summary in
the envelope's `data`.

`units.both_flagged`, `only_a`, `only_b`, and `neither` count review units cited by both,
one, or neither reviewer. `agreement_rate` is `both_flagged` divided by all units flagged
by at least one reviewer; it is `null` when neither reviewer flags a unit. Findings are
paired when they cite the same unit and path and their line numbers are equal or within
three lines. `findings.severity_disagreements` contains paired locations whose severities
differ; the remaining finding lists show shared and reviewer-only locations.

These measurements are descriptive. Agreement is not proof of correctness, disagreement
is not proof that either reviewer erred, and unit-level agreement ignores finding quality.
The report also cannot measure defects both reviewers missed.

For an authorized maintainer study, [collect-agreement.sh](examples/collect-agreement.sh)
copies each run's report into a user-local agreement directory. The prepared
[summarizer](examples/summarize-agreement.py) aggregates those files without sending data
anywhere.
