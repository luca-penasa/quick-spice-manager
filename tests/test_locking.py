"""
Tests for the generic cross-process bounded-semaphore helper (locking.py).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from quick_spice_manager.locking import bounded_slot


def test_bounded_slot_bounds_concurrent_holders(tmp_path: Path):
    """No more than n_slots holders can be "inside" at once; extras wait."""
    n_slots = 3
    release = threading.Event()
    concurrent_count = 0
    max_concurrent_seen = 0
    count_lock = threading.Lock()

    def hold_slot():
        nonlocal concurrent_count, max_concurrent_seen
        with bounded_slot(tmp_path, n_slots, timeout=5):
            with count_lock:
                concurrent_count += 1
                max_concurrent_seen = max(max_concurrent_seen, concurrent_count)
            release.wait(timeout=5)
            with count_lock:
                concurrent_count -= 1

    threads = [threading.Thread(target=hold_slot) for _ in range(n_slots + 2)]
    for t in threads:
        t.start()

    time.sleep(0.3)  # let every thread attempt acquisition
    assert concurrent_count == n_slots  # extras are waiting, not "inside"

    release.set()
    for t in threads:
        t.join(timeout=5)

    assert all(not t.is_alive() for t in threads)
    assert max_concurrent_seen == n_slots


def test_bounded_slot_releases_on_exception(tmp_path: Path):
    """A slot must be released even when the body raises, otherwise repeated
    failures would permanently exhaust every slot."""
    n_slots = 2
    for _ in range(n_slots + 3):
        with pytest.raises(RuntimeError, match="boom"), bounded_slot(
            tmp_path, n_slots, timeout=1,
        ):
            raise RuntimeError("boom")


def test_bounded_slot_raises_timeout_when_exhausted(tmp_path: Path):
    """When every slot is held and none frees before the timeout, a
    TimeoutError is raised rather than blocking forever."""
    n_slots = 1
    held = bounded_slot(tmp_path, n_slots, timeout=5)
    held.__enter__()
    try:
        with (
            pytest.raises(TimeoutError, match="Timed out waiting"),
            bounded_slot(tmp_path, n_slots, timeout=0.3),
        ):
            pass
    finally:
        held.__exit__(None, None, None)


def test_bounded_slot_independent_per_directory(tmp_path: Path):
    """Two different lock_dir paths have entirely independent slot pools."""
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    n_slots = 1

    held_a = bounded_slot(dir_a, n_slots, timeout=5)
    held_a.__enter__()
    try:
        # dir_b's single slot is free even though dir_a's is fully held.
        with bounded_slot(dir_b, n_slots, timeout=1):
            pass
    finally:
        held_a.__exit__(None, None, None)
