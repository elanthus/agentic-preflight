"""The pre-push hook predicate.

A pure function of Git notes and the push refs. It reads the attestation note
on each commit and no run state: no network and no mutation.
That is what keeps it inside its latency budget and what makes it safe to run on
every push. Each :class:`Decision` carries a machine-readable reason code and the
outcome of every update, so ``hook-check`` can log the decision outside the
repository (see :mod:`agentic_preflight.hooklog`) without parsing its prose.

The hook cannot call back up to the agent; it is a subprocess of the agent's
own ``git push``. So its only lever is to fail with a message written *for an
agent to read*, naming the Claude and Codex invocations so the block doubles as
a skill trigger that loops the agent back into the gate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import gitx
from .attestation import NOTES_REF
from .evidence_transport import REF_PREFIX

ZERO_SHA = "0" * 40

#: The environment variable through which the installed hook passes Git's first
#: pre-push argument, the remote name, to ``hook-check``. An environment variable
#: rather than an argument, so an older ``hook-check`` on PATH ignores it instead
#: of failing the push with a usage error.
REMOTE_ENV = "AGENTIC_PREFLIGHT_PUSH_REMOTE"

HOOK_SCRIPT = """#!/bin/sh
# Installed by agentic-preflight. Blocks pushes of commits with no green run.
# If agentic-preflight is not on PATH this allows the push and warns: a broken tool
# must never leave a repository you cannot push from.
if ! command -v agentic-preflight >/dev/null 2>&1; then
  echo "agentic-preflight: not found on PATH, skipping the quality gate (warn only)" >&2
  exit 0
fi
# The remote's name, for the local hook decision log. Older versions ignore it.
AGENTIC_PREFLIGHT_PUSH_REMOTE="$1"
export AGENTIC_PREFLIGHT_PUSH_REMOTE
exec agentic-preflight hook-check
"""

#: Hook scripts that earlier versions installed, byte for byte. ``install``
#: replaces one of these with :data:`HOOK_SCRIPT`; any other content is left alone.
PREVIOUS_HOOK_SCRIPTS: frozenset[bytes] = frozenset(
    {
        b"""#!/bin/sh
# Installed by agentic-preflight. Blocks pushes of commits with no green run.
# If agentic-preflight is not on PATH this allows the push and warns: a broken tool
# must never leave a repository you cannot push from.
if ! command -v agentic-preflight >/dev/null 2>&1; then
  echo "agentic-preflight: not found on PATH, skipping the quality gate (warn only)" >&2
  exit 0
fi
exec agentic-preflight hook-check
""",
    }
)

HOOK_MARKER = b"agentic-preflight hook-check"

# -- reason codes -------------------------------------------------------------
# Every decision has exactly one. They are a closed set: the hook decision log and
# the report count them, so a new code is a documented compatibility change.

#: Every update that needed an attestation had a valid one.
ATTESTED = "attested"
#: No update needed an attestation, and at least one moved a ref to a commit the
#: remote already has.
ALREADY_ON_REMOTE = "already_on_remote"
#: Every update deleted a ref.
DELETION = "deletion"
#: Every update was exempt (a tag, the attestation notes ref, an evidence ref, or a
#: deletion), and not every update was a deletion.
EXEMPT = "exempt"
#: Git sent no update to check.
NO_UPDATES = "no_updates"
#: ``hook-check`` could not load the repository or its configuration and allowed
#: the push without evaluating it. Set by ``hook-check``, never by :func:`evaluate`.
CONFIG_UNAVAILABLE = "config_unavailable"
#: An update would rewrite history the remote has, and force pushes are not allowed.
FORCE_PUSH_REFUSED = "force_push_refused"
#: An update's new tip has no valid attestation note for publication.
MISSING_ATTESTATION = "missing_attestation"

ALLOWED_REASONS = (ATTESTED, ALREADY_ON_REMOTE, DELETION, EXEMPT, NO_UPDATES, CONFIG_UNAVAILABLE)
BLOCKED_REASONS = (FORCE_PUSH_REFUSED, MISSING_ATTESTATION)
REASONS = ALLOWED_REASONS + BLOCKED_REASONS

# Per-update outcomes beyond the reason codes above. None of them decides a push.
TAG = "tag"
ATTESTATION_NOTE = "attestation_note"
EVIDENCE_REF = "evidence_ref"
#: Not evaluated: an earlier update blocked the push, or the configuration was
#: unavailable.
NOT_EVALUATED = "not_evaluated"
UPDATE_OUTCOMES = (
    DELETION,
    ATTESTATION_NOTE,
    EVIDENCE_REF,
    TAG,
    ALREADY_ON_REMOTE,
    ATTESTED,
    FORCE_PUSH_REFUSED,
    MISSING_ATTESTATION,
    NOT_EVALUATED,
)


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
        return self.remote_ref == NOTES_REF

    @property
    def is_tag(self) -> bool:
        return self.remote_ref.startswith("refs/tags/")


def parse_stdin(text: str) -> list[RefUpdate]:
    """Parse git's pre-push stdin protocol: <local ref> <local sha> <remote ref> <remote sha>."""
    updates: list[RefUpdate] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        updates.append(RefUpdate(*parts))
    return updates


@dataclass(frozen=True)
class UpdateOutcome:
    """How the hook treated one pushed ref update."""

    update: RefUpdate
    #: One of :data:`UPDATE_OUTCOMES`.
    outcome: str
    #: Whether the update rewrites history the remote has. ``None`` when the hook
    #: did not evaluate it, because it was exempt or not reached.
    force: bool | None


@dataclass
class Decision:
    allowed: bool
    #: The machine-readable reason code, one of :data:`REASONS`.
    reason: str = ""
    #: The prose written to stderr for a blocked push. Empty when allowed.
    message: str = ""
    #: One outcome for each update, in the order Git sent them.
    updates: list[UpdateOutcome] = field(default_factory=list)


def _exemption(update: RefUpdate) -> str | None:
    """The kind of an update that needs no attestation, else ``None``."""
    if update.is_deletion:
        return DELETION
    if update.is_attestation_note:
        return ATTESTATION_NOTE
    if update.remote_ref.startswith(REF_PREFIX):
        return EVIDENCE_REF
    if update.is_tag:
        return TAG
    return None


def _allowed_reason(outcomes: Sequence[UpdateOutcome]) -> str:
    kinds = {item.outcome for item in outcomes}
    if not kinds:
        return NO_UPDATES
    if ATTESTED in kinds:
        return ATTESTED
    if ALREADY_ON_REMOTE in kinds:
        return ALREADY_ON_REMOTE
    if kinds == {DELETION}:
        return DELETION
    return EXEMPT


def unevaluated(updates: Sequence[RefUpdate], reason: str) -> Decision:
    """An allowed decision made without evaluating ``updates``, such as ``config_unavailable``."""
    return Decision(
        allowed=True,
        reason=reason,
        updates=[UpdateOutcome(update, NOT_EVALUATED, None) for update in updates],
    )


def _blocked(
    reason: str, message: str, outcomes: list[UpdateOutcome], rest: Sequence[RefUpdate]
) -> Decision:
    remaining = [UpdateOutcome(update, NOT_EVALUATED, None) for update in rest]
    return Decision(allowed=False, reason=reason, message=message, updates=outcomes + remaining)


def evaluate(
    updates: list[RefUpdate],
    *,
    is_ancestor,
    has_attestation,
    allow_force_push: bool = False,
) -> Decision:
    """Decide the push. ``is_ancestor(a, b)`` is injected so this stays pure.

    The first update that blocks decides the push, and later updates are not
    evaluated. The reason code comes from the branch that decides, never from
    the message.
    """
    outcomes: list[UpdateOutcome] = []
    for index, update in enumerate(updates):
        exemption = _exemption(update)
        if exemption is not None:
            outcomes.append(UpdateOutcome(update, exemption, None))
            continue

        forced = update.remote_sha != ZERO_SHA and not is_ancestor(
            update.remote_sha, update.local_sha
        )
        if forced and not allow_force_push:
            outcomes.append(UpdateOutcome(update, FORCE_PUSH_REFUSED, True))
            return _blocked(
                FORCE_PUSH_REFUSED,
                _block_message(
                    update.local_sha,
                    headline="force push",
                    reason=(
                        "the remote tip is not an ancestor of yours, so this rewrites "
                        "history the remote already has"
                    ),
                    fix="rebase onto the remote tip, or set [hook] allow_force_push = true",
                ),
                outcomes,
                updates[index + 1 :],
            )

        # The remote already has this commit, so the push publishes nothing new.
        if update.remote_sha != ZERO_SHA and is_ancestor(update.local_sha, update.remote_sha):
            outcomes.append(UpdateOutcome(update, ALREADY_ON_REMOTE, forced))
            continue

        if not has_attestation(update.local_sha):
            outcomes.append(UpdateOutcome(update, MISSING_ATTESTATION, forced))
            return _blocked(
                MISSING_ATTESTATION,
                _block_message(
                    update.local_sha,
                    headline="no green run recorded for this exact SHA",
                    reason="no valid attestation note is attached to this exact SHA",
                    fix=(
                        "invoke the skill (/agentic-preflight in Claude Code, "
                        "$agentic-preflight in Codex)"
                    ),
                ),
                outcomes,
                updates[index + 1 :],
            )
        outcomes.append(UpdateOutcome(update, ATTESTED, forced))

    return Decision(allowed=True, reason=_allowed_reason(outcomes), updates=outcomes)


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


def contains_hook_check(path: Path) -> bool:
    """Recognize composition as well as the exact hook installed by this tool."""
    return path.is_file() and HOOK_MARKER in path.read_bytes()


def install(repo_root: Path | str, *, force: bool = False) -> tuple[Path, bool]:
    """Write the pre-push hook. Returns (path, newly_written).

    Refuses to clobber a hook we did not write: someone else's pre-push hook is
    important to their workflow, and replacing it silently would be an
    unreviewed change to it. A hook that is byte for byte one this tool
    installed earlier is replaced with the current script.
    """
    path = gitx.hook_path(repo_root, "pre-push")
    hooks_dir = path.parent
    hooks_dir.mkdir(parents=True, exist_ok=True)

    if path.exists() and not force:
        # Read bytes, not text. The ownership marker is ASCII, while somebody
        # else's hook is arbitrary bytes that need not decode as UTF-8, and
        # failing to decode it must not turn "this hook is not ours, refuse to
        # touch it" into an unhandled error.
        existing = path.read_bytes()
        if HOOK_MARKER not in existing:
            raise FileExistsError(str(path))
        if existing not in PREVIOUS_HOOK_SCRIPTS:
            return path, False
        # Exactly a script this tool wrote earlier, so replacing it loses nothing
        # anyone added. A composed or edited hook is never rewritten.

    # LF explicitly: this is a shell script executed by Git's own ``sh``, and a
    # CRLF shebang line makes the interpreter unfindable.
    path.write_text(HOOK_SCRIPT, encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return path, True
