# Approval readiness

Maintainer runbook for when a pull request is ready to merge.

Verified Dependabot updates do not require human review, including major versions and
GitHub Actions updates. Require the protected-base hosted approval workflow to verify
the exact head's bot provenance and pass its applicable attestation and CI checks.
Clean merges from the protected base may preserve bot provenance; human-added commits
and merge resolutions do not. A bot author or label alone is not an exemption. See
[Dependabot verification](../attestations-and-ci.md#dependabot-and-other-bot-authored-pull-requests).

For changes requiring human approval, treat approval of an exact PR head as the final
readiness step. Routine documentation that accurately reflects reviewed implementation
does not require owner approval solely because it edits skill or reference material.
Changes to authorization, approval enforcement, or bypass rules require the configured
owner to approve the final PR content, including instruction-only changes and changes
with a `clear` automated risk verdict. An earlier approval is sufficient only when the
hosted approval check reports `approval_carried` after a clean merge from the base or a
clean rebase. Identify that requirement in the PR and wait for approval before merging;
see [CONTRIBUTING.md](../../CONTRIBUTING.md#risk-and-human-review). For a batch, choose the merge order, complete preceding merges,
and refresh the next PR to the latest base before requesting approval. Do not request approval on every intermediate
push or batch approval for heads known to need further base updates.

Before requesting approval, complete requested, internal, and automated reviews,
consolidate their fixes, run applicable checks, push the final head, and wait for CI
and expected reviews to finish. A rate-limited review is still pending. Immediately
before the request, check unresolved comments and confirm the current head and base.

After approval, avoid optional changes. If a necessary fix or base change moves the
head, revalidate. A clean merge from the base or a clean rebase keeps the approval when
the hosted check reports `approval_carried`; otherwise explain why fresh review is
needed. Never bypass the policy or approve your own work. Chat authorization to merge
permits the operation; it does not replace the approval required by GitHub policy.
