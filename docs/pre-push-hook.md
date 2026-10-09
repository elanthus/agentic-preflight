# Pre-push hook

`agentic-preflight init` installs an advisory pre-push hook. For each pushed ref, it
checks the ref tip's Git note in `refs/notes/agentic-preflight` and blocks the update
unless that exact SHA has a valid attestation for publication.

The hook checks ref tips, not every commit newly reachable from them. Remote CI should
verify the pull-request or branch-tip SHA when complete remote enforcement is required.

Two kinds of update need no attestation. A push to `refs/tags/*` is allowed, so a
release tag on a merge commit created by GitHub does not block. An update whose new
tip is already reachable from the remote tip it replaces is allowed, because the
remote already has that commit. Such an update is a rewind, so it still needs
`[hook] allow_force_push = true` unless the tip is unchanged.

Original execution commits travel as data under
`refs/agentic-preflight/evidence/<SHA>`. Updates to this destination namespace are
exempt like tags, including replacement and deletion. Pushing an evidence object to
a branch still requires valid publication evidence.

For opt-in CI test delegation, the hook explicitly uses the publication predicate.
It accepts the schema version 7 `tests_pending` outcome only after validating its local review/docs/lint evidence and
protected-base delegation policy. Tests remain `delegated`, not green or skipped;
the hook never waits for a CI run that cannot exist until the first push.
The required `preflight merge readiness` check and `agentic-preflight ci status`
combine current remote test evidence with local evidence and human approval before
merge. A successful pre-push check alone does not establish merge readiness.

## Failure behavior

The hook never touches the network and never mutates the repository. If
`agentic-preflight` is not on `PATH`, or configuration cannot be loaded, it allows the
push and prints a warning. A teammate who clones a repository without installing the
tool can still push, and a broken local tool does not block every push.

The hook is also bypassable with `git push --no-verify`. That is the documented human
escape hatch; agents using the skill are instructed never to invoke it. Use the
fail-closed remote attestation check described in
[attestations-and-ci.md](attestations-and-ci.md) when the rule must be enforced.

## Existing hooks

`init` asks Git for the hook it will actually execute with
`git rev-parse --git-path hooks/pre-push`. This honors `core.hooksPath`, including
paths configured by tools such as Husky and lefthook. If no hook exists at the effective
path, `init` creates its parent directory and installs the hook there. If a foreign hook
is already present, `init` refuses to change it and reports its real path.

To compose with Husky or lefthook, add `agentic-preflight hook-check` to their
pre-push script at the path reported by `init`, and let its nonzero exit stop the push.
`status` reports `hook.path` and `hook.active`; `active` is true only when the effective
hook contains that command, so a `core.hooksPath` change that bypasses the hook shows up in `status`.

Treat `init --force` as replacement, not composition: it overwrites the existing hook
and removes whatever behavior that hook previously provided.

## Force pushes

Force pushes are rejected by default, even when the new tip has a valid attestation. Set
`[hook] allow_force_push = true` only when repository policy permits history rewrites.
Attestation validation of the new tip still applies.
