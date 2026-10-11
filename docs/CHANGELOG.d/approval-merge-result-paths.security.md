- The hosted approval check now classifies the paths that differ between the base and
  Git's merge of the pull-request head into it, in addition to the paths changed since the
  merge base. With criss-cross history, Git's single chosen merge base could hide a change
  the merge makes, such as an `-s ours` merge that reverts a high-risk side branch already
  in the base, so the pull request no longer required human approval.
