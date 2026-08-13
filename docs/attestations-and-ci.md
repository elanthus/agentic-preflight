# Portable attestations and CI enforcement

Successful merge-back writes a versioned JSON attestation as a Git note on the exact
commit. The version 4 note includes the run identity, commit and tree hashes, dedicated
SHA-256 bindings for user intent and effective configuration, finding status and severity
totals, and a complete stage set. Green lint and test stages include the exact command,
exit code, and SHA-256 of the redacted captured output. Explicitly skipped stages say why
and carry no invented process evidence. Earlier schema versions are rejected.

`agentic-preflight push` atomically pushes the branch and
`refs/notes/agentic-preflight`, so the attestation is not stranded in one clone. Git
does not fetch notes in an ordinary checkout; fetch the dedicated ref before reading or
verifying it:

```bash
git fetch origin refs/notes/agentic-preflight:refs/notes/agentic-preflight
git notes --ref=refs/notes/agentic-preflight show HEAD
agentic-preflight verify HEAD
```

`verify <sha>` exits non-zero when the note is missing or malformed, names another
commit, describes another tree, omits a stage, or claims a green shell stage without its
command, zero exit code, and output hash.

## Required GitHub check

A minimal GitHub Actions required check is:

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0
- run: git fetch origin refs/notes/agentic-preflight:refs/notes/agentic-preflight
- run: pipx install 'agentic-preflight==0.3.0'
- name: Verify the attested commit
  env:
    ATTESTED_SHA: ${{ github.event.pull_request.head.sha || github.sha }}
  run: agentic-preflight verify "$ATTESTED_SHA"
```

Make that job a required status check in branch protection. The local hook remains
fail-open and bypassable so it cannot brick a repository; the required remote check is
what rejects a branch tip without an attestation.

This repository dogfoods that check by installing the verifier from the protected pull
request base commit, then fetching the proposed commit and its note from the
contributor's remote. The pull request cannot change the verifier that judges it.
Governance paths are also listed in `.github/CODEOWNERS`; enable **Require review from
Code Owners** in the branch ruleset because the file alone only requests reviewers.

## High-risk merge handling

High-risk merge handling is enforced by a separate `pull_request_target` workflow that
also installs its policy checker from the protected base and never executes proposed
branch content. It reruns when the head changes or a review is submitted or dismissed.

The default `manual_merge` mode reports success only while GitHub auto-merge is disabled
and instructs the agent never to merge or enable auto-merge. `environment` pauses a
dedicated job at the configured GitHub Environment, and `peer_review` retains the
exact-head approval rule for an eligible person other than the pull-request author.

Make **high-risk human approval** a required status check on `main`; keep **Require
review from Code Owners** enabled as the stricter ownership rule for sensitive paths.
Because the workflow and policy are loaded from the protected base, a pull request that
changes approval mode is judged by the old mode until that change is merged.

## Dependabot and other bot-authored pull requests

Dependabot creates commits on GitHub rather than through your local preflight workflow.
Those commits therefore have no `refs/notes/agentic-preflight` note, and a required
attestation check reports “has no agentic-preflight attestation.” If approval policy is
derived from the same attestation, its checks fail too; those are downstream failures,
not evidence that Dependabot found a vulnerability or that the update itself is bad.

Do not broadly exempt Dependabot from the required check. Group its version updates to
reduce review overhead, then attest each grouped pull request without rewriting its
commit:

1. Add a wildcard `groups` rule to each ecosystem in `.github/dependabot.yml`. This
   repository groups Python dependencies separately from GitHub Actions because Actions
   updates change trusted CI and release code.
2. Check out the Dependabot pull request locally with `gh pr checkout <number>`.
3. Run the normal Agentic Preflight workflow against that exact branch tip. Do not amend,
   squash, or rebase it after review; any new SHA needs its own run.
4. When the workflow reaches its publication gate, run `agentic-preflight push`; it
   atomically pushes the unchanged branch and `refs/notes/agentic-preflight` together.
5. Re-run the failed GitHub Actions workflows from the pull request. A notes-only push
   does not create a new `pull_request` event, so the original failed runs will not
   automatically notice the new attestation.

Keep GitHub Actions updates under normal code-owner and manual-merge policy even when
they are grouped. For a frequently changing dependency pull request, finish review only
after Dependabot has stopped rebasing it; every rebase changes the attested SHA.
