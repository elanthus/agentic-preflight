- The review diff no longer depends on the Git configuration of the machine that builds it.
  Before, `core.abbrev`, `core.quotePath`, `diff.noprefix`, `diff.srcPrefix`,
  `diff.dstPrefix`, `diff.context`, `diff.interHunkContext`, `diff.suppressBlankEmpty`,
  `diff.submodule`, `diff.orderFile`, and the user-level attributes file each changed the
  patch text that the review digest hashes. `verify`, `push`, and the pre-push hook then
  rejected a valid note with "review fingerprint does not match the Git diff" on any machine
  whose settings differed from the producer's. Git also lengthens abbreviated object IDs as
  a clone gains objects, so a fresh CI clone could reject a note that verified locally.
  Review patches now carry full object IDs, and every such setting is fixed to Git's default.
- `diff.ignoreSubmodules = all` no longer hides a submodule pointer change from the review
  diff, the changed-file list, or risk classification.
- `diff.submodule = log` or `diff` no longer stops `context` with an internal error when a
  change moves a submodule pointer.
- Notes written by 0.8.1 and earlier still verify when their producer left these settings
  at Git's default. A note written with another value, such as `diff.context = 5`, fails
  verification even on the machine that wrote it; validate the commit again to replace it.
  `COMPATIBILITY.md` lists what changes at upgrade.
