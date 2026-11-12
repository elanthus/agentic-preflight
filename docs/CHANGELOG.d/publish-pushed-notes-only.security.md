- `push` no longer publishes the whole local `refs/notes/agentic-preflight` ref. It builds a notes
  commit from the remote notes tip plus only the notes of the commits being pushed, so the notes
  of aborted or never-pushed branches, with their branch names, findings, file paths, and
  configuration, stay local. A concurrent notes update from another clone is kept: `push`
  rebuilds on the new remote tip and retries. `gate`, dry runs, and `push` report the published
  commits in `data.published_notes`, and the manual gate command names the built notes commit.
  (GHSA-r5jg-frwr-44qc)
