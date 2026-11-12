- `approval-check` now chooses each reviewer's latest decisive review across every head
  of the pull request before checking which head it approves. A changes-requested review,
  a dismissal, or (for an `owner_review` self-approval) a later Comment review with other
  text on a different head now revokes an approval of the current head, so returning the
  branch to an approved commit no longer restores a revoked approval. A later approval of
  a head with different content also replaces an earlier approval. Previously the
  exact-head check ignored reviews submitted on other heads. (GHSA-h78r-2g3v-5227)
