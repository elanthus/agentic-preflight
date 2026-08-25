"""An exclusive advisory lock on a file, portable across platforms.

The lock guards the read-modify-write window around a run document. Two
parallel ``Bash`` calls in a single agent turn are a real hazard, not a
theoretical one, so the semantics that matter are pinned here rather than left
to whichever primitive a platform happens to offer:

* **Exclusive.** One holder at a time, across processes.
* **Blocking.** Waiting is correct; failing a concurrent caller is not. The
  competing writer is another invocation of this tool doing legitimate work,
  and the second defence against a *logically* stale write is ``expect_seq``,
  not lock contention.
* **Released on any exit.** Including an exception, which is the path that
  leaves the on-disk state untouched.

``fcntl.flock`` provides all three directly. Windows has no ``fcntl``; the
equivalent is a mandatory byte-range lock through ``msvcrt``, which differs in
two ways that have to be handled rather than papered over. It locks a *range*
rather than a whole file, so a single conventional byte at offset zero stands in
for the file. And its blocking mode is not truly blocking: it retries for about
ten seconds and then raises, so it is driven here in a loop to restore the
indefinite wait that ``flock`` gives for free.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# ``sys.platform`` rather than ``os.name``: it is the form a type checker
# narrows on, so each platform's branch is checked against its own standard
# library instead of being reported as a missing attribute on the other's.
if sys.platform == "win32":  # pragma: no cover - platform-selected at import
    import msvcrt
else:  # pragma: no cover - platform-selected at import
    import fcntl

# The byte the Windows range lock is taken on. Every participant locks the same
# one, so the choice only has to be consistent, and offset zero always exists
# once the file has been created.
_LOCK_BYTE = 1


def _acquire(handle) -> None:
    if sys.platform == "win32":
        handle.seek(0)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, _LOCK_BYTE)
                return
            except OSError:
                # LK_LOCK gives up after roughly ten seconds. A contended lock
                # is normal here, so keep waiting rather than failing a caller
                # that is simply second in line.
                continue
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _release(handle) -> None:
    if sys.platform == "win32":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, _LOCK_BYTE)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive(path: Path | str) -> Iterator[None]:
    """Hold an exclusive lock on ``path`` for the duration of the block.

    The file is opened for append rather than write: truncating it would be a
    second, unsynchronised mutation of the very file being used to synchronise,
    and the Windows range lock needs a byte to exist to lock.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab+") as handle:
        if sys.platform == "win32" and os.fstat(handle.fileno()).st_size < _LOCK_BYTE:
            # Locking past end-of-file is permitted, but writing the byte keeps
            # the lock range backed by real content on every platform.
            handle.write(b"\0")
            handle.flush()
        _acquire(handle)
        try:
            yield
        finally:
            _release(handle)
