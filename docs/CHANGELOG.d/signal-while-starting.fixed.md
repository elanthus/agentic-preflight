- On POSIX, a SIGTERM, SIGINT, or SIGHUP that reaches the CLI while a stage or a remote Git
  command is starting now stops that command's process group before the CLI exits. Before, a
  signal that arrived between starting the command and installing the signal handlers ended
  the CLI and left the command running.
- On POSIX, `agentic-preflight reviewer run` now stops the provider CLI it started when it
  receives SIGTERM, SIGINT, or SIGHUP. Before, SIGTERM or SIGHUP ended the reviewer and left
  the provider CLI running.
