- Git attributes no longer hide content from review or change the review patch.
  Before, a branch that committed `.gitattributes` with `* -diff` turned every patch into
  "Binary files ... differ": the reviewer saw no content, each file still counted as
  reviewed, and local verification and the pre-push hook passed. `$GIT_DIR/info/attributes`,
  `attr.tree`, `GIT_ATTR_SOURCE`, and a `diff=<driver>` attribute with `diff.<driver>.*`
  configuration also changed the patch text. Review patches are now built without
  attributes: text files are diffed as text, and only a file that Git's default rule calls
  binary gets a "Binary files ... differ" line. Hunk headers no longer follow a diff driver.
  (GHSA-m8wg-p58p-cqmf)
- A diff driver with a hunk-header pattern in `$GIT_DIR/info/attributes`, `diff` set
  there on a binary file, or a driver there that prints a binary file as text, now stops the command with `review_diff_attribute` (exit 3),
  because no other machine can rebuild that text. So does `diff.default.xfuncname` or
  `diff.default.funcname`, which would rewrite every hunk header.
- A note from 0.8.1 or earlier whose review text marked a text file as binary is now
  rejected. A note that neither text reproduces still fails as an ordinary mismatch, even
  where the checkout's attributes mark a text file as binary. `COMPATIBILITY.md` lists
  what changes at upgrade.
