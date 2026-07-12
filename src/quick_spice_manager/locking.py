"""
Cross-process bounded semaphore built on file locks.

Used to cap how many OS processes (or threads, including across processes)
may concurrently be inside a given critical section -- e.g. connections to
the ESA FTP server, or local kernel-file reads during furnsh() -- without
requiring any persistent coordinator process. Implemented as round-robin
polling over a fixed number of lock files, since plain file locks have no
"notify" mechanism to wake a waiter the instant a slot frees.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

import filelock

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@contextmanager
def bounded_slot(
    lock_dir: Path,
    n_slots: int,
    *,
    timeout: float,
    poll_interval: float = 0.1,
) -> Iterator[None]:
    """Block until one of *n_slots* lock files under *lock_dir* is free, then
    hold it for the duration of the ``with`` block.

    Raises
    ------
    TimeoutError
        If no slot becomes free within *timeout* seconds.
    """
    lock_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        for i in range(n_slots):
            lock = filelock.FileLock(str(lock_dir / f"slot-{i}.lock"))
            try:
                lock.acquire(timeout=0)
            except filelock.Timeout:
                continue
            try:
                yield
            finally:
                lock.release()
            return
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"Timed out waiting for a free slot in {lock_dir} "
                f"(max {n_slots} concurrent holders)",
            )
        time.sleep(poll_interval)
