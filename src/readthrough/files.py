"""Small filesystem helpers shared by the build, the ledger, the study log, and the live server."""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import time
from collections.abc import Generator
from pathlib import Path

LOCK_TIMEOUT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.02


@contextlib.contextmanager
def locked(path: Path, *, timeout: float = LOCK_TIMEOUT_SECONDS) -> Generator[None]:
    """Hold an exclusive lock on ``path`` across threads and processes while a read-modify-write runs.

    The live server, the CLI (``readthrough study N --note`` from a review agent's terminal), and a second
    server thread can all append to the same study log or rewrite the same ledger at once; without this, two
    writers both read the same state and one's update is lost (or two log entries get the same id). The lock is
    a sidecar ``<name>.lock`` file, since the data file itself is replaced atomically on write; the OS releases
    it if the holder dies. Waiting longer than ``timeout`` raises ``TimeoutError`` rather than hanging a request.
    """
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                _try_lock(fd)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"another readthrough is still writing {path.name}; try again") from None
                time.sleep(LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def write_atomic(path: Path, text: str) -> None:
    """Write via a temp file and rename, so a reader (the live server) never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return 0.0


def stamp(path: Path) -> tuple[float, int]:
    """(mtime, size): what a watcher compares to notice a change. The size catches an append that lands within
    the same timestamp tick as the one before it, which coarse filesystem clocks make common."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return (0.0, 0)
    return (st.st_mtime, st.st_size)
