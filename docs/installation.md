# Installation

The [Quickstart](../README.md#quickstart) covers the common path.
This page covers the other paths.

## Installing the CLI

From a source checkout, install or update the CLI and all supported agent skills:

```bash
./install.sh
```

On Windows, use the PowerShell installer:

```powershell
.\install.ps1
```

The script installs from that checkout.
You can run it again after you pull changes.
Pass one or more agent names to limit the skill installation, for example `./install.sh codex` or `.\install.ps1 codex`.
The script refuses to overwrite a locally modified or unmanaged skill copy.

Both installers do the same two things in the same order.
The PowerShell installer also stops first when `git` is not installed.
Matching test suites keep them in step.
The uninstall step below also has a PowerShell counterpart.
Everything else on this page is identical on all platforms.

To install the published package instead:

```bash
uv tool install agentic-preflight
```

If `uv` reports that its tool directory is not on `PATH`, run `uv tool update-shell`.
Then open a new shell before you continue.

## Installing the agent skill

```bash
agentic-preflight integrations install codex claude cursor opencode amp
```

Install only the agents that you use.
Then invoke the skill with `$agentic-preflight` in Codex or `/agentic-preflight` in Claude Code.
Restart a running agent if it does not detect the new skill directory immediately.

The integration installer copies the same bundled skill to each agent's documented discovery directory:

| Agent | User-scope directory |
|---|---|
| Codex | `.agents/skills` |
| Claude Code | `.claude/skills` |
| Cursor | `.cursor/skills` |
| opencode | `.config/opencode/skills` |
| Amp | `.config/agents/skills` |

The installer refuses to overwrite local edits unless you pass `--force`.
If a copy fails, the destination can be absent or partial.
Correct the filesystem failure. Then run `integrations install` again with `--force` to repair that copy.

Copies do not follow the source checkout or the installed package.
Thus, a managed skill can be `outdated` after a CLI or source update. This is not an installer failure.
Use `agentic-preflight integrations status` to inspect each bundle.
To refresh from a reviewed source checkout, run `./install.sh`.
To refresh after you upgrade a published tool, run `agentic-preflight integrations update`.
Both paths replace an unmodified managed bundle.
Both paths refuse a locally modified or unmanaged bundle.
The `install.sh` script accepts only agent names. It has no force mode.
To replace a modified bundle, first inspect and preserve the customizations.
Then reinstall the reviewed checkout and use the explicit lifecycle command, for example `agentic-preflight integrations update codex claude --force`.

## Upgrading

The skill is copied, not linked.
An upgrade of the CLI does not refresh the installed copies.
From a source checkout, run `./install.sh` again.
For a published installation, do both:

```bash
uv tool upgrade agentic-preflight
agentic-preflight integrations update
```

For an upgrade from 0.6.0 to 0.7.0, follow the [upgrade guide](upgrade-0.7.md) before you start new runs.
Align the protected verifier, remove retired configuration, and inspect active runs.
New runs always execute applicable lint and test stages locally.

Earlier versions wrote run and findings journals.
After an upgrade from such a version, the next `start` in a source worktree retires an interrupted run that has a pending journal.
Validation then starts fresh.
`status` directs an affected run to restart.
Audit history and user work stay available for normal safe cleanup.
Package installation does not scan or delete repository runs.
See [compatibility](../COMPATIBILITY.md#saved-pending-run-updates).

### Upgrading from 0.7 to 0.8

Upgrade the CLI, copied skills, and protected CI verifier together. Use the same reviewed
0.8 release for producers and verifiers; the commands below select the published 0.8.1 release.

1. Before upgrading, run `agentic-preflight status --all` in each clone.
   Preserve unmerged fixes and local skill customizations, and record the current verifier pin.
2. Prepare the protected-base CI verifier to use the selected release before publishing new evidence.
   For a package-based verifier, pin `agentic-preflight==0.8.1`.
   For a source-based verifier, use the matching reviewed source revision.
   See [protected-base fetch and verification](attestations-and-ci.md#protected-base-fetch-and-verification).
3. Upgrade every machine that shares a clone before enabling new configuration or letting
   the new CLI write shared run records:

   ```bash
   uv tool install --force 'agentic-preflight==0.8.1'
   agentic-preflight --version
   agentic-preflight integrations status
   agentic-preflight integrations update
   agentic-preflight integrations status
   ```

   Expect version 0.8.1 and `current` for each managed installed integration.
   From a reviewed 0.8.1 source checkout, use `./install.sh` or `.\install.ps1` instead.
   Update project copies with `integrations update --scope project` and custom targets with `--target PATH`.
   Preserve modified or unmanaged skill copies before an intentional `--force` replacement.
   Restart running agents so they read the refreshed skills.
4. Commit any repository configuration changes before starting the affected run.
   Version 0.8 removes no 0.7 configuration keys, but 0.7 rejects `[telemetry]`
   (including `hook_log`) and `[review.contracts]`.
   Version 0.7 also rejects new run fields such as `producer`, `started_at`, and `response_reason`.
   Use 0.8 or later to inspect and clean up runs written by 0.8; downgrading the executable
   does not make those records readable by 0.7. Preserve history rather than editing records or digests.
5. In each source worktree, run `status` and follow its returned command.
   Validate an ordinary branch through the local stages and the protected hosted checks before
   expanding the rollout. Applicable lint and tests still run locally in every new run.

Run schema 2, attestation schema 7, and fingerprint version 2 are unchanged.
Notes and run records written by 0.7 remain readable by 0.8.
Schema numbers alone do not establish compatibility with an older reader.
See [0.8 compatibility changes](../COMPATIBILITY.md#upgrading-from-070-to-080) for details.

## Scopes and other clients

User scope is the default.
To check a skill into one repository instead:

```bash
agentic-preflight integrations install codex claude cursor opencode amp --scope project
```

Project scope uses each client's documented repository directory.
Codex and Amp share `.agents/skills`.
Repeated operations on that shared location are safe and idempotent.
For another Agent Skills client, `--target PATH` also installs under a custom skills directory.

## Inspecting and removing

```bash
agentic-preflight integrations status
agentic-preflight integrations uninstall codex claude cursor opencode amp
```

`status` inspects the installed copies.
`uninstall` removes managed user copies.

From a source checkout, remove all managed agent skills and the CLI together:

```bash
./uninstall.sh
```

On Windows:

```powershell
.\uninstall.ps1
```

Pass agent names to limit the skill removal, for example `./uninstall.sh codex` or `.\uninstall.ps1 codex`.

The script first asks you to enter `agentic-preflight:uninstall` in your coding agent for every repository where you ran `agentic-preflight init`.
It waits for Enter before it continues.
That trigger removes the current repository's `.agentic-preflight.toml` file and the agentic-preflight hook logic.
It preserves unrelated hooks, run history, and attestations.
The script then removes the managed skills and the CLI.
It refuses to remove a locally modified or unmanaged skill directory.
Finally, it prints manual hook-removal instructions for any repository that you missed.
