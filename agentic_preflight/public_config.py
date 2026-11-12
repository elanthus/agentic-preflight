"""The configuration a published attestation carries, without user-level settings.

A run executes with its full configuration snapshot, including sections from
the user configuration file (``~/.config/agentic-preflight/config.toml``). That
file describes one machine and can hold local paths or inline credentials, so
the note that ``push`` publishes leaves its sections out. The repository file is
already public in the repository.

Fingerprints and digests that the note carries are computed over the same
published view, so a verifier that recomputes them from the note still agrees.
Verification takes its policy from the protected base, not from the note.
"""

from __future__ import annotations

import re
from typing import Any

from .config import Config, snapshot_config
from .digests import json_digest
from .models import RunDoc, Stage

#: User-level sections that are published anyway. Verification rebuilds the
#: review diff from ``[diff] exclude``, and the section holds only path patterns.
PUBLISHED_USER_SECTIONS = frozenset({"diff"})

#: ``NAME=value`` shell assignments, as a command prefix, after ``env``, or after
#: an operator. The value may combine quoted and unquoted parts.
_ASSIGNMENT = re.compile(
    r"""(?:^|(?<=[\s;&|(]))([A-Za-z_][A-Za-z0-9_]*)="""
    r"""(?:"(?:[^"\\]|\\.)*"?|'[^']*'?|\\.|[^\s;&|()'"\\])*"""
)
REDACTED_VALUE = "[redacted]"


def withheld_sections(run: RunDoc) -> set[str]:
    """Snapshot sections that came from user configuration and are not published."""
    return set(run.user_config_sections) - PUBLISHED_USER_SECTIONS


def published_snapshot(run: RunDoc) -> dict[str, Any]:
    """The run's configuration snapshot without its user-level sections.

    A run with no user-level sections publishes its snapshot unchanged, with the
    same digest as before.
    """
    withheld = withheld_sections(run)
    return {key: value for key, value in run.config_snapshot.items() if key not in withheld}


def withheld_snapshot(run: RunDoc) -> dict[str, Any]:
    """The user-level sections that :func:`published_snapshot` leaves out."""
    withheld = withheld_sections(run)
    return {key: value for key, value in run.config_snapshot.items() if key in withheld}


def published_config_digest(run: RunDoc) -> str:
    """The digest a note binds to :func:`published_snapshot`."""
    return json_digest(published_snapshot(run))


def redact_assignments(command: str) -> str:
    """Replace the value of every ``NAME=value`` shell assignment in ``command``."""
    return _ASSIGNMENT.sub(lambda match: f"{match.group(1)}={REDACTED_VALUE}", command)


def _command_section(stage: Stage) -> str:
    return "review" if stage is Stage.REVIEW else "commands"


def published_command(run: RunDoc, stage: Stage, command: str | None) -> str | None:
    """The stage command as a note records it.

    A command configured in the repository file is published as written, so a
    verifier can compare it with the protected policy. A command from a
    user-level ``[review]`` or ``[commands]`` section is published with its
    environment assignments redacted.
    """
    if command is None or _command_section(stage) not in run.user_config_sections:
        return command
    return redact_assignments(command)


def local_policy(run: RunDoc) -> Config:
    """The run's own policy, with its review command in published form.

    Building a note checks it against this policy. The note carries the
    published review command, so the policy compares the same form.
    """
    cfg = snapshot_config(run.config_snapshot)
    command = published_command(run, Stage.REVIEW, cfg.review.command)
    if command == cfg.review.command:
        return cfg
    return cfg.model_copy(update={"review": cfg.review.model_copy(update={"command": command})})
