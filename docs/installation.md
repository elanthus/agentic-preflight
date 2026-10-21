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
