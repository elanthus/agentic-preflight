- Synchronization no longer silently changes what a branch lands. A rebase drops merge
  commits and skips commits the base already has, so it could discard an `-s ours` merge
  that reverts a branch already in the base, or a revert of a change the base also made,
  and validate and publish different content from the branch. When a rebase would change
  the tree that merging the branch produces, `start` and `mergeback` now restore the
  branch and merge the base in instead, and `start` reports `data.sync.merged: true`.
- `start` now validates a branch whose only change is hidden by criss-cross history
  instead of reporting that it has no changes.
