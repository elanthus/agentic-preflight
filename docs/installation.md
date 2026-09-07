# Installation

The [Quickstart](../README.md#quickstart) covers the common path. This page covers
everything else.

## Installing the CLI

From a source checkout, install or update the CLI and all supported agent skills with:

```bash
./install.sh
```

On Windows, use the PowerShell installer instead:

```powershell
.\install.ps1
```

The script installs from that checkout and is safe to rerun after pulling changes. Pass
one or more agent names to limit the skill installation, for example
`./install.sh codex` (`.\install.ps1 codex`). It refuses to overwrite locally modified
or unmanaged skill copies.

Both installers do the same two things in the same order and are kept in step by
matching test suites. The uninstall step below has a PowerShell counterpart too;
everything else on this page is identical on all platforms.

To install the published package instead:

```bash
uv tool install agentic-preflight
```

If `uv` reports that its tool directory is not on `PATH`, run `uv tool update-shell` and
open a new shell before continuing.

## Installing the agent skill

```bash
agentic-preflight integrations install codex claude cursor opencode amp
```

Install only the agents you use. Then invoke the skill with
`$agentic-preflight` in Codex or `/agentic-preflight` in Claude Code. Restart a running
agent if the newly created skill directory is not detected immediately.

The integration installer copies the same bundled skill to each agent's documented
discovery directory: `.agents/skills` for Codex, `.claude/skills` for Claude Code,
`.cursor/skills` for Cursor, `.config/opencode/skills` for opencode, and
`.config/agents/skills` for Amp. It refuses to overwrite local edits unless you pass
`--force`.

## Upgrading

The skill is copied, not linked, so upgrading the CLI does not refresh installed copies.
From a source checkout, rerun `./install.sh`. For a published installation, do both:

```bash
uv tool upgrade agentic-preflight
agentic-preflight integrations update
```

### Upgrading to 0.5.3

Refresh installed skills along with the CLI so the agent follows the current workflow.
Merge polling and automatic post-merge cleanup now default to `false`; an existing
explicit `[pr] automatedCleanup = true` remains enabled. Automatic PR creation and
hosted-check monitoring are unchanged.

Evidence refresh and CI delegation require compatible protected-base consumers before
activation. Upgrade the publisher, hook installation, and trusted verifier together;
follow the [consumer-first rollout](attestations-and-ci.md#evidence-reuse-across-rebases)
and [CI delegation setup](attestations-and-ci.md#delegating-tests-to-trusted-ci).
Existing default configurations continue to execute tests locally and emit schema 4.

## Scopes and other clients

User scope is the default. To check a skill into one repository instead:

```bash
agentic-preflight integrations install codex claude cursor opencode amp --scope project
```

Project scope uses each client's documented repository directory. Codex and Amp share
`.agents/skills`; repeated operations on that shared location remain safe and
idempotent. For another Agent Skills client, `--target PATH` installs beneath a custom
skills directory.

## Inspecting and removing

```bash
agentic-preflight integrations status
agentic-preflight integrations uninstall codex claude cursor opencode amp
```

`status` inspects installed copies. `uninstall` removes managed user copies.

From a source checkout, remove all managed agent skills and the CLI together with:

```bash
./uninstall.sh
```

On Windows:

```powershell
.\uninstall.ps1
```

Pass agent names to limit skill removal, such as `./uninstall.sh codex`
(`.\uninstall.ps1 codex`). The script
first asks you to enter `agentic-preflight:uninstall` in your coding agent for every
repository where `agentic-preflight init` was run, and waits for Enter before it
continues. That trigger removes the current repository's `.agentic-preflight.toml` and
agentic-preflight hook logic while preserving unrelated hooks, run history, and
attestations. The script then removes the managed skills and CLI, refusing to remove
locally modified or unmanaged skill directories, and finishes by printing manual hook
removal instructions for any repository you missed.
