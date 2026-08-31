"""The exclusive lock guarding the run-document read-modify-write window.

The property under test is cross-*process* exclusion, so these tests spawn real
processes. Two parallel ``Bash`` calls in one agent turn are the hazard this
lock exists for, and an in-process test of the same code would pass on a lock
that does not actually cross a process boundary.
"""

import errno
import subprocess
import sys
import textwrap

import pytest

from agentic_preflight import filelock

CHILD = textwrap.dedent(
    """
    import sys
    from agentic_preflight import filelock
    with filelock.exclusive(sys.argv[1]):
        sys.stdout.write("acquired")
    """
)


def child_waiting_for(lock_path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", CHILD, str(lock_path)],
        stdout=subprocess.PIPE,
        text=True,
    )


def test_a_second_process_waits_until_the_lock_is_released(tmp_path):
    lock = tmp_path / "run.lock"

    with filelock.exclusive(lock):
        child = child_waiting_for(lock)
        with pytest.raises(subprocess.TimeoutExpired):
            child.communicate(timeout=2)

    stdout, _ = child.communicate(timeout=60)
    assert stdout.strip() == "acquired"
    assert child.returncode == 0


def test_the_lock_is_released_when_the_body_raises(tmp_path):
    """The failure path is the one that leaves on-disk state untouched."""
    lock = tmp_path / "run.lock"

    with pytest.raises(RuntimeError), filelock.exclusive(lock):
        raise RuntimeError("transaction body failed")

    child = child_waiting_for(lock)
    stdout, _ = child.communicate(timeout=60)
    assert stdout.strip() == "acquired"


def test_the_lock_can_be_taken_again_by_the_same_process(tmp_path):
    lock = tmp_path / "run.lock"

    with filelock.exclusive(lock):
        pass
    with filelock.exclusive(lock):
        pass


def test_a_nonblocking_probe_reports_contention_and_later_success(tmp_path):
    lock = tmp_path / "run.lock"
    probe = textwrap.dedent(
        """
        import sys
        from agentic_preflight import filelock
        with filelock.try_exclusive(sys.argv[1]) as acquired:
            sys.stdout.write(str(acquired))
        """
    )

    with filelock.exclusive(lock):
        result = subprocess.run(
            [sys.executable, "-c", probe, str(lock)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout == "False"

    with filelock.try_exclusive(lock) as acquired:
        assert acquired is True


@pytest.mark.skipif(sys.platform != "win32", reason="the retry loop is the Windows path")
def test_a_lock_error_that_is_not_contention_is_raised_rather_than_retried(tmp_path, monkeypatch):
    """Contention resolves by waiting; a bad descriptor never will.

    Retrying everything meant a permanent error became an unkillable loop at
    full CPU instead of an exception naming the problem.
    """
    attempts: list[int] = []

    def always_broken(fileno, mode, nbytes):
        attempts.append(fileno)
        raise OSError(errno.EBADF, "bad file descriptor")

    monkeypatch.setattr(filelock.msvcrt, "locking", always_broken)

    with (
        pytest.raises(OSError, match="bad file descriptor"),
        filelock.exclusive(tmp_path / "run.lock"),
    ):
        pass

    assert len(attempts) == 1


def test_the_lock_file_is_created_with_its_parent_directory(tmp_path):
    lock = tmp_path / "runs" / "abc123" / ".lock"

    with filelock.exclusive(lock):
        assert lock.exists()


def test_locking_does_not_truncate_an_existing_lock_file(tmp_path):
    """The lock file must not be a second, unsynchronised mutation of itself."""
    lock = tmp_path / "run.lock"
    lock.write_bytes(b"sentinel")

    with filelock.exclusive(lock):
        pass

    assert lock.read_bytes().startswith(b"sentinel")
