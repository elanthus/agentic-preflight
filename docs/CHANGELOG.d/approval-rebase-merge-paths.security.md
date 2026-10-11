- The hosted approval check now also classifies every path touched by a non-merge commit
  in `base..head`. A GitHub rebase merge replays those commits and drops merge commits, so
  a merge commit whose resolution cancelled an earlier high-risk commit hid that commit
  from both the branch diff and the merge result, yet a rebase merge landed it without
  human approval. A branch that changes a high-risk path and later reverts it now requires
  approval.
