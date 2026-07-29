"""The pre-push hook predicate.

A pure function of Git notes and the push refs. It reads the attestation note
on each commit and no run state: no network and no mutation.
That is what keeps it inside its latency budget and what makes it safe to run on
every push.

The hook cannot call back up to the agent — it is a subprocess of the agent's
own ``git push``. So its only lever is to fail with a message written *for an
agent to read*, naming the Claude and Codex invocations so the block doubles as
a skill trigger that loops the agent back into the gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .attestation import NOTES_REF
from .envelope import ExitCode

ZERO_SHA = "0" * 40

HOOK_SCRIPT = """#!/bin/sh
# Installed by agentic-preflight. Blocks pushes of commits with no green run.
# If agentic-preflight is not on PATH this allows the push and warns: a broken tool
# must never leave a repository you cannot push from.
if ! command -v agentic-preflight >/dev/null 2>&1; then
  echo "agentic-preflight: not found on PATH, skipping the quality gate (warn only)" >&2
  exit 0
fi
exec agentic-preflight hook-check
"""


@dataclass
class RefUpdate:
    local_ref: str
    local_sha: str
    remote_ref: str
    remote_sha: str

    @property
    def is_deletion(self) -> bool:
        return set(self.local_sha) == {"0"}

    @property
    def is_attestation_note(self) -> bool:
        return self.local_ref == NOTES_REF


def parse_stdin(text: str) -> list[RefUpdate]:
    """Parse git's pre-push stdin protocol: <local ref> <local sha> <remote ref> <remote sha>."""
    updates: list[RefUpdate] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        updates.append(RefUpdate(*parts))
    return updates


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    message: str = ""


def evaluate(
    updates: list[RefUpdate],
    *,
    is_ancestor,
    has_attestation,
    allow_force_push: bool = False,
) -> Decision:
    """Decide the push. ``is_ancestor(a, b)`` is injected so this stays pure."""
    for update in updates:
        if update.is_deletion or update.is_attestation_note:
            continue

        forced = update.remote_sha != ZERO_SHA and not is_ancestor(
            update.remote_sha, update.local_sha
        )
        if forced and not allow_force_push:
            return Decision(
                allowed=False,
                reason="force push",
                message=_block_message(
                    update.local_sha,
                    headline="force push",
                    reason=(
                        "the remote tip is not an ancestor of yours, so this rewrites "
                        "history the remote already has"
                    ),
                    fix="rebase onto the remote tip, or set [hook] allow_force_push = true",
                ),
            )

        if not has_attestation(update.local_sha):
            return Decision(
                allowed=False,
                reason="not green",
                message=_block_message(
                    update.local_sha,
                    headline="no green run recorded for this exact SHA",
                    reason="no valid attestation note is attached to this exact SHA",
                    fix=(
                        "invoke the skill (/agentic-preflight in Claude Code, "
                        "$agentic-preflight in Codex)"
                    ),
                ),
            )

    return Decision(allowed=True)


def _block_message(sha: str, *, headline: str, reason: str, fix: str) -> str:
    """Written for an agent to read and act on.

    The headline must state the *actual* cause. A force-push block that also
    claimed the commit was unverified would send the agent to re-run the gate
    when the real problem is the rewrite.
    """
    return "\n".join(
        [
            "agentic-preflight: push blocked.",
            f"  commit: {sha[:7]} ({headline})",
            f"  reason: {reason}",
            f"  fix:    {fix}",
            "  bypass: git push --no-verify   (documented escape hatch)",
        ]
    )


def install(git_dir: Path | str, *, force: bool = False) -> tuple[Path, bool]:
    """Write the pre-push hook. Returns (path, newly_written).

    Refuses to clobber a hook we did not write: someone else's pre-push hook is
    important to their workflow, and silently replacing it would be exactly the kind
    of unreviewed change this tool exists to prevent.
    """
    hooks_dir = Path(git_dir) / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    path = hooks_dir / "pre-push"

    if path.exists() and not force:
        existing = path.read_text()
        if "agentic-preflight hook-check" not in existing:
            raise FileExistsError(str(path))
        return path, False

    path.write_text(HOOK_SCRIPT)
    path.chmod(0o755)
    return path, True


BLOCK_EXIT = ExitCode.HOOK_BLOCK
