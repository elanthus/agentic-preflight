## Agentic Preflight report

### Source

| Item | Value |
|---|---|
| Source | Mirror rows |
| Scope | All repositories |
| Mirror path | `/data/runs.jsonl` |
| Mirror file exists | yes |
| Mirror lines read | 13 |
| Malformed mirror lines | 2 |
| Older duplicate mirror rows left out | 3 |
| Filters | since 2026-01-01T00:00:00+00:00 |
| Rows left out by filters | 0 |
| Runs in this report | 8 |

### Runs

| State | Runs |
|---|---|
| DONE | 4 |
| ABORTED | 2 |
| ORPHANED | 2 |

| Measure | Rate | Count | Denominator | Excluded runs |
|---|---|---|---|---|
| Completed (`DONE`) | 50.0% | 4 | runs: 8 | n/a |
| Runs with a fix commit | 28.6% | 2 | runs with a fix-commit count: 7 | 1 |
| Runs with a validation restart | 33.3% | 2 | runs with a validation-restart count: 6 | 2 |

### Caught before push

A catch is something the gate stopped before the push: a blocking review or docs finding that was fixed, or a lint or test stage that failed and later passed in the same run. It is not proof that hosted checks or a reviewer would have missed it.

| Measure | Rate | Count | Denominator | Excluded runs |
|---|---|---|---|---|
| Runs with any catch | 100.0% | 1 | DONE runs with every catch source known or a known catch: 1 | 3 |
| Blocking review finding fixed | 0.0% | 0 | DONE runs with a known review blocking fixed value: 1 | 3 |
| Blocking docs finding fixed | 0.0% | 0 | DONE runs with a known docs blocking fixed value: 2 | 2 |
| Lint failed, then passed | 50.0% | 1 | DONE runs with a known lint red then green value: 2 | 2 |
| Test failed, then passed | 0.0% | 0 | DONE runs with a known test red then green value: 2 | 2 |

| Findings | Total |
|---|---|
| Blocking review finding fixed | 0 |
| Blocking docs finding fixed | 0 |

### Hosted outcomes synced

Fetched from GitHub by `telemetry sync-hosted`. Only synced runs count. Runs with pending checks are left out of the failed share. A conclusion of `unknown` is a run whose commit GitHub did not know.

| Measure | Rate | Count | Denominator | Excluded runs |
|---|---|---|---|---|
| Runs synced | 25.0% | 2 | runs: 8 | n/a |
| Preflight green, hosted checks failed | 0.0% | 0 | synced runs with settled checks: 2 | 0 |
| Pull request found | 100.0% | 2 | synced runs whose commit GitHub knew: 2 | 0 |
| Pull request merged | 100.0% | 2 | synced runs with a pull request: 2 | n/a |

| Hosted check conclusion | Synced runs |
|---|---|
| success | 2 |

| Repository | Synced runs | Green, hosted checks failed | Share of synced runs with settled checks | Merged of synced runs with a pull request | Merged share |
|---|---|---|---|---|---|
| widget (github.com/owner/widget) | 2 | 0 | 0.0% | 2 of 2 | 100.0% |

### Findings

| Measure | Value | Denominator | Excluded runs |
|---|---|---|---|
| Findings | 7 | runs with a findings file: 6 | 2 |
| Blocking findings | 2 | runs with a blocking count: 6 | 2 |
| Blocking findings fixed | 0 to 1 | blocking findings: 2 | 2 |
| Share of blocking findings fixed | 0.0% to 50.0% | blocking findings: 2 | 2 |
| Review findings per 100 review units | 8.7 | review units: 46 | 2 |

A summary counts blocking findings and finding statuses separately. The fixed count is exact only for runs where every finding or no finding is blocking. Otherwise it is a range.

| Dimension | Value | Findings |
|---|---|---|
| Stage | docs | 3 |
| Stage | review | 4 |
| Severity | critical | 2 |
| Severity | high | 2 |
| Severity | low | 2 |
| Severity | medium | 1 |
| Status | accepted | 1 |
| Status | dismissed | 2 |
| Status | fixed | 3 |
| Status | open | 1 |
| Action | ask_user | 1 |
| Action | auto_fix | 4 |
| Action | no_op | 2 |

### Finding responses

The perceived false-positive share counts the findings that the agent dismissed or accepted as `false_positive`, over every finding with a recorded response. A dismissal or acceptance without a reason counts as `unspecified`. While any are unspecified, the share is a range.

| Measure | Rate | Count | Denominator | Excluded runs |
|---|---|---|---|---|
| Perceived false-positive share | 0.0% to 50.0% | 0 to 3 | findings with a recorded response: 6 | 6 |
| Dismissed or accepted without a reason | 100.0% | 3 | dismissed or accepted findings: 3 | 6 |

| Reason | Findings |
|---|---|
| unspecified | 3 |

| Repository | Reason | Findings |
|---|---|---|
| widget (github.com/owner/widget) | unspecified | 1 |
| widget (github.com/owner/widget) | unspecified | 2 |

| Repository | Perceived false-positive share | Count | Denominator | Excluded runs |
|---|---|---|---|---|
| widget (github.com/owner/widget) | 0.0% to 33.3% | 0 to 1 | findings with a recorded response: 3 | 3 |
| gadget (gitlab.example/team/gadget) | n/a | 0 | findings with a recorded response: 0 | 3 |
| widget (github.com/owner/widget) | 0.0% to 66.7% | 0 to 2 | findings with a recorded response: 3 | 0 |

### Stages

| Stage | Runs | Statuses | Failed attempts: one or more | Failed attempts: more than one | Reused | Excluded runs (attempts / reuse) |
|---|---|---|---|---|---|---|
| review | 6 | green 6 | 0 of 6 | 0 of 6 | 1 of 6 | 0 / 0 |
| docs | 1 | green 1 | 0 of 1 | 0 of 1 | 0 of 1 | 0 / 0 |
| lint | 7 | green 6, red 1 | 2 of 7 | 1 of 7 | 0 of 6 | 0 / 1 |
| test | 5 | green 3, skipped 2 | 0 of 4 | 0 of 4 | 0 of 3 | 1 / 2 |

| Stage | Duration observations | Excluded runs | Median | p90 |
|---|---|---|---|---|
| review | 0 | 6 | not computed: too few observations | not computed: too few observations |
| docs | 0 | 1 | not computed: too few observations | not computed: too few observations |
| lint | 0 | 7 | not computed: too few observations | not computed: too few observations |
| test | 0 | 5 | not computed: too few observations | not computed: too few observations |

### Duration

| Measure | Value |
|---|---|
| Observations | 7 |
| Excluded runs | 1 |
| Minimum observations | 5 |
| Median | 10m 00s |
| p90 | 93h 00m |

Review time covers the review and docs states. Shell time covers the lint and test states. Only runs with a complete transition history are counted.

| Time in | Share | Time | Denominator | Excluded runs |
|---|---|---|---|---|
| Review and docs states | 12.1% | 40s | seconds in runs with a complete transition history: 5m 30s | 5 |
| Lint and test states | 78.8% | 4m 20s | seconds in runs with a complete transition history: 5m 30s | 5 |

| Repository | Runs with complete timing | Review and docs share | Lint and test share | Median review | Median docs | Median lint | Median test |
|---|---|---|---|---|---|---|---|
| widget (github.com/owner/widget) | 3 | 12.1% | 78.8% | not computed | not computed | not computed | not computed |
| gadget (gitlab.example/team/gadget) | 0 | n/a | n/a | not computed | n/a | not computed | not computed |
| widget (github.com/owner/widget) | 0 | n/a | n/a | not computed | n/a | not computed | n/a |

### Tool time

Tool time is the wall time of the commands the tool ran on its own before the run ended: sync, setup, the review command, the contract planner, the shadow review of `review compare`, the baseline check, lint, test, and merge-back. The push is not timed. A run recorded before command timing has no tool time. A pull request sums the tool time of its synced `DONE` runs, and is left out when any of them has no tool time.

| Group | Observations | Excluded | Median | p90 |
|---|---|---|---|---|
| Per run (DONE runs) | 1 | 3 | not computed: too few observations | not computed: too few observations |
| Per pull request (synced pull requests of DONE runs) | 0 | 1 | not computed: too few observations | not computed: too few observations |

### Selected events

| Event | Events | Runs with the event | Denominator |
|---|---|---|---|
| `review_coverage_invalidated` | 0 | 0 | runs with event counts: 6 |
| `test_interrupted` | 0 | 0 | runs with event counts: 6 |
| `sync_conflict` | 0 | 0 | runs with event counts: 6 |
| `stage_reused` | 0 | 0 | runs with event counts: 6 |

### Orphaned reasons

| Reason | Runs |
|---|---|
| replaced by a new start | 1 |
| unknown | 1 |

### Abort reasons

| Reason | Runs |
|---|---|
| unspecified | 2 |

| State when aborted | Runs |
|---|---|
| TEST_GREEN | 1 |
| unknown | 1 |

### By repository

| Repository | Runs | DONE | ABORTED | ORPHANED | Completed | Findings | Median duration |
|---|---|---|---|---|---|---|---|
| widget (github.com/owner/widget) | 4 | 3 | 1 | 0 | 75.0% | 3 | not computed |
| gadget (gitlab.example/team/gadget) | 3 | 1 | 0 | 2 | 33.3% | 0 | not computed |
| widget (github.com/owner/widget) | 1 | 0 | 1 | 0 | 0.0% | 4 | not computed |

### By client

| Client | Runs | DONE | ABORTED | ORPHANED | Completed | Findings | Median duration |
|---|---|---|---|---|---|---|---|
| unknown | 7 | 3 | 2 | 2 | 42.9% | 7 | 15m 00s |
| claude-code | 1 | 1 | 0 | 0 | 100.0% | 0 | not computed |

### By reported model

| Reported model | Runs | DONE | ABORTED | ORPHANED | Completed | Findings | Median duration |
|---|---|---|---|---|---|---|---|
| unknown | 8 | 4 | 2 | 2 | 50.0% | 7 | 10m 00s |

### Hook decisions

Each row is one pre-push hook decision, not one push. Git can still fail an allowed push, and a blocked push is often retried. The state filter does not apply to hook decisions.

| Measure | Value |
|---|---|
| Hook log path | `/data/hook-decisions.jsonl` |
| Hook log enabled | yes |
| Hook log file exists | yes |
| Hook log readable | yes |
| Hook log lines read | 4 |
| Malformed hook log lines | 1 |
| Decisions left out by scope and filters | 0 |
| Decisions in this report | 3 |
| Allowed | 2 |
| Blocked | 1 |
| Blocked share | 33.3% (1 of 3 hook decisions) |

| Decision | Reason | Decisions |
|---|---|---|
| allowed | attested | 2 |
| blocked | missing_attestation | 1 |

| Repository | Decisions | Allowed | Blocked | Blocked share |
|---|---|---|---|---|
| widget (github.com/owner/widget) | 2 | 1 | 1 | 50.0% |
| gadget (gitlab.example/team/gadget) | 1 | 1 | 0 | 0.0% |

### Limits

- The counts measure use and how findings were resolved. They do not measure reviewer recall or the defect escape rate.
- A run with no findings can be a clean change or a missed defect. The data cannot tell these apart.
- The mirror holds only runs that reached a terminal state after mirroring was enabled, or that `telemetry rebuild` added.
- Producer identity is partly self-reported by the agent. Runs without a client or a model are grouped as `unknown`.
- A duration is the wall-clock time from run creation to the terminal commit. It includes the agent's own working time.
- An orphaned run can be a replaced attempt, not a failure.
- A null value is unknown, not zero. Each statistic leaves out null values and reports how many runs it left out.
- Finding response reasons and abort reasons are labels that the agent chose. They record the agent's judgment, not a verified cause. The perceived false-positive share is not a measured false-positive rate.
- Stage and state times are wall-clock seconds from recorded transitions, with one-second granularity. They include the agent's working time and any wait for the user. Reused and skipped stages, and runs without a complete transition history, have no time.
- `git push --no-verify` skips the pre-push hook, so a bypassed push leaves no hook decision.
- A push that the installed hook allowed because `agentic-preflight` was not on `PATH` is not logged. Neither is a decision whose log write failed or did not finish in time.
- A failed hosted check is not proof of an escaped defect. The check can be flaky, or fail for a reason unrelated to the change.
- Synced hosted outcomes count only the runs that `telemetry sync-hosted` fetched, as GitHub reported them at the fetch time. Runs that were not synced are not in them.
