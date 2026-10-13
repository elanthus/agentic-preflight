- On Windows, Ctrl+C during a stage, setup, or reviewer command now kills its
  process tree before re-raising cancellation, preventing child processes from
  continuing after the CLI exits. POSIX signal handling is unchanged.
