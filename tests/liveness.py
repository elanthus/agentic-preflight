"""Tell that processes are gone without asking the process table.

``os.kill(pid, 0)`` and ``os.killpg(pgid, 0)`` succeed for a process that has
exited and that its parent has not reaped yet. A process whose parent was killed is
reaped by whatever adopted it, and when that happens is not the test's to decide.
A file descriptor is closed when its process exits, reaped or not.
"""

from __future__ import annotations

import os
import time
from pathlib import Path


class HeldFifo:
    """A FIFO that the processes under test hold open for writing.

    Have each process open ``path`` for writing, or inherit it open, before the
    test asks ``closed_by_all``: a FIFO that nothing has opened yet reads the same
    as one that everything has closed.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        os.mkfifo(path)
        # Non-blocking, so that opening does not wait for a writer and reading
        # does not wait for one to write.
        self._reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK)

    def closed_by_all(self, seconds: float = 10.0) -> bool:
        """Whether every process that held the FIFO has exited within ``seconds``."""
        deadline = time.monotonic() + seconds
        while True:
            try:
                if os.read(self._reader, 1) == b"":
                    return True
            except BlockingIOError:
                # Open for writing somewhere, with nothing written.
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def close(self) -> None:
        os.close(self._reader)
