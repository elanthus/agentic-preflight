# Repository instructions

## Long-running commands

- Treat a command as complete only when its execution result includes an explicit exit code.
- A quiet interval, output timeout, polling-window completion, or returned session or process
  ID does not mean the command exited.
- If a command returns a session or process ID without an exit code, continue polling that
  same session until it exits or the user explicitly cancels it.
- For test runs, report the final exit code and pytest terminal summary. Do not claim that the
  summary was missing unless the process exited and its complete output genuinely lacks one.
- Do not start a duplicate full-suite run merely because the current run is temporarily quiet.
