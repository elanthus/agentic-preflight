- Each captured stage, setup, and review command stream keeps at most 8 MiB: its first and
  last 4 MiB around an `[output truncated: N bytes omitted]` line. Output goes to temporary
  files instead of pipes, so a timed-out stage keeps what it printed even when a descendant
  escaped the kill; the old `[output truncated: a process outside the stage's group kept
  its pipe open]` marker is gone.
