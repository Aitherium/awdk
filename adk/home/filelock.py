"""One OS lock per home state file, that says who holds it.

``<file>.lock`` beside the state; byte 0 is locked with ``fcntl.flock`` /
``msvcrt.locking``, so the kernel drops it when the holder exits or crashes (a lock is
never "stale" and nothing is ever taken over). The holder writes a label (``adk home
updates pid 4242``) after byte 0, outside the locked byte, so a waiter that times out
can name who has it instead of guessing.

* :func:`run_lock` -- blocking, for CLI runs.
* :func:`arun_lock` -- for the event loop (``adk home serve``): it waits with
  ``asyncio.sleep``, never by blocking the loop, and serialises the process's own
  coroutines first so two tool calls in one serve never wait on each other's OS lock.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import AsyncIterator, Dict, Iterator

logger = logging.getLogger("adk.home.filelock")

#: Where the holder's label starts (after the one locked byte).
LABEL_AT = 16
LABEL_MAX = 120
_LOCAL: Dict[str, asyncio.Lock] = {}


def lock_path(path: Path) -> Path:
    p = Path(path)
    return p.with_name(p.name + ".lock")


def try_lock(fd: int) -> bool:
    """Take the exclusive lock on ``fd`` without blocking. True when taken."""
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def unlock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError as exc:  # closing the fd releases it anyway
        logger.debug("unlock: %s", exc)


def _write_label(fd: int, holder: str) -> None:
    label = f"{holder} pid {os.getpid()}"[:LABEL_MAX].encode("utf-8", "replace")
    try:
        os.lseek(fd, LABEL_AT, os.SEEK_SET)
        os.write(fd, label)
        os.ftruncate(fd, LABEL_AT + len(label))
    except OSError as exc:  # the lock still holds; only the label is missing
        logger.debug("lock label not written: %s", exc)


def read_holder(path: Path) -> str:
    """Who holds the lock on ``path`` (its label), or ``""`` when unknown."""
    try:
        with open(lock_path(path), "rb") as fh:
            fh.seek(LABEL_AT)
            raw = fh.read(LABEL_MAX)
    except OSError:
        return ""
    text = raw.decode("utf-8", "replace")
    return "".join(c for c in text if c.isprintable()).strip()


def _busy(path: Path, fallback: str) -> RuntimeError:
    who = read_holder(path)
    if not who:
        return RuntimeError(f"{fallback} holds {lock_path(path)}; try again when it finishes")
    return RuntimeError(f"another run holds {lock_path(path)}: {who}; try again when it "
                        "finishes")


def _open(path: Path) -> int:
    lock = lock_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    return os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)


@contextmanager
def run_lock(path: Path, *, holder: str, timeout: float = 10.0,
             fallback: str = "another run") -> Iterator[None]:
    """Hold the lock on ``path`` for the block, labelled ``holder``.

    Raises:
        RuntimeError: someone held it for ``timeout`` seconds (the message names them).
    """
    fd = _open(path)
    deadline = time.monotonic() + timeout
    try:
        while not try_lock(fd):
            if time.monotonic() > deadline:
                raise _busy(path, fallback)
            time.sleep(0.05)
        _write_label(fd, holder)
        try:
            yield
        finally:
            unlock(fd)
    finally:
        os.close(fd)


@asynccontextmanager
async def arun_lock(path: Path, *, holder: str, timeout: float = 10.0,
                    fallback: str = "another run") -> AsyncIterator[None]:
    """:func:`run_lock` for a coroutine: never blocks the event loop."""
    key = str(lock_path(path).resolve())
    local = _LOCAL.setdefault(key, asyncio.Lock())
    try:
        await asyncio.wait_for(local.acquire(), timeout)
    except asyncio.TimeoutError as exc:
        raise _busy(path, fallback) from exc
    try:
        fd = _open(path)
        try:
            deadline = time.monotonic() + timeout
            while not try_lock(fd):
                if time.monotonic() > deadline:
                    raise _busy(path, fallback)
                await asyncio.sleep(0.05)
            _write_label(fd, holder)
            try:
                yield
            finally:
                unlock(fd)
        finally:
            os.close(fd)
    finally:
        local.release()


__all__ = ["run_lock", "arun_lock", "read_holder", "lock_path", "try_lock", "unlock"]
