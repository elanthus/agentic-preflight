- Attestation verification now classifies the paths that differ between the attested base
  and Git's merge of the head into it when it checks a test skip and the review-executor
  policy. Previously, criss-cross history could hide a source change behind Git's single
  chosen merge base, so a test skip was accepted even though merging the head changed
  source.
