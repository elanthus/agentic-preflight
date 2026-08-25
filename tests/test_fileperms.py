"""Narrowing a copied local-environment file to its owner.

`copy_files` puts a `.env` inside the validation worktree, which makes that copy
a second on-disk home for a secret the user keeps in one place. These tests
cover the guarantee that makes the copy acceptable, at the level where it is
actually implemented: `copy_files` cannot reproduce the interesting cases,
because `shutil.copy2` does not carry a Windows ACL across and the destination
therefore always starts from whatever its parent grants.
"""

import stat
import subprocess
import sys

import pytest

from agentic_preflight import fileperms
from tests.conftest import access_entries, assert_owner_only, requires_windows

DOTENV_CONTENTS = "SECRET=hunter2\n"


@pytest.fixture
def secret_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(DOTENV_CONTENTS, encoding="utf-8")
    return path


def grant(path, principal: str) -> None:
    """Add an *explicit* access entry, the kind inheritance rules do not govern."""
    subprocess.run(
        ["icacls", str(path), "/grant", f"{principal}:(F)"],
        capture_output=True,
        check=True,
    )


def test_a_restricted_file_is_owner_only(secret_file):
    fileperms.restrict_to_owner(secret_file)

    assert_owner_only(secret_file)


def test_the_owner_can_still_read_the_file(secret_file):
    """Owner-only is the goal; owner-*less* would break every stage that reads it."""
    fileperms.restrict_to_owner(secret_file)

    assert secret_file.read_text(encoding="utf-8") == DOTENV_CONTENTS


@requires_windows
def test_explicit_access_entries_do_not_survive(secret_file):
    """The regression, and the reason `/inheritance:r` alone is not enough.

    A file whose DACL came from the creating process's default token rather
    than from its parent — which is how GitHub's Windows runners produce one,
    and how any machine does when the parent grants nothing inheritable —
    carries SYSTEM, Administrators, and OWNER RIGHTS as *explicit* entries.
    Removing inheritance does not touch those, and granting one principal
    replaces only that principal, so all three kept full access to the secret.
    """
    grant(secret_file, "NT AUTHORITY\\SYSTEM")
    grant(secret_file, "BUILTIN\\Administrators")
    assert len(access_entries(secret_file)) > 1, "the precondition did not take effect"

    fileperms.restrict_to_owner(secret_file)

    assert_owner_only(secret_file)


@requires_windows
def test_restriction_is_idempotent(secret_file):
    fileperms.restrict_to_owner(secret_file)
    fileperms.restrict_to_owner(secret_file)

    assert_owner_only(secret_file)


@pytest.mark.skipif(sys.platform == "win32", reason="mode bits carry no permissions on Windows")
def test_posix_uses_mode_bits(secret_file):
    fileperms.restrict_to_owner(secret_file)

    assert stat.S_IMODE(secret_file.stat().st_mode) == fileperms.OWNER_ONLY_MODE


def test_a_missing_file_is_reported_rather_than_ignored(tmp_path):
    """Silence would mean reporting a secret as protected when it was not touched."""
    with pytest.raises(fileperms.PermissionRestrictionError) as caught:
        fileperms.restrict_to_owner(tmp_path / "absent.env")

    assert "absent.env" in str(caught.value)


@requires_windows
def test_an_unusable_tool_is_reported_with_its_reason(secret_file, monkeypatch):
    """Windows-only by mechanism: the POSIX path reaches ``os.chmod``, not a tool.

    Its equivalent failure is covered by the missing-file case above, which
    exercises the same wrapping on both platforms."""
    monkeypatch.setattr(
        fileperms,
        "_run",
        lambda argv, path: (_ for _ in ()).throw(
            fileperms.PermissionRestrictionError(path, "icacls could not be run")
        ),
    )

    with pytest.raises(fileperms.PermissionRestrictionError) as caught:
        fileperms.restrict_to_owner(secret_file)

    assert "could not be run" in str(caught.value)
