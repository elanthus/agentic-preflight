# demo.tape - VHS source for the agentic-preflight workflow GIF.
#
# Build the fixture, then render:
#
#     ./docs/demo-fixture.sh && vhs docs/demo.tape
#
# demo-fixture.sh creates everything described below from scratch under /tmp,
# destroying and rebuilding it each run. The paths here are fixed rather than
# configurable because the origin path is visible on screen in the blocked-push
# frame; changing it would make the committed GIF disagree with this tape.
#
#   /tmp/ap-demo/repo        a tiny python git repo:
#                        - branch `feature/percentage` checked out, no upstream set
#                        - contains a real, uncommitted-free bug: calc.py's
#                          percentage() divides by `whole` with no zero guard
#                        - `.agentic-preflight.toml` committed, `agentic-preflight init`
#                          already run (pre-push hook installed)
#                        - `origin` remote points at /tmp/ap-demo-origin.git, a local
#                          bare repo with only `main` pushed, so `git push origin
#                          feature/percentage` is a real, offline push attempt that the
#                          pre-push hook can intercept. A short /tmp path is used
#                          deliberately so the blocked-push frame (the most-viewed
#                          frame in the recording) doesn't render a long scratch path
#                          or session UUID.
#   /tmp/ap-demo/workspace   sibling directory (outside the repo) holding:
#                        - findings.json       (the one genuine `high` finding, F001)
#                        - findings-empty.json (`{"findings": []}`, used for the docs stage)
#                        - fix_percentage.py    (patches calc.py + tests/test_calc.py
#                                                to add the missing zero guard)
#
# The workspace is deliberately outside the repo: an untracked findings.json in
# the validation checkout would dirty the tree and stop the run.
#
# The hidden setup step below defines short shell functions (jqs/jqc/jqb/jqd/jqg)
# that wrap the `jq -c '{...}'` filters used throughout, purely so the commands
# typed on screen stay short and readable within the recording's time budget.

Output demo.gif

Require agentic-preflight
Require jq
Require git

Set Shell "zsh"
Set FontSize 16
Set Width 1000
Set Height 650
Set Theme "Dracula"
Set TypingSpeed 20ms
Set Padding 20

Hide
Type "cd '/tmp/ap-demo/repo' && clear && jqs() { jq -c '{ok,state,next}'; } && jqc() { jq -c '{ok,state,data:{changed_files:.data.changed_files},next}'; } && jqd() { jq -c '{ok,state,data:{doc_surface:.data.doc_surface},next}'; } && jqg() { jq -c '{ok,state,data:{token:.data.token,remote:.data.remote,branch:.data.branch},next}'; } && jqb() { jq -c '{ok,state,blocking:[.blocking[]|{id,severity,title}],next}'; }"
Enter
Sleep 500ms
Show

# 1. The hook payoff shot: a real push attempt, blocked by the pre-push hook.
Type "git push origin feature/percentage"
Enter
Sleep 2200ms

Type "clear"
Enter
Sleep 300ms

# 2. Start a run against the buggy feature branch.
Type "agentic-preflight start --intent 'Add a percentage helper.' | jqs"
Enter
Sleep 1800ms

# 3. Fetch the diff to review.
Type "agentic-preflight context | jqc"
Enter
Sleep 1800ms

# 4. Submit the one genuine high finding and see it accepted as blocking.
Type "agentic-preflight submit-findings --file ../workspace/findings.json | jqb"
Enter
Sleep 2200ms

# 5. Fix the bug for real, verify locally, commit, then respond to F001.
Type "python3 ../workspace/fix_percentage.py"
Enter
Sleep 1000ms

Type "uvx pytest -q"
Enter
Sleep 1000ms

Type "git commit -am 'Guard percentage() against a zero whole'"
Enter
Sleep 1000ms

Type "agentic-preflight respond --id F001 --action fixed --commit $(git rev-parse HEAD) | jqs"
Enter
Sleep 1800ms

Type "agentic-preflight verify | jqs"
Enter
Sleep 1000ms

# 6. Targeted tests, green.
Type "agentic-preflight stage run test | jqs"
Enter
Sleep 1800ms

# 7. Docs pass: nothing stale, zero findings is a valid outcome.
Type "agentic-preflight context --section docs | jqd"
Enter
Sleep 1800ms

Type "agentic-preflight submit-findings --file ../workspace/findings-empty.json | jqs"
Enter
Sleep 1000ms

Type "clear"
Enter
Sleep 300ms

# 8. Lint, green.
Type "agentic-preflight stage run lint | jqs"
Enter
Sleep 1800ms

# 9. Merge fixes back, then open the gate.
Type "agentic-preflight mergeback | jqs"
Enter
Sleep 1000ms

Type "agentic-preflight gate | jqg"
Enter
Sleep 2800ms
