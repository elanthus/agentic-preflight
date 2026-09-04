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
`.agentic-preflight.toml` and keep its wrapper at the documented path. Both configurations
set `executor = "command"`, so `agentic-preflight review run` replaces the coding agent's
review with an independent model call.

Set `AP_CODEX_BIN` or `AP_CLAUDE_BIN` when the executable is not named `codex` or `claude`.
Set `AP_REVIEWER_MODEL` to choose a different model and `AP_REVIEWER_TIMEOUT` to change the
wrapper's 600-second timeout. These calls may consume paid model quota. To use the coding
agent normally and require command review only for high-risk changes, set `executor =
"in_harness"` and `require_command_for = ["high"]` as shown in the examples.

## Compare two reviewers

Every accepted review is saved in the run directory as
`review-submission-<executor>.json`. After an in-harness review reaches green, run:

```console
agentic-preflight review compare
```

When `[review] command` is configured, this launches one shadow command review over the
same bundle. It writes redacted process output to `logs/review-compare.txt`, but it does
not submit those findings, consume review retries, or change the run state. A shadow
comparison costs one model call.

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
