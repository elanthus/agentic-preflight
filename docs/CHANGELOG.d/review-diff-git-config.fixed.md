- The review diff no longer depends on the Git configuration of the machine that builds it.
  Before, `core.abbrev`, `core.quotePath`, `core.bigFileThreshold`, `core.precomposeUnicode`,
  `diff.noprefix`, `diff.srcPrefix`, `diff.dstPrefix`, `diff.algorithm`, `diff.context`,
  `diff.interHunkContext`, `diff.indentHeuristic`, `diff.suppressBlankEmpty`,
  `diff.submodule`, `diff.orderFile`, `diff.default.binary`, the
  `GIT_DIFF_OPTS` environment variable, and the user-level attributes file each changed
  the patch text that the review digest hashes. `verify`, `push`, and the pre-push hook then rejected a valid note with
  "review fingerprint does not match the Git diff" on any machine whose settings differed
  from the producer's. Git also lengthens abbreviated object IDs as a clone gains objects, so
  a fresh CI clone could reject a note that verified locally. Review patches now carry full
  object IDs, and every such setting is fixed to Git's default. `diff.relative` is fixed too,
  as a precaution: it has no effect when Git runs from the repository root, as preflight
  does. Git attributes still shape
  the text: those in `.gitattributes`, `$GIT_DIR/info/attributes`, `attr.tree`,
  `GIT_ATTR_SOURCE`, and the system-wide attributes file, together with the `diff.<driver>.*`
  configuration of a driver an attribute names. So do `diff.default.xfuncname` and
  `diff.default.funcname`. `COMPATIBILITY.md` lists them.
- Replacement objects under `refs/replace` no longer change the review diff, the changed-file
  list, or risk classification. They exist in one clone only.
- `diff.ignoreSubmodules = all`, and `submodule.<name>.ignore = all` in local configuration,
  no longer hide a submodule pointer change from the review diff, the changed-file list,
  risk classification, or the list of paths a fix commit changes.
- `diff.submodule = log` or `diff` no longer stops `context` with an internal error when a
  change moves a submodule pointer.
- `core.precomposeUnicode`, which Git on macOS sets at `git init`, no longer stops `context`
  with an internal error for a path stored in decomposed Unicode.
- Notes written by 0.8.1 and earlier still verify when their producer left these settings
  at Git's default and the Git attributes and diff-driver settings that still apply give
  the verifier the same patch, unless the verifier's clone holds a number of objects for
  which Git picks a different abbreviation length; the earlier text then differs. A note
  written with another value, such as `diff.context = 5`, may fail verification, even on the
  machine that wrote it, when that value changed the patch. A note for a commit whose
  committed `.gitmodules` sets `submodule.<name>.ignore = all` on a changed submodule always
  fails, because the earlier patch left that pointer out. If verification fails, validate
  the commit again to replace the note.
  The verifier's own `core.abbrev` no longer matters for such a note. Until that format is
  removed in 0.10.0, a binary or other file-level patch in it binds its content only by an
  abbreviated object ID.
  `COMPATIBILITY.md` lists what changes at upgrade.
