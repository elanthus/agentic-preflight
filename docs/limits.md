# Limits

The [README](../README.md#limits) states the three that matter most: the gate is advisory
rather than a security boundary, `git push --no-verify` defeats it by design, and the
confirmation token is ceremony rather than a secret. This page covers the rest.

## Amending invalidates green

The attestation note is bound to an exact SHA, so any amend, rebase, or squash forces a
fresh run. A green result describes one tree; once that tree no longer exists the result
describes nothing.

Cherry-picked merge-back is handled via tree-equivalence attestation.

## Isolated worktrees can differ from your environment

In-place mode deliberately uses the checkout's existing dependencies and ignored files. An
isolated runner does not inherit the source checkout's `.venv` or `.env`.

- Configure `[worktree] setup_command` for non-Node dependencies.
- Configure `copy_files` for ignored files such as `.env`.
- Node lockfiles are handled automatically. See
  [worktree-modes.md](worktree-modes.md#node-dependencies).

Use `--baseline` so a pre-existing failure is reported rather than blamed on your diff.

## Runtime pins are activated explicitly

Non-interactive agent shells often miss interactive version-manager shims, so a stage can
resolve a different toolchain than your terminal does. Stages detect committed Node pins
for NVM, Volta, asdf, mise, fnm, and nodenv.

A missing pinned manager fails clearly instead of silently running a different system
Node. `init` reports unpinned Node projects, so a fresh clone, CI, and the gate can agree
on the version.

If a stage is green in your shell and red under the gate, compare the toolchain version
*inside the stage* before debugging the code. A native module built for another ABI fails
as missing bindings, not as a version error.

## What a green run does and does not prove

It proves what the gate reported: that the agent submitted findings, that the configured
commands exited zero against that exact SHA, and which judgment calls were recorded along
the way.

It does not prove the review was good. The same diff reviewed twice can yield different
findings. Treat the Git note as an audit trail, and note that it substitutes for neither
CI nor a human reviewer. It is not a signature: anyone allowed to update the notes ref
can replace it.
