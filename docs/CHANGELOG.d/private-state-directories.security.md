- The run store (`$GIT_COMMON_DIR/agentic-preflight`) and the telemetry directory are
  created owner-only (`0700`) on POSIX. An existing run store you own is narrowed when the
  next run is created, and an existing telemetry directory on its next write. Run records,
  stage logs, events, and telemetry rows were readable by other local users under a default
  umask. On Windows these directories still inherit the parent folder's access list.
- `copy_files` creates each copy at `0600` instead of copying under the umask and narrowing
  it afterwards, and replaces a file or symlink already at the destination instead of
  writing through it.
