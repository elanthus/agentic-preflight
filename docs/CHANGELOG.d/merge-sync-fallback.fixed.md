- Synchronization no longer silently changes what a branch lands. A rebase drops merge
  commits and skips commits the base already has, so it could discard an `-s ours` merge
  that reverts a branch already in the base, or a revert of a change the base also made,
  and validate and publish different content from the branch. When a rebase would change
  the tree that a clean merge of the branch produces, `start` and `mergeback` now restore
  the branch and merge the base in instead, and `start` reports `data.sync.merged: true`.
  The merge always commits and skips commit hooks regardless of local merge
  configuration. If Git cannot compute the merge result for a reason other than unrelated
  histories, synchronization stops before changing the branch.
- `start` now validates a branch whose only change is hidden by criss-cross history
  instead of reporting that it has no changes.
- Synchronization now refuses, before changing anything, when a path added by the base or
  by the branch's merge result already exists as an untracked or ignored local file, and
  lists those files. Previously a rebase could silently overwrite an ignored file, for
  example one the base starts tracking, or one a directory rename moved a branch file onto.
