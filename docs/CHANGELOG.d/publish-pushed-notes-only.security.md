- `push` no longer publishes the whole local `refs/notes/agentic-preflight` ref. It builds a notes
  commit from the remote notes tip plus only the notes of the commits being pushed, so the notes
  of aborted or never-pushed branches, with their branch names, findings, file paths, and
  configuration, stay local. A concurrent notes update from another clone is kept: `push`
  rebuilds on the new remote tip and retries. After the push, the published notes commit is
  merged into the local notes ref. `gate` and dry runs list the pushed commits that carry a local
  note in `data.published_notes`, `push` reports the notes it added or changed there, and the
  manual gate command names the built notes commit. `gc` and publication delete temporary notes
  refs that an interrupted command left behind.
  (GHSA-r5jg-frwr-44qc)
