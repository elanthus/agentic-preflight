# Pre-push hook

`agentic-preflight init` installs an advisory pre-push hook.
For each pushed ref, the hook reads the Git note of the ref tip in `refs/notes/agentic-preflight`.
It blocks the update unless that exact SHA has a valid attestation for publication.

The hook checks ref tips. It does not check every commit that becomes reachable from them.
When you require complete remote enforcement, remote CI must verify the pull-request SHA or the branch-tip SHA.
Fetch that exact commit, its notes, and `refs/agentic-preflight/evidence/*` from the contributor remote.
Then run `agentic-preflight verify SHA` offline.
See the [protected-base workflow](attestations-and-ci.md#protected-base-fetch-and-verification).

Four kinds of update need no attestation:

- A push to `refs/tags/*`. A release tag on a merge commit that GitHub created does not block.
- An update whose new tip is already reachable from the remote tip that it replaces. The remote already has that commit.
- A deletion of a remote ref.
- A push of the attestation notes ref, `refs/notes/agentic-preflight`.

The second kind of update is a rewind.
It still needs `[hook] allow_force_push = true` unless the tip is unchanged.

Original execution commits travel as data under `refs/agentic-preflight/evidence/<SHA>`.
Updates to this destination namespace are exempt, like tags. This includes replacement and deletion.
A push of an evidence object to a branch still requires valid publication evidence.

## Failure behavior

The hook never uses the network. It never changes the repository.
If `agentic-preflight` is not on `PATH`, the hook permits the push and prints a warning.
That push is not in the hook decision log.
If the configuration cannot be loaded, the hook also permits the push and prints a warning.
The log records that decision as `config_unavailable` when the `[telemetry]` preference is still known.
A teammate who clones a repository without the tool can still push.
A broken local tool does not block every push.

`git push --no-verify` bypasses the hook.
A bypassed push leaves no line in the hook decision log.
This bypass is the documented escape hatch for people.
The skill instructs agents never to use it.
When the rule must be enforced, use the fail-closed remote attestation check in [attestations-and-ci.md](attestations-and-ci.md).

## Hook decision log

`hook-check` appends each decision to `hook-decisions.jsonl` in your local telemetry directory.
The line records the time, the repository, the remote name, the reason code, and each ref update with a seven-character SHA.
The hook writes the line after it decides. A log failure or a slow write does not change the exit code or the message.
The file is outside the repository, so the hook still does not change the repository.
Set `[telemetry] hook_log = false` to stop the log.
A repository under the system temporary directory without a network remote logs nothing unless `[telemetry] path` is set.
See [Temporary repositories](local-telemetry.md#temporary-repositories).
See [Hook decisions](local-telemetry.md#hook-decisions) for the fields, the reason codes, and the report.

The installed hook gives `hook-check` the remote name in the `AGENTIC_PREFLIGHT_PUSH_REMOTE` environment variable.
Git gives the name to the hook as its first argument.
An earlier version of `hook-check` ignores the variable, so a newer hook does not block a push with an older tool.
A hook that an earlier version installed does not set the variable. Its log lines have `remote = null`.

## Existing hooks

`init` asks Git for the hook that Git will execute. It uses `git rev-parse --git-path hooks/pre-push`.
This honors `core.hooksPath`, including paths that tools such as Husky and lefthook configure.
If no hook exists at the effective path, `init` creates the parent directory and installs the hook there.
If a foreign hook is already present, `init` refuses to change it and reports its real path.
If the hook is exactly a hook that an earlier version of `init` installed, `init` replaces it with the current hook.
`init` does not change a hook that contains other changes.

To compose with Husky or lefthook, add `agentic-preflight hook-check` to their pre-push script at the path that `init` reports.
Let its nonzero exit stop the push.
To record the remote name in the decision log, set the variable before the command:
`AGENTIC_PREFLIGHT_PUSH_REMOTE="$1" agentic-preflight hook-check`.
`status` reports `hook.path` and `hook.active`.
`active` is true only when the effective hook contains that command.
Thus, a `core.hooksPath` change that bypasses the hook is visible in `status`.

`init --no-hook` writes the configuration and skips the hook; `status` then reports `hook.active` as false.

`init --force` is a replacement, not a composition.
It overwrites the existing hook and removes the behavior that the hook provided.

## Force pushes

The hook rejects force pushes by default, even when the new tip has a valid attestation.
Set `[hook] allow_force_push = true` only when repository policy permits history rewrites.
Attestation validation of the new tip still applies.
