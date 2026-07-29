# Installation

The [Quickstart](../README.md#quickstart) covers the common path. This page covers
everything else.

## Installing the CLI

```bash
uv tool install agentic-preflight
```

If `uv` reports that its tool directory is not on `PATH`, run `uv tool update-shell` and
open a new shell before continuing.

## Installing the agent skill

```bash
agentic-preflight integrations install codex claude
```

Install only the agents you use if you do not need both. Then invoke the skill with
`$agentic-preflight` in Codex or `/agentic-preflight` in Claude Code. Restart a running
agent if the newly created skill directory is not detected immediately.

The integration installer copies the same bundled skill to each agent's documented
discovery directory. It refuses to overwrite local edits unless you pass `--force`.

## Upgrading

The skill is copied, not linked, so upgrading the CLI does not refresh installed copies.
Do both:

```bash
uv tool upgrade agentic-preflight
agentic-preflight integrations update
```

## Scopes and other clients

User scope is the default. To check a skill into one repository instead:

```bash
agentic-preflight integrations install codex claude --scope project
```

For another agent that supports Agent Skills, `--target PATH` installs beneath a custom
skills directory.

## Inspecting and removing

```bash
agentic-preflight integrations status
agentic-preflight integrations uninstall codex claude
```

`status` inspects installed copies. `uninstall` removes managed user copies.
