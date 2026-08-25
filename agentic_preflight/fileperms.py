"""Restricting a copied local-environment file to its owner.

``copy_files`` puts things like ``.env`` inside the validation worktree, so the
copy is a second on-disk location for a secret the user has only ever kept in
one. Narrowing it to the owner is the guarantee that makes that acceptable.

``os.chmod(path, 0o600)`` delivers it on POSIX. On Windows it does almost
nothing: ``chmod`` there only toggles the read-only attribute, and permissions
live in an ACL that the call never touches. A copied ``.env`` would inherit the
containing directory's ACEs and be readable by every principal that already had
access — silently, because the call still returns successfully.

The Windows equivalent is therefore built explicitly: reset the file's access
list, drop what it inherits, and grant the calling user alone. ``icacls`` ships
with Windows and needs no elevation to rewrite the access list of a file the
caller owns, which keeps this free of a new dependency in a package that has
two. See :func:`_restrict_via_acl` for why all three steps are load-bearing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

OWNER_ONLY_MODE = 0o600

_ACL_TIMEOUT_SECONDS = 30
_cached_sid: str | None = None


class PermissionRestrictionError(Exception):
    """A file could not be narrowed to its owner.

    Raised rather than warned about: the caller copied a secret on the promise
    that it would be unreadable by anyone else, and a promise that quietly
    failed is worse than one that was never made.
    """

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"cannot restrict {str(path)!r} to its owner: {reason}")
        self.path = path
        self.reason = reason


def _run(argv: list[str], path: Path) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_ACL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PermissionRestrictionError(path, f"{argv[0]} could not be run ({exc})") from exc


def current_user_sid(path: Path) -> str:
    """The calling user's SID, in the ``*S-1-5-...`` form ``icacls`` accepts.

    The SID rather than the account name: it is stable, unambiguous between a
    local and a domain account of the same name, and unaffected by the display
    language of the built-in groups.
    """
    global _cached_sid
    if _cached_sid is not None:
        return _cached_sid

    result = _run(["whoami", "/user", "/fo", "csv", "/nh"], path)
    if result.returncode != 0:
        raise PermissionRestrictionError(
            path, f"could not resolve the current user: {result.stderr.strip()}"
        )

    fields = [field.strip().strip('"') for field in result.stdout.strip().split(",")]
    sid = fields[-1] if fields else ""
    if not sid.startswith("S-1-"):
        raise PermissionRestrictionError(
            path, f"could not parse a user SID from {result.stdout.strip()!r}"
        )

    _cached_sid = sid
    return sid


def _restrict_via_mode(path: Path) -> None:
    try:
        os.chmod(path, OWNER_ONLY_MODE)
    except OSError as exc:
        raise PermissionRestrictionError(path, str(exc)) from exc


def _restrict_via_acl(path: Path) -> None:
    """Replace the file's DACL with a single entry for the calling user.

    Two passes, because neither ``icacls`` option does this alone and the gap
    between them is exactly where a secret leaks:

    ``/reset`` discards *explicit* entries, leaving only what the parent
    directory grants by inheritance. This is the step that is easy to omit and
    hard to notice missing: a file whose DACL came from the creating process's
    default rather than from inheritance carries SYSTEM, Administrators, and
    OWNER RIGHTS as explicit entries, which ``/inheritance:r`` will not touch
    and ``/grant:r`` will not replace, because it only replaces the principal
    it names.

    ``/inheritance:r`` then drops the inherited entries that ``/reset`` left,
    and ``/grant:r`` adds the one entry that should remain.
    """
    sid = current_user_sid(path)

    reset = _run(["icacls", str(path), "/reset"], path)
    if reset.returncode != 0:
        raise PermissionRestrictionError(
            path,
            f"icacls /reset failed ({reset.returncode}): {(reset.stderr or reset.stdout).strip()}",
        )

    granted = _run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:F"], path)
    if granted.returncode != 0:
        raise PermissionRestrictionError(
            path,
            f"icacls failed ({granted.returncode}): {(granted.stderr or granted.stdout).strip()}",
        )


def restrict_to_owner(path: Path | str) -> None:
    """Make ``path`` readable and writable by its owner and by nobody else."""
    path = Path(path)
    if sys.platform == "win32":
        _restrict_via_acl(path)
    else:
        _restrict_via_mode(path)
