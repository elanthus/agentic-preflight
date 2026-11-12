- Git attributes no longer hide content from review or change the review patch.
  Before, a branch that committed `.gitattributes` with `* -diff` turned every patch into
  "Binary files ... differ": the reviewer saw no content, each file still counted as
  reviewed, and local verification and the pre-push hook passed. `$GIT_DIR/info/attributes`,
  `attr.tree`, `GIT_ATTR_SOURCE`, and a `diff=<driver>` attribute with `diff.<driver>.*`
  configuration also changed the patch text. Review patches are now built without
  attributes: text files are diffed as text, and only a file that Git's default rule calls
  binary gets a "Binary files ... differ" line. Hunk headers no longer follow a diff driver.
  (GHSA-m8wg-p58p-cqmf)
- A diff driver with a hunk-header pattern in `$GIT_DIR/info/attributes`, or `diff` set
  there on a binary file, now stops the command with `review_diff_attribute` (exit 3),
  because no other machine can rebuild that text.
- A note from 0.8.1 or earlier whose review text marked a text file as binary is now
  rejected. `COMPATIBILITY.md` lists what changes at upgrade.
