- Reading a run ID that does not exist no longer creates an empty `runs/<id>/` directory
  with a lock file that `gc` never removed.
