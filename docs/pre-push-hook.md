# Pre-push hook

`agentic-preflight init` installs an advisory pre-push hook. For each pushed ref, it
checks the ref tip's Git note in `refs/notes/agentic-preflight` and blocks the update
unless that exact SHA has a structurally valid green attestation.

The hook checks ref tips, not every commit newly reachable from them. Remote CI should
verify the pull-request or branch-tip SHA when complete remote enforcement is required.

## Failure behavior

The hook never touches the network and never mutates the repository. If
`agentic-preflight` is not on `PATH`, or configuration cannot be loaded, it allows the
push and prints a warning. That is deliberate: a teammate who clones a repository
without installing the tool must not end up unable to push, and a broken local tool must
not brick the repository.

The hook is also bypassable with `git push --no-verify`. That is the documented human
escape hatch; agents using the skill are instructed never to invoke it. Use the
fail-closed remote attestation check described in
[attestations-and-ci.md](attestations-and-ci.md) when the rule must be enforced.

## Existing hooks

`init` does not compose with an existing `pre-push` hook. If Husky, pre-commit, or a
custom hook already owns that path, `init` refuses to change it. Add
`agentic-preflight hook-check` to the existing hook manually if both are needed.

Treat `init --force` as replacement, not composition: it overwrites the existing hook
and removes whatever behavior that hook previously provided.

## Force pushes

Force pushes are rejected by default, even when the new tip has a valid attestation. Set
`[hook] allow_force_push = true` only when repository policy permits history rewrites.
Attestation validation of the new tip still applies.
