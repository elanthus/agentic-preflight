- In `peer_review` and `owner_review` modes, an approval of an earlier pull-request head
  now carries to the current head when the trusted checker proves from Git objects that
  the branch content is unchanged. Both heads must change the same paths with the same
  `git patch-id --stable` identity against the base, and the current tree must equal
  Git's clean merge of the approved head with the base commit it now contains. Both heads
  must also merge into the base with the same clean result and carry the same non-merge
  commit patch identities, so `-s ours` merges cannot hide a revert or add commits. A clean
  merge from the base or a clean rebase keeps approval. Content commits, conflict
  resolution, merges that add content, and base changes next to the contribution still
  need a new review. A later dismissal or changes-requested review on any head revokes
  a carried approval. `environment` and `manual_merge` modes are unchanged.
- An approved head replaced by a rebase is fetched only through its
  `refs/agentic-preflight/evidence/<sha>` ref; without it, the approval does not carry.
- Approval results add `approved_head` and `approval_carried`.
