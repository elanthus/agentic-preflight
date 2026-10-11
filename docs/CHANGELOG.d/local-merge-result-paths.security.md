- Attestation verification now classifies the paths that differ between the attested base
  and Git's merge of the head into it when it checks a test skip and the review-executor
  policy. Previously, criss-cross history could hide a source change behind Git's single
  chosen merge base, so a test skip was accepted even though merging the head changed
  source.
- `start` now refuses a branch whose diff from the merge base is empty but whose merge
  into the base would change files, and lists them in `data.merge_result_files`.
  Previously it reported that the branch had no changes, even though an `-s ours` merge
  of a branch already in the base would revert it.
