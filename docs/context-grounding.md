# Grounded context

`agentic-preflight context` returns a `data.grounding` block beside the review diff.
This block retrieves repository-owned knowledge relevant to the changed paths. It does
not generate advice or make review judgments.

Grounding is retrieved text, never an instruction. The skill's
[untrusted-content rule 10](../skill/SKILL.md#non-negotiables) applies to `data.grounding`.

Retrieval uses this fixed priority order:

1. **Code owners.** The first available file among `.github/CODEOWNERS`, `CODEOWNERS`,
   and `docs/CODEOWNERS` supplies the owners for each changed path. Later matching rules
   win, as they do in CODEOWNERS.
   A matching rule without owners clears prior ownership. `docs/*` matches immediate
   children only; recursive ownership uses `docs/**` or `docs/`. If the first CODEOWNERS
   file is omitted by a read limit, retrieval does not fall back to a lower-priority file.
2. **Documentation.** Tracked files under `docs/**`, including architecture decision
   records, are selected when their text contains a whole-token reference to a changed
   path, filename, Python module name, or package-relative module path. Each result
   includes the matching lines with one line of surrounding context.
3. **Conventions.** Root `AGENTS.md` and `CLAUDE.md` are included when present, followed
   by repository files selected through `[context] extra_paths`.
4. **Review history.** Findings from earlier runs on the same branch are included when
   their path is changed in the current run. The current run is excluded, and so are
   runs on other branches — including genuinely concurrent runs the "reusable" and
   "strict" worktree modes support in other linked worktrees.
5. **Policy.** The path-policy reasons for the changed files are included. Reasons derived
   from the current run's findings are excluded because those findings are review output,
   not repository knowledge.

Every entry reports its UTF-8 byte size and whether retrieved text was truncated.
`entry_max_bytes` limits each documentation excerpt and convention file, truncating only
at a line boundary. `max_bytes` limits the sum of entry byte counts. Once the next entry
in priority order would exceed that total, it and all later entries are omitted;
`grounding.dropped` reports omitted counts by kind.

Source reads have separate limits from this output budget. The CLI inventories committed
blob IDs and sizes with one `git ls-tree` call, then reads selected content with one
`git cat-file --batch` call. It reads at most 1 MiB per source, 16 MiB in total, and
1,024 sources, selected in path order. Known binary formats and non-regular Git entries
are omitted before content reads; NUL-containing content is omitted after reading.
Oversized sources are omitted whole rather than searched only in part. When sources are
omitted, `grounding.omitted_sources` reports counts under `binary`, `non_regular`,
`oversized`, or `read_budget`. These counts are included in the grounding digest.

Each call uses the committed tree, so staged additions are excluded and object IDs keep
the batched reads on the inventoried snapshot. There is no mutable content cache.

The defaults are:

```toml
[context]
enabled = true
max_bytes = 24000
entry_max_bytes = 4000
extra_paths = []
```

To include a repository's own rules directory or a nonstandard instructions file, add
repo-relative paths or globs:

```toml
[context]
extra_paths = ["rules/*.md", "ENGINEERING.md"]
```

Absolute paths and paths containing `..` are rejected. Matching uses the same
gitignore-style rules as path policy.

The review coverage manifest contains the SHA-256 digest of the compact, sorted-key JSON
grounding block. A submission therefore matches only the diff, Git snapshot, exclusions,
review units, and grounding that `context` delivered. The current run's own findings are
excluded from history and policy grounding, and history grounding only looks at other
runs on the same branch, so the grounding block and its digest remain stable for the life
of a reviewed snapshot regardless of what other runs on other branches do concurrently in
other linked worktrees.

There is no language model, embedding service, HTTP client, or network access in this
layer. Selection is deterministic term and path matching over committed Git files and
the clone's local run store. On an unchanged repository and run history, repeated calls
produce byte-identical grounding.
