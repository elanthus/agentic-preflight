# ADR 0004: Sign the protected verifier's result, not the local run

- Status: Accepted
- Date: 2026-11-19
- Accepted: 2026-11-22

This ADR answers the question that issue #25 left open: what a signature on an attestation would prove.
The `sign` job, `data.statement`, `verify SHA --signature`, and the `[attestation] require_signature` policy key are implemented.
"Implementation notes" records where the implementation differs from the text, and "Resolved questions" records how the open questions were settled.

## Context

A successful `mergeback` writes an attestation as a Git note on the exact commit, and `push` publishes it under `refs/notes/agentic-preflight`.
The note is not signed.
Anyone who can update that ref can replace a note, as [Attestations are not signatures](../limits.md#attestations-are-not-signatures) says.
The records bundle and the HTML report from `audit` are not signed either.

Issue #25 asks for an attestation that the coding agent cannot forge.
It sets one hard rule: the signing authority must be outside the credentials and the execution environment that the agent can reach.
A key in the repository, in the developer's shell, or in a CI job that runs branch code does not satisfy it.

The project already has a job that the agent cannot reach.
`.github/workflows/attestation.yml` runs on `pull_request_target`, so its definition comes from the default branch.
It checks out and installs only the base commit.
It fetches the proposed commit, the notes ref, and the evidence refs as inert objects, and runs `verify SHA --base BASE_SHA` from the base checkout.
No file from the pull request is executed.

The release workflow already signs without a long-lived key.
`.github/workflows/release.yml` requests `id-token: write` and `attestations: write`, and uses `actions/attest` for build provenance and for the SBOM.

Three prerequisites from the 2026-11-11 notes on issue #25 have changed since then:

- **Reproducible digests.** A signer must rebuild each digest from the commits.
  `COMPATIBILITY.md` records that review patches no longer depend on Git configuration or on Git attributes, so the base-trusted verifier can rebuild the review digest on another machine.
- **A canonical byte form.** This ADR avoids the need for one. See "The signed statement".
- **A repository identity.** Telemetry derives one that is the same in every clone, `repo.root_commit` in `agentic_preflight/telemetry.py`.

### The limit that no signer in CI can remove

The review, docs, lint, and test stages run on the developer's machine.
A signer in CI cannot see them run.
A writer of the notes ref can record a stage result for a stage that never ran, and the note will still be well formed.

A signer can remove this limit in only two ways.
It can run the stages again, which is CI and makes the local attestation redundant for those stages.
Or the stages can run in an environment that attests its own execution, which the project does not have.

## Decision

Sign what the protected verifier observed, and nothing else.
Keep the local stage results as an unsigned claim, and label them as one.

The result is a record with two tiers:

| Tier | Statement | Who makes it | Signed |
|---|---|---|---|
| Verified | The verifier, running code from the protected base, found a note for this commit. The note's digests match the commits, and its evidence satisfies the policy committed at the base. | The verifier workflow | Yes |
| Claimed | The stages ran locally with the commands, exit codes, and output digests that the note records. | The run on the developer's machine | No |

### The signer

Add a `sign` job to `.github/workflows/attestation.yml`.
It runs only after the `attestation` job succeeds.
It uses the workflow's OIDC identity for keyless signing, as the release workflow does.
There is no long-lived key to store, rotate, or revoke.

The `attestation` job keeps `contents: read` and no other permission.
Only the `sign` job gets `id-token: write` and `attestations: write`.
The `sign` job checks out nothing from the pull request and runs no code from it.
It receives the note's digest and the fields of the statement from the `attestation` job as job outputs.

This is a new risk, and the ADR accepts it deliberately: a `pull_request_target` workflow that until now held only read permissions gains a job that can mint an identity token.
The separation into two jobs limits the exposure to a job that handles no pull request content.

### The signed statement

The subject is the note blob as stored in the notes ref: its SHA-256 digest and its Git object ID.
A signature covers bytes, and the stored blob already is one fixed sequence of bytes.
So the project does not need a canonical serialization of the note model.
A change to how the note is written changes the blob, and the verifier signs whatever blob it verified.

The statement carries these fields beside the subject:

- The repository identity: the root commit, and the forge's repository ID.
- The attested commit and its tree.
- The base commit that supplied the policy, and the SHA-256 digest of the `.agentic-preflight.toml` at that commit.
- The note's schema version.
- The commit of the verifier code, which is the base commit.
- The records bundle commit, when the note names one.

The repository identity and the attested commit prevent replay in another repository or on another commit.
The base commit and the policy digest prevent a downgrade to an older policy.
The note itself holds the event anchor, the producer, and the transcript digests, so the subject digest covers them.

### Where the signature lives

The `sign` job stores the signature in the forge's attestation store, with the note blob's digest as the subject.
It writes nothing to the repository, so no job in this workflow needs `contents: write`.

A verifier therefore needs one of two things: access to the forge, or a signature bundle that someone downloaded from the forge earlier.
Issue #25 asks that a third-party clone can verify offline.
This decision meets that requirement only for a clone that also holds a downloaded bundle.
A clone without one, and without the forge, reports the signature as `absent`.

A repository administrator on the forge may be able to delete a stored attestation.
That is not verified here.
If it is so, deletion gives `absent`, never a forged `verified`.

### What the signature proves and does not prove

It proves that this repository's verifier workflow, at a protected ref, accepted this exact note for this exact commit under this exact base policy.

It does not prove any of the following:

- That a stage ran. This stays in the claimed tier.
- That the review was good.
- Who acted. The client, the models, and the push authorization stay self-reported.
- Anything about events after merge-back. The note is written before `gate` and `push`, so its anchor does not cover `gate_minted` or `push_authorized`.
- Anything about a commit that never had a pull request. The signer runs only on `pull_request_target`.

## Verification

`verify SHA` looks up the signature by the digest of the note blob that is now in the notes ref.
It reports the result in `data.signature` with one of three states:

| State | Meaning |
|---|---|
| `verified` | A signature exists for this note blob, its signer is this repository's verifier workflow, and its statement names this repository, commit, tree, and base policy. |
| `invalid` | A signature exists for this note blob and fails its cryptographic or identity check. |
| `absent` | No signature was found for this note blob. |

A note that was replaced after it was signed has a different digest, so the lookup finds nothing and the state is `absent`.
`absent` also cannot distinguish a deleted signature from one that was never made, or from a forge that the verifier could not reach.
So the policy at the trusted base decides whether `absent` is a failure.
A new key in `.agentic-preflight.toml`, `[attestation] require_signature`, makes the signature mandatory for `verify --signature`.
The verifier reads it from the trusted base, like every other policy value, so a branch cannot turn it off.

The signature check shells out to the forge's CLI and adds no signing library to the core dependencies.
This follows the dependency policy of [ADR 0003](0003-ship-a-packaged-reviewer.md).

Until a signature is `verified`, the README, `docs/limits.md`, and the `audit` report keep the present wording that the record is not signed.
When it is `verified`, they name the verified tier and still label the claimed tier as unsigned.

## Consequences

An auditor can confirm, long after the pull request closed, that the note in the notes ref is the note the protected verifier accepted.
Where the base policy makes the signature mandatory, a replaced note fails verification instead of passing silently.
Without that policy, a replaced note reads as `absent`, the same as a note that was never signed.

The guarantee exists only where the forge supplies a workload identity and a protected workflow.
The first implementation is specific to GitHub.
A repository on another forge, or one that pushes without pull requests, keeps the unsigned guarantee of today.
The signatures do not move with the repository: a mirror, or a repository that changes forge, keeps its notes and loses the signatures unless someone downloaded the bundles first.

The signature adds no assurance about local execution.
A reader who skips the tier labels can overestimate it.
The labels in `verify`, in `audit`, and in the documentation are therefore part of this decision, not a later improvement.

The verifier workflow gains a job with an identity token.
A change to `.github/workflows/attestation.yml` needs the same review as a change to the release workflow.

Adversarial tests must cover a forged note, a replaced note, a statement replayed from another repository or commit, a downgraded policy, and a missing signature.

## Options set aside

- **Notes signed with an SSH or GPG key that the developer holds.**
  Git supports it and it needs little infrastructure.
  It fails the rule of issue #25 when the agent runs as the developer and can reach the key or the signing agent.
  It can serve another claim later: that a named person approved, if each use of the key needs a touch or a passphrase.
- **A key in a CI secret.**
  It fails the rule if any workflow that runs branch code can read the secret.
- **The signer runs the stages again.**
  It closes the execution gap, but it is CI, and it removes the purpose of a local attestation for those stages.
- **A second ref in Git that only the workflow can update.**
  It travels with every clone and verifies offline.
  The `sign` job would need `contents: write` under `pull_request_target`, so a fault in that job could change the repository.
  Pushes from concurrent pull requests to one ref would also need retry logic.
- **A copy of the signature bundle in an evidence ref, written by an unprivileged party.**
  A bundle cannot be altered without detection, so its writer does not need to be trusted.
  This gives offline verification in every clone without `contents: write` on the signer.
  It is not rejected. It is deferred until offline verification without a prior download proves necessary.

## Implementation notes

These notes record what the implementation found. They change no decision above.

- **Implemented:** the `sign` job makes the signature, and `verify SHA --signature` reads it. See [Signature check](../attestations-and-ci.md#signature-check).
  `[attestation] require_signature` makes `absent` fail with `signature_required`. See [Signature requirement](../configuration.md#signature-requirement-attestation).
- **An unreadable base policy fails closed.** `verify --signature` reads the base policy a second time for the signer's branch and the key.
  When that read fails and no signature is found, nothing shows that the key is off, so the check fails with `signature_required`, and `data.signature.required` is `null`.
- **The predicate is narrower than the decision.** It carries the root commit and not the forge's repository ID.
  The signing certificate names the repository, and the workflow cannot falsify the certificate.
- **The reader compares only the commit and the tree of the predicate with the clone.** It pins the certificate's repository, workflow, and source ref, and the subject digest covers the note.
- **The caller names the repository.** The reader takes it from `GITHUB_REPOSITORY` or from `origin` and reports it in `data.signature.repository`.
  A fork shares the root commit, so the root commit in the predicate cannot tell a fork from its parent.
  A key at the trusted base that names the repository was considered and set aside, because it adds configuration that a repository must set before the check works.
- **A failed verify step runs twice.** `gh` does not say whether it failed on a bad signature or on a failed fetch, so `invalid` is reported only after a second failure.
- **The check is a flag, not a default.** The hosted `attestation` job runs `verify` before the signature exists.
  A policy key that made `absent` fail every `verify` would fail that job.
  So the signature check runs only with `--signature`, and the policy key, when it is added, applies only to that check.
- **The signer signs only pull requests that target the default branch.** Since 2025-12-08, GitHub runs a `pull_request_target` workflow from the default branch, whatever branch the pull request targets.
  The workflow file is therefore always the protected one, but the `attestation` job checks out and runs the verifier of the target branch.
  A person who can push to an unprotected branch could change that verifier and have its output signed.
  The `sign` job therefore requires the target branch to be the default branch.
  The reader requires the signing certificate's source ref to be the branch that `[general] base_ref` names at the trusted base, which must be the default branch.
- **The statement uses the base that `verify` used.** `verify` returns the base commit it resolved, and the statement and the signature check take the policy from that commit. They also require the stored note blob to decode to the note that `verify` accepted.
- **Plan limits.** The `actions/attest` README states that artifact attestations are available in public repositories on every plan, need GitHub Enterprise Cloud in private and internal repositories, and are not supported on GitHub Enterprise Server.

## Resolved questions

1. **The policy key.** It is `[attestation] require_signature`, a strict boolean that defaults to `false`.
   No release will change the default.
   A signature exists only on GitHub, in repositories whose plan provides artifact attestations, and for changes that reach the default branch through a pull request; a default of `true` would fail every other repository.
   `[attestation]` is excluded from run snapshots and configuration digests, like `[records]`.
   The key applies only to `verify --signature`, as "Implementation notes" explains.
2. **Events after merge-back.** Deferred.
   Covering `gate_minted` and `push_authorized` needs a second signed statement, made at merge or by the signer, over the event log after the push.
   That is a separate decision for a later ADR. Until then, only the event log's own hash chain covers these events, as [What you can audit](../limits.md#what-you-can-audit) says.
