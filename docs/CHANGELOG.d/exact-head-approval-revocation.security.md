- `approval-check` now chooses each reviewer's latest decisive review across every head
  of the pull request before checking which head it approves. A changes-requested review,
  a dismissal, or (for an `owner_review` self-approval) a later Comment review with other
  text on a different head now revokes an approval of the current head, so returning the
  branch to an approved commit no longer restores a revoked approval. A later approval of
  a head with different content also replaces an earlier approval. Previously the
  exact-head check ignored reviews submitted on other heads. (GHSA-h78r-2g3v-5227)
- A revoking review now counts even when GitHub reports its `commit_id` as `null`, for
  example after the reviewed commit is garbage-collected. A decisive review by an
  eligible reviewer without an integer `id` now fails the check with
  `attestation_failed` instead of sorting before every other review.
- In `owner_review` mode on the reviewer's own pull request, a review-thread reply (an
  empty-body Comment review) on any head now removes an earlier `approved` self-review.
