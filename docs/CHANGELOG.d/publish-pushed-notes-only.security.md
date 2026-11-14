- `push` no longer publishes the whole local `refs/notes/agentic-preflight` ref. It builds a notes
  commit from the remote notes tip plus only the notes of the commits being pushed (the run's
  merge base to head). A note on any commit outside that range, with its branch name, findings,
  file paths, and configuration, stays local. The guarantee is per commit: a pushed commit's
  note is published even when a run on another branch, such as an aborted one, wrote it, and it
  then carries that branch's name. A concurrent notes update from another clone is kept: `push`
  leases the remote notes ref to the fetched tip, so any change to it, including a deletion or
  rewind, rejects the push, and `push` rebuilds on the new remote tip and retries. In manual
  gate mode, `gate` rejects a fetch that changes the head's attestation. After the push, the published notes commit is
  merged into the local notes ref. `gate` and dry runs list the pushed commits that carry a local
  note in `data.published_notes`, `push` reports the notes it added or changed there, and the
  manual gate command names the built notes commit. `gc` and publication delete temporary notes
  refs that an interrupted command left behind.
  (GHSA-r5jg-frwr-44qc)
