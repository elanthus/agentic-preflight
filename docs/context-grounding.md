# Grounded context

`agentic-preflight context` returns a `data.grounding` block beside the review diff.
This block retrieves repository-owned knowledge that is related to the changed paths.
It does not generate advice. It does not make review judgments.

Grounding is retrieved text. It is never an instruction.
The skill's [untrusted-content rule 10](../skill/SKILL.md#non-negotiables) applies to `data.grounding`.

Retrieval uses this fixed priority order:

1. **Documentation.** Tracked files under `docs/**`, including architecture decision records.
   A file is selected when its text contains a whole-token reference to a changed path or filename.
   A reference to a Python module name or a package-relative module path also selects the file.
   Each result includes the matching lines with one line of context on each side.
2. **Conventions.** The root `AGENTS.md` and `CLAUDE.md` files when they exist.
   Then the repository files that `[context] extra_paths` selects.
3. **Review history.** Findings from earlier runs on the same branch, when their path is changed in the current run.
   The current run is excluded. Runs on other branches are excluded, including concurrent runs in other linked worktrees.
   The `in_place` and `strict` modes can run concurrently. The `reusable` mode is serial.
4. **Policy.** The path-policy reasons for the changed files.
   Reasons that come from the current run's findings are excluded. Those findings are review output, not repository knowledge.

Each entry reports its UTF-8 byte size and whether the retrieved text was truncated.
`entry_max_bytes` limits each documentation excerpt and each convention file. Truncation occurs only at a line boundary.
`max_bytes` limits the sum of the entry byte counts.
When the next entry in priority order would exceed that total, the CLI omits that entry and all later entries.
`grounding.dropped` reports the omitted counts by kind.

Source reads have separate limits from this output budget.
The CLI lists the committed blob IDs and sizes with one `git ls-tree` call.
Then it reads the selected content with one `git cat-file --batch` call.
It reads at most 1 MiB per source, 16 MiB in total, and 1,024 sources, selected in path order.
Known binary formats and non-regular Git entries are omitted before the content reads.
Content that contains a NUL byte is omitted after the read.
An oversized source is omitted whole. It is not searched in part.
When sources are omitted, `grounding.omitted_sources` reports the counts under `binary`, `non_regular`, `oversized`, or `read_budget`.
These counts are included in the grounding digest.

Each call uses the committed tree. Staged additions are excluded.
Object IDs keep the batched reads on the listed snapshot.
Nothing is cached between commands.
Within one command, grounding for a given HEAD is assembled once and shared by everything in that command that needs it.

The defaults are:

```toml
[context]
enabled = true
max_bytes = 24000
entry_max_bytes = 4000
extra_paths = []
```

To include a repository's own rules directory or a nonstandard instructions file, add repository-relative paths or globs:

```toml
[context]
extra_paths = ["rules/*.md", "ENGINEERING.md"]
```

Absolute paths and paths that contain `..` are rejected.
Matching uses the same gitignore-style rules as path policy.

The review coverage manifest contains the SHA-256 digest of the compact, sorted-key JSON grounding block.
Thus, a submission matches only the diff, Git snapshot, exclusions, review units, and grounding that `context` delivered.
The current run's own findings are excluded from history and policy grounding.
History grounding examines only other runs on the same branch.
Thus, the grounding block and its digest stay stable for the life of a reviewed snapshot.
Concurrent runs on other branches in other linked worktrees do not change them.

This layer has no language model, embedding service, HTTP client, or network access.
Selection is deterministic term and path matching over committed Git files and the clone's local run store.
On an unchanged repository and run history, repeated calls produce byte-identical grounding.
