- Repeat `--client` and `--model` in the `start` command that `next.command` suggests
  for a refused, stale, interrupted, or aborted run when those values were given as
  flags, so a replaced or restarted run records the same client and model. A client
  detected from the environment is not turned into a flag: the next `start` detects it
  again.
