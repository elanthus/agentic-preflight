- On macOS, killing a process group now stops a process that a member of the group was
  forking at that moment. This covers a stage or a remote Git command that timed out or
  whose CLI was signalled, and the provider CLI of a reviewer that was killed. Before, the
  kill was one `killpg`, which on macOS does not reach a child whose fork completes while
  the signal is being delivered, so that child ran on after the rest of its group was
  gone. The kill is now repeated until no process in the group is left to signal, for at
  most two seconds. On other platforms a stage or Git command is still killed with one
  signal: Linux delivers a group signal to both sides of a fork and did not have the
  defect, and no other platform has been measured. The watcher that kills a dead
  reviewer's provider CLI repeats the signal on every POSIX platform.
