"""
Unit tests for SpiceManager standalone kernel loading and context manager.

All spiceypy calls and FTP downloads are mocked — no real SPICE kernels or
network access required.
"""

from __future__ import annotations

import tempfile
import textwrap
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, call, patch

import filelock
import pytest

from quick_spice_manager import spice_manager as spice_manager_module
from quick_spice_manager.dirs import get_cache_lock_path
from quick_spice_manager.spice_manager import SpiceManager, _KERNEL_MAX_LENGTH


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_MK = textwrap.dedent("""\
    KPL/MK

    \\begindata
    PATH_VALUES = ( '/original/kernels' )
    PATH_SYMBOLS = ( 'KERNELS' )
    KERNELS_TO_LOAD = (
        '$KERNELS/fk/juice_v01.tf'
    )
    \\begintext
""")


def _make_local_mk(tmp_path: Path, content: str = _MINIMAL_MK) -> Path:
    """Write a minimal metakernel to a temp file and return its path."""
    mk = tmp_path / "test_kernels.tm"
    mk.write_text(content, encoding="utf-8")
    return mk


def _make_sm(tmp_path: Path, mk: Path | None = None, **kwargs) -> SpiceManager:
    """Create a SpiceManager pointing at a local metakernel with no downloads."""
    if mk is None:
        mk = _make_local_mk(tmp_path)
    return SpiceManager(
        mk=str(mk),
        kernels_dir=str(tmp_path),
        download_kernels=False,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# _localize_metakernel — PATH_VALUES rewriting
# ---------------------------------------------------------------------------


def test_localize_metakernel_rewrites_path(tmp_path):
    mk = _make_local_mk(tmp_path)
    sm = _make_sm(tmp_path, mk=mk)

    tmp_mk = sm._localize_metakernel(mk)
    try:
        localized_content = tmp_mk.read_text(encoding="utf-8")
        assert str(tmp_path) in localized_content
        assert "/original/kernels" not in localized_content
        assert tmp_mk.suffix == ".tm"
        assert tmp_mk != mk  # must be a different (temp) file
    finally:
        tmp_mk.unlink(missing_ok=True)


def test_localize_metakernel_path_too_long(tmp_path):
    mk = _make_local_mk(tmp_path)
    long_dir = tmp_path / ("x" * (_KERNEL_MAX_LENGTH + 1))
    sm = SpiceManager(
        mk=str(mk),
        kernels_dir=str(long_dir),
        download_kernels=False,
    )
    with pytest.raises(ValueError, match=str(_KERNEL_MAX_LENGTH)):
        sm._localize_metakernel(mk)


def test_localize_metakernel_path_at_limit_is_accepted(tmp_path):
    mk = _make_local_mk(tmp_path)
    # Build a path string of exactly KERNEL_MAX_LENGTH characters.
    # Use a fake absolute path so length is controlled.
    exact_dir = "/" + "a" * (_KERNEL_MAX_LENGTH - 1)  # len == _KERNEL_MAX_LENGTH
    assert len(exact_dir) == _KERNEL_MAX_LENGTH
    sm = SpiceManager(
        mk=str(mk),
        kernels_dir=exact_dir,
        download_kernels=False,
    )
    tmp_mk = sm._localize_metakernel(mk)
    try:
        assert tmp_mk.exists()
    finally:
        tmp_mk.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# is_active / is_dirty / clean_pool
# ---------------------------------------------------------------------------


def test_is_dirty_none_before_load(tmp_path):
    sm = _make_sm(tmp_path)
    assert sm.is_dirty is None


def test_is_active_false_before_load(tmp_path):
    sm = _make_sm(tmp_path)
    assert sm.is_active is False


def _sm_loaded(tmp_path, pool_after_furnsh):
    """Return a SpiceManager whose load_kernels() has been called with a mocked pool."""
    mk = _make_local_mk(tmp_path)
    sm = _make_sm(tmp_path, mk=mk)
    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        # furnsh() is a no-op; pool after loading contains one kernel
        mock_spice.ktotal.return_value = len(pool_after_furnsh)
        mock_spice.kdata.side_effect = [
            (f, "SPK", "file", i) for i, f in enumerate(pool_after_furnsh)
        ]
        sm.load_kernels()
    return sm, frozenset(pool_after_furnsh)


def test_is_active_true_after_load(tmp_path):
    sm, _ = _sm_loaded(tmp_path, ["/kernels/juice_spk.bsp"])
    assert sm.is_active is True


def test_is_active_false_when_superseded(tmp_path):
    """When a second manager loads kernels, the first one loses active status."""
    k1 = "/kernels/juice_spk.bsp"
    k2 = "/kernels/ros_spk.bsp"
    sm_juice, _ = _sm_loaded(tmp_path, [k1])
    assert sm_juice.is_active is True

    sm_rosetta, _ = _sm_loaded(tmp_path, [k2])
    assert sm_rosetta.is_active is True
    assert sm_juice.is_active is False  # superseded


def test_is_dirty_none_when_superseded(tmp_path):
    """is_dirty returns None for a superseded manager, not True."""
    sm_juice, _ = _sm_loaded(tmp_path, ["/kernels/juice_spk.bsp"])
    _sm_loaded(tmp_path, ["/kernels/ros_spk.bsp"])  # supersedes juice
    assert sm_juice.is_dirty is None


def test_is_dirty_false_when_pool_unchanged(tmp_path):
    k1 = "/kernels/juice_spk.bsp"
    sm, expected = _sm_loaded(tmp_path, [k1])
    assert sm._expected_kernels == expected

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        mock_spice.ktotal.return_value = 1
        mock_spice.kdata.return_value = (k1, "SPK", "file", 0)
        assert sm.is_dirty is False


def test_is_dirty_true_when_extra_kernel_added(tmp_path):
    k1 = "/kernels/juice_spk.bsp"
    sm, _ = _sm_loaded(tmp_path, [k1])

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        # pool now has an extra kernel
        mock_spice.ktotal.return_value = 2
        mock_spice.kdata.side_effect = [
            (k1, "SPK", "file", 0),
            ("/extra/custom.bc", "CK", "file", 1),
        ]
        assert sm.is_dirty is True


def test_is_dirty_true_when_kernel_removed(tmp_path):
    k1, k2 = "/kernels/juice_spk.bsp", "/kernels/juice_fk.tf"
    sm, _ = _sm_loaded(tmp_path, [k1, k2])

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        # k2 was removed from pool
        mock_spice.ktotal.return_value = 1
        mock_spice.kdata.return_value = (k1, "SPK", "file", 0)
        assert sm.is_dirty is True


def test_clean_pool_raises_before_load(tmp_path):
    sm = _make_sm(tmp_path)
    with pytest.raises(RuntimeError, match="load_kernels()"):
        sm.clean_pool()


def test_clean_pool_raises_when_superseded(tmp_path):
    sm_juice, _ = _sm_loaded(tmp_path, ["/kernels/juice_spk.bsp"])
    _sm_loaded(tmp_path, ["/kernels/ros_spk.bsp"])  # supersedes juice
    with pytest.raises(RuntimeError, match="no longer the active pool owner"):
        sm_juice.clean_pool()


def test_clean_pool_unloads_extra_kernels(tmp_path):
    k1 = "/kernels/juice_spk.bsp"
    extra = "/extra/custom.bc"
    sm, _ = _sm_loaded(tmp_path, [k1])

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        # Pool has grown: k1 + extra
        mock_spice.ktotal.return_value = 2
        mock_spice.kdata.side_effect = [
            (k1, "SPK", "file", 0),
            (extra, "CK", "file", 1),
        ]
        result = sm.clean_pool()

    assert result["unloaded"] == [extra]
    assert result["restored"] == []
    mock_spice.unload.assert_called_once_with(extra)
    mock_spice.furnsh.assert_not_called()


def test_clean_pool_restores_missing_kernel(tmp_path):
    k1 = "/kernels/juice_spk.bsp"
    k2 = tmp_path / "juice_fk.tf"  # must exist for restoration
    k2.write_text("placeholder")
    sm, _ = _sm_loaded(tmp_path, [k1, str(k2)])

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        # Only k1 left in pool; k2 was removed
        mock_spice.ktotal.return_value = 1
        mock_spice.kdata.return_value = (k1, "SPK", "file", 0)
        result = sm.clean_pool()

    assert result["unloaded"] == []
    assert str(k2) in result["restored"]
    mock_spice.furnsh.assert_called_once_with(str(k2))


def test_clean_pool_skips_missing_file_on_disk(tmp_path):
    k1 = "/kernels/juice_spk.bsp"
    phantom = "/gone/juice_ck.bc"  # not on disk
    sm, _ = _sm_loaded(tmp_path, [k1, phantom])

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        # phantom was removed from pool
        mock_spice.ktotal.return_value = 1
        mock_spice.kdata.return_value = (k1, "SPK", "file", 0)
        result = sm.clean_pool()

    assert result["restored"] == []  # file doesn't exist, silently skipped
    mock_spice.furnsh.assert_not_called()


def test_clean_pool_no_op_when_pool_clean(tmp_path):
    k1 = "/kernels/juice_spk.bsp"
    sm, _ = _sm_loaded(tmp_path, [k1])

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        mock_spice.ktotal.return_value = 1
        mock_spice.kdata.return_value = (k1, "SPK", "file", 0)
        result = sm.clean_pool()

    assert result == {"unloaded": [], "restored": []}
    mock_spice.unload.assert_not_called()
    mock_spice.furnsh.assert_not_called()


def test_expected_kernels_cleared_on_unload(tmp_path):
    k1 = "/kernels/juice_spk.bsp"
    sm, _ = _sm_loaded(tmp_path, [k1])
    assert sm._expected_kernels is not None

    with patch("quick_spice_manager.spice_manager.spiceypy"):
        sm.unload_kernels()

    assert sm._expected_kernels is None
    assert sm.is_dirty is None


# ---------------------------------------------------------------------------
# add_kernel
# ---------------------------------------------------------------------------


def test_add_kernel_furnishes_and_marks_clean(tmp_path):
    k1 = "/kernels/juice_spk.bsp"
    extra = tmp_path / "custom.bc"
    extra.write_text("placeholder")
    sm, _ = _sm_loaded(tmp_path, [k1])

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        result = sm.add_kernel(extra)

    assert result == extra.resolve()
    mock_spice.furnsh.assert_called_once_with(str(extra.resolve()))
    # Expected set updated → not dirty
    assert str(extra.resolve()) in sm._expected_kernels


def test_add_kernel_pool_not_dirty_after_add(tmp_path):
    k1 = "/kernels/juice_spk.bsp"
    extra = tmp_path / "custom.bc"
    extra.write_text("placeholder")
    sm, _ = _sm_loaded(tmp_path, [k1])

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        sm.add_kernel(extra)
        # Pool now reports k1 + extra
        mock_spice.ktotal.return_value = 2
        mock_spice.kdata.side_effect = [
            (k1, "SPK", "file", 0),
            (str(extra.resolve()), "CK", "file", 1),
        ]
        assert sm.is_dirty is False


def test_add_kernel_raises_if_file_missing(tmp_path):
    sm, _ = _sm_loaded(tmp_path, ["/kernels/juice_spk.bsp"])
    with pytest.raises(FileNotFoundError, match="not found"):
        sm.add_kernel(tmp_path / "does_not_exist.bc")


def test_add_kernel_raises_if_not_active(tmp_path):
    k1 = "/kernels/juice_spk.bsp"
    extra = tmp_path / "custom.bc"
    extra.write_text("placeholder")
    sm_juice, _ = _sm_loaded(tmp_path, [k1])
    _sm_loaded(tmp_path, ["/kernels/ros_spk.bsp"])  # supersedes juice
    with pytest.raises(RuntimeError, match="not the active pool owner"):
        sm_juice.add_kernel(extra)


def test_add_kernel_collection_furnishes_all(tmp_path):
    k1 = "/kernels/juice_spk.bsp"
    extra1 = tmp_path / "custom1.bc"
    extra2 = tmp_path / "custom2.bc"
    extra1.write_text("placeholder")
    extra2.write_text("placeholder")
    sm, _ = _sm_loaded(tmp_path, [k1])

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        result = sm.add_kernel([extra1, extra2])

    assert isinstance(result, list)
    assert len(result) == 2
    assert mock_spice.furnsh.call_count == 2
    assert str(extra1.resolve()) in sm._expected_kernels
    assert str(extra2.resolve()) in sm._expected_kernels


def test_add_kernel_collection_returns_single_path_for_scalar(tmp_path):
    extra = tmp_path / "custom.bc"
    extra.write_text("placeholder")
    sm, _ = _sm_loaded(tmp_path, ["/kernels/juice_spk.bsp"])

    with patch("quick_spice_manager.spice_manager.spiceypy"):
        result = sm.add_kernel(extra)

    assert isinstance(result, Path)


def test_add_kernel_collection_atomic_on_missing(tmp_path):
    """If any file is missing, none should be furnished."""
    extra1 = tmp_path / "custom1.bc"
    extra1.write_text("placeholder")
    missing = tmp_path / "does_not_exist.bc"
    sm, _ = _sm_loaded(tmp_path, ["/kernels/juice_spk.bsp"])

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        with pytest.raises(FileNotFoundError):
            sm.add_kernel([extra1, missing])
        mock_spice.furnsh.assert_not_called()


def test_add_kernel_accepts_generator(tmp_path):
    kernels = [tmp_path / f"k{i}.bc" for i in range(3)]
    for k in kernels:
        k.write_text("placeholder")
    sm, _ = _sm_loaded(tmp_path, ["/kernels/juice_spk.bsp"])

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        result = sm.add_kernel(k for k in kernels)  # generator

    assert len(result) == 3
    assert mock_spice.furnsh.call_count == 3


def test_add_kernel_before_load_does_not_raise(tmp_path):
    """add_kernel works even before load_kernels (no expected set to track)."""
    extra = tmp_path / "custom.bc"
    extra.write_text("placeholder")
    sm = _make_sm(tmp_path)
    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        sm.add_kernel(extra)
    mock_spice.furnsh.assert_called_once_with(str(extra.resolve()))
    assert sm._expected_kernels is None


# ---------------------------------------------------------------------------
# snapshot_pool
# ---------------------------------------------------------------------------


def test_snapshot_pool_writes_valid_metakernel(tmp_path):
    existing = tmp_path / "juice_fk.tf"
    existing.write_text("placeholder")
    sm = _make_sm(tmp_path)
    out = tmp_path / "snapshot.tm"

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        mock_spice.ktotal.return_value = 1
        mock_spice.kdata.return_value = (str(existing), "TEXT", "file", 1)
        result = sm.snapshot_pool(out)

    assert result == out
    content = out.read_text(encoding="utf-8")
    assert "KPL/MK" in content
    assert "KERNELS_TO_LOAD" in content
    assert "\\begindata" in content
    assert "\\begintext" in content
    # With a single kernel the common prefix is its parent dir
    assert "PATH_VALUES" in content
    assert "PATH_SYMBOLS" in content
    assert "$KERNELS" in content
    # Relative portion of the filename must appear
    assert existing.name in content


def test_snapshot_pool_uses_common_prefix(tmp_path):
    """All kernels under the same root are factored into a single PATH_VALUES."""
    root = tmp_path / "kernels" / "juice"
    k1 = root / "ck" / "juice_sc.bc"
    k2 = root / "spk" / "juice_orb.bsp"
    k3 = root / "fk" / "juice_v45.tf"
    for k in (k1, k2, k3):
        k.parent.mkdir(parents=True, exist_ok=True)
        k.write_text("placeholder")

    sm = _make_sm(tmp_path)
    out = tmp_path / "snapshot.tm"

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        mock_spice.ktotal.return_value = 3
        mock_spice.kdata.side_effect = [
            (str(k1), "CK", "file", 0),
            (str(k2), "SPK", "file", 1),
            (str(k3), "TEXT", "file", 2),
        ]
        sm.snapshot_pool(out)

    content = out.read_text(encoding="utf-8")
    # Common prefix must be the shared root directory
    assert str(root) in content
    assert "PATH_VALUES" in content
    assert "$KERNELS/ck/juice_sc.bc" in content
    assert "$KERNELS/spk/juice_orb.bsp" in content
    assert "$KERNELS/fk/juice_v45.tf" in content
    # Full absolute paths must NOT appear in KERNELS_TO_LOAD
    assert str(k1) not in content.split("KERNELS_TO_LOAD")[1]


def test_snapshot_pool_skips_meta_entries(tmp_path):
    sm = _make_sm(tmp_path)
    out = tmp_path / "snapshot.tm"

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        mock_spice.ktotal.return_value = 1
        mock_spice.kdata.return_value = ("/tmp/temp_metakernel.tm", "META", "file", 1)
        sm.snapshot_pool(out)

    content = out.read_text(encoding="utf-8")
    assert "KERNELS_TO_LOAD = (\n)" in content


def test_snapshot_pool_raises_on_path_too_long(tmp_path):
    sm = _make_sm(tmp_path)
    out = tmp_path / "snapshot.tm"
    long_path = "/" + "k" * _KERNEL_MAX_LENGTH  # len == _KERNEL_MAX_LENGTH + 1

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        mock_spice.ktotal.return_value = 1
        mock_spice.kdata.return_value = (long_path, "SPK", "file", 1)
        with pytest.raises(ValueError, match=str(_KERNEL_MAX_LENGTH)):
            sm.snapshot_pool(out)


def test_snapshot_pool_creates_parent_dirs(tmp_path):
    sm = _make_sm(tmp_path)
    out = tmp_path / "subdir" / "nested" / "snapshot.tm"

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        mock_spice.ktotal.return_value = 0
        sm.snapshot_pool(out)

    assert out.exists()


def test_snapshot_pool_is_reproducible_via_localize(tmp_path):
    """A snapshot file can be passed to _localize_metakernel without error."""
    k1 = tmp_path / "fk" / "juice_fk.tf"
    k2 = tmp_path / "spk" / "juice_spk.bsp"
    k1.parent.mkdir()
    k1.write_text("placeholder")
    k2.parent.mkdir()
    k2.write_text("placeholder")

    sm = _make_sm(tmp_path)
    out = tmp_path / "snapshot.tm"

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        mock_spice.ktotal.return_value = 2
        mock_spice.kdata.side_effect = [
            (str(k1), "TEXT", "file", 1),
            (str(k2), "SPK", "file", 2),
        ]
        sm.snapshot_pool(out)

    content = out.read_text(encoding="utf-8")
    # Paths are expressed as $KERNELS/... relative to the common prefix
    assert "$KERNELS" in content
    assert "fk/juice_fk.tf" in content
    assert "spk/juice_spk.bsp" in content
    # Snapshot uses PATH_VALUES — localize must not error (it rewrites PATH_VALUES)
    localized = sm._localize_metakernel(out)
    try:
        assert localized.exists()
    finally:
        localized.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# version parameter resolution
# ---------------------------------------------------------------------------


def test_version_passed_to_download_kernels(tmp_path):
    """QuickSpiceManager._version is forwarded to download_kernels_via_ftp."""
    mk = _make_local_mk(tmp_path)
    sm = SpiceManager(
        mk="plan",
        kernels_dir=str(tmp_path),
        download_kernels=True,
        version="v461_20260121_001",
    )

    with patch(
        "quick_spice_manager.spice_manager.download_kernels_via_ftp",
        return_value=mk,
    ) as mock_dl:
        _ = sm.resolved_mk

    mock_dl.assert_called_once()
    _, kwargs = mock_dl.call_args
    assert kwargs.get("version") == "v461_20260121_001"


def test_version_latest_passed_to_download_kernels(tmp_path):
    """Default version='latest' is forwarded as-is."""
    mk = _make_local_mk(tmp_path)
    sm = SpiceManager(
        mk="plan",
        kernels_dir=str(tmp_path),
        download_kernels=True,
        # version not set → defaults to "latest"
    )

    with patch(
        "quick_spice_manager.spice_manager.download_kernels_via_ftp",
        return_value=mk,
    ) as mock_dl:
        _ = sm.resolved_mk

    mock_dl.assert_called_once()
    _, kwargs = mock_dl.call_args
    assert kwargs.get("version") == "latest"


def test_download_kernels_flag_forwarded_to_download_kernels_via_ftp(tmp_path):
    """QuickSpiceManager._download_kernels is forwarded to
    download_kernels_via_ftp() -- it must actually gate network access,
    not just be stored inertly."""
    mk = _make_local_mk(tmp_path)
    sm = SpiceManager(
        mk="plan",
        kernels_dir=str(tmp_path),
        download_kernels=False,
    )

    with patch(
        "quick_spice_manager.spice_manager.download_kernels_via_ftp",
        return_value=mk,
    ) as mock_dl:
        _ = sm.resolved_mk

    mock_dl.assert_called_once()
    _, kwargs = mock_dl.call_args
    assert kwargs.get("download_kernels") is False


def test_download_kernels_false_raises_without_cache(tmp_path):
    """End-to-end: download_kernels=False with an unresolvable shortcut and
    no prior cache raises FileNotFoundError rather than reaching FTP."""
    sm = SpiceManager(
        spacecraft="JUICE",
        mk="plan",
        kernels_dir=str(tmp_path),
        download_kernels=False,
    )

    with patch("quick_spice_manager.ftp.ftplib.FTP") as mock_ftp_cls:
        with pytest.raises(FileNotFoundError, match="download_kernels=False"):
            sm.resolved_mk

    mock_ftp_cls.assert_not_called()


def test_version_change_invalidates_resolved_mk(tmp_path):
    """Changing _version clears the resolved_mk cache (without deleting the
    active temp file if kernels are loaded)."""
    mk = _make_local_mk(tmp_path)
    sm = _make_sm(tmp_path, mk=mk)

    with patch(
        "quick_spice_manager.spice_manager.download_kernels_via_ftp",
        return_value=mk,
    ):
        first_path = sm.resolved_mk

    assert sm._localized_mk_path == first_path

    sm._version = "v461_20260121_001"

    assert sm._localized_mk_path is None  # cache invalidated
    assert not first_path.exists()         # temp file deleted (not loaded)


def test_version_change_while_loaded_preserves_temp_file(tmp_path):
    """If kernels are loaded, changing _version must NOT delete the temp file
    because spiceypy.unload() needs to re-read it."""
    mk = _make_local_mk(tmp_path)
    sm = _make_sm(tmp_path, mk=mk)

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        mock_spice.ktotal.return_value = 1
        mock_spice.kdata.return_value = ("/kernels/juice_spk.bsp", "SPK", "file", 0)
        sm.load_kernels()

    loaded_path = sm._loaded_mk_path
    assert loaded_path is not None and loaded_path.exists()

    sm._version = "v461_20260121_001"

    assert sm._localized_mk_path is None  # cache pointer cleared
    assert loaded_path.exists()            # temp file kept — unload_kernels() owns it
    loaded_path.unlink(missing_ok=True)    # manual cleanup


# ---------------------------------------------------------------------------
# load_kernels / unload_kernels
# ---------------------------------------------------------------------------


def test_load_kernels_calls_furnsh(tmp_path):
    mk = _make_local_mk(tmp_path)
    sm = _make_sm(tmp_path, mk=mk)

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        # After furnsh, _pool_files() is called; simulate one loaded kernel
        mock_spice.ktotal.return_value = 1
        mock_spice.kdata.return_value = ("/kernels/juice_spk.bsp", "SPK", "file", 0)
        returned = sm.load_kernels()

    assert returned == sm._localized_mk_path  # load_kernels now returns the localized path
    assert sm.resolved_mk == returned          # resolved_mk is the same localized path
    assert str(returned) != str(mk)            # localized copy is a different file
    # furnsh must have been called with the localized file
    assert mock_spice.furnsh.call_count == 1
    furnished_path = mock_spice.furnsh.call_args[0][0]
    assert furnished_path.endswith(".tm")
    assert furnished_path != str(mk)
    # temp file should still exist (kept for unload)
    assert sm._loaded_mk_path is not None
    assert sm._loaded_mk_path.exists()
    # cleanup
    sm._loaded_mk_path.unlink(missing_ok=True)


def test_unload_kernels_calls_unload_and_cleans_up(tmp_path):
    mk = _make_local_mk(tmp_path)
    sm = _make_sm(tmp_path, mk=mk)

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        mock_spice.ktotal.return_value = 1
        mock_spice.kdata.return_value = ("/kernels/juice_spk.bsp", "SPK", "file", 0)
        sm.load_kernels()
        tmp_mk_path = sm._loaded_mk_path
        assert tmp_mk_path.exists()
        sm.unload_kernels()

    mock_spice.unload.assert_called_once_with(str(tmp_mk_path))
    assert not tmp_mk_path.exists()
    assert sm._loaded_mk_path is None
    assert sm._localized_mk_path is None  # resolved_mk cache cleared by unload_kernels


def test_reconfigure_while_loaded_does_not_delete_active_temp_file(tmp_path):
    """Changing _mk/_version after load_kernels() must NOT delete the temp file —
    spiceypy.unload() needs to re-read it.  unload_kernels() is still responsible
    for deleting it afterwards."""
    mk = _make_local_mk(tmp_path)
    sm = _make_sm(tmp_path, mk=mk)

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        mock_spice.ktotal.return_value = 1
        mock_spice.kdata.return_value = ("/kernels/juice_spk.bsp", "SPK", "file", 0)
        sm.load_kernels()

    loaded_path = sm._loaded_mk_path
    assert loaded_path is not None and loaded_path.exists()

    # Reconfigure — should invalidate the resolved_mk cache pointer but NOT
    # delete the file that is still actively loaded in the pool.
    sm._mk = "ops"

    assert sm._localized_mk_path is None          # cache pointer cleared
    assert loaded_path.exists()                   # temp file still on disk
    assert sm._loaded_mk_path == loaded_path      # load state unchanged

    # Cleanup manually (normally done by unload_kernels).
    loaded_path.unlink(missing_ok=True)


def test_context_manager_nested_restores_active(tmp_path):
    """When inner context manager exits, outer manager becomes active again."""
    k_juice = "/kernels/juice_spk.bsp"
    k_ros = "/kernels/ros_spk.bsp"
    mk = _make_local_mk(tmp_path)

    sm_juice = SpiceManager(mk=str(mk), kernels_dir=str(tmp_path), download_kernels=False)
    sm_ros = SpiceManager(mk=str(mk), kernels_dir=str(tmp_path), download_kernels=False)

    # Simulate juice entering its context, then rosetta entering and exiting inside
    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        # juice __enter__: empty pool snapshot, then furnsh
        mock_spice.ktotal.side_effect = [
            0,                  # juice __enter__: ktotal for snapshot
            1, 1,               # juice load_kernels: furnsh + _pool_files() (ktotal x2 — once for range, once checked sep)
            1,                  # rosetta __enter__: ktotal for snapshot (juice kernels in pool)
            1, 1,               # rosetta load_kernels: furnsh + _pool_files()
            0,                  # rosetta __exit__: ktotal in kclear (no re-furnish needed — saved=[juice])
            0,                  # juice __exit__: ktotal in kclear
        ]
        mock_spice.ktotal.side_effect = None  # simpler: just use return_value per call stage

        # Use a stateful side_effect list matching the real call sequence
        ktotal_calls = iter([
            0,   # juice enter: snapshot loop count
            1,   # juice _pool_files after furnsh
            1,   # rosetta enter: snapshot loop count (juice kernel in pool)
            1,   # rosetta _pool_files after furnsh
            0,   # rosetta exit: no kernels to re-furnish (saved = juice kernel, but we just test active)
        ])
        mock_spice.ktotal.side_effect = lambda *a: next(ktotal_calls)
        mock_spice.kdata.side_effect = [
            (k_juice, "SPK", "file", 0),   # juice _pool_files
            (k_juice, "SPK", "file", 0),   # rosetta enter snapshot
            (k_ros, "SPK", "file", 0),     # rosetta _pool_files
        ]

        sm_juice.__enter__()
        assert sm_juice.is_active is True

        sm_ros.__enter__()
        assert sm_ros.is_active is True
        assert sm_juice.is_active is False  # superseded

        sm_ros.__exit__(None, None, None)
        # After inner exit, outer manager should be active again
        assert sm_ros.is_active is False
        assert sm_juice.is_active is True

        sm_juice.__exit__(None, None, None)
        assert sm_juice.is_active is False  # fully exited

def test_context_manager_exclusive_clears_pool_on_enter(tmp_path):
    """exclusive=True must kclear() before loading so the pool is exact."""
    existing_kernel = tmp_path / "preloaded.bsp"
    existing_kernel.write_text("placeholder")
    mk = _make_local_mk(tmp_path)
    sm = SpiceManager(
        mk=str(mk),
        kernels_dir=str(tmp_path),
        download_kernels=False,
        exclusive=True,
    )

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        mock_spice.ktotal.return_value = 1
        mock_spice.kdata.return_value = (str(existing_kernel), "SPK", "file", 1)

        with sm:
            pass

    # kclear() must be called twice: once on enter (exclusive), once on exit (restore)
    assert mock_spice.kclear.call_count == 2


def test_context_manager_non_exclusive_does_not_kclear_on_enter(tmp_path):
    """Default (exclusive=False): existing kernels stay in pool during block."""
    existing_kernel = tmp_path / "preloaded.bsp"
    existing_kernel.write_text("placeholder")
    mk = _make_local_mk(tmp_path)
    sm = _make_sm(tmp_path, mk=mk, exclusive=False)

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        mock_spice.ktotal.return_value = 1
        mock_spice.kdata.return_value = (str(existing_kernel), "SPK", "file", 1)

        with sm:
            pass

    # kclear() called only once on exit (restore), never on enter
    assert mock_spice.kclear.call_count == 1

# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


def test_context_manager_loads_and_restores(tmp_path):
    existing_kernel = tmp_path / "existing.bsp"
    existing_kernel.write_text("placeholder")
    mk = _make_local_mk(tmp_path)
    sm = _make_sm(tmp_path, mk=mk, exclusive=False)

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        # Simulate one existing kernel in the pool (non-META)
        mock_spice.ktotal.return_value = 1
        mock_spice.kdata.return_value = (str(existing_kernel), "SPK", "file", 1)

        with sm:
            assert mock_spice.furnsh.call_count == 1  # load_kernels called

        # After exit: kclear called once (restore only, no exclusive clear on enter)
        mock_spice.kclear.assert_called_once()
        assert mock_spice.furnsh.call_count == 2
        second_furnsh_arg = mock_spice.furnsh.call_args_list[1][0][0]
        assert second_furnsh_arg == str(existing_kernel)

    assert sm._saved_kernels == []
    assert sm._loaded_mk_path is None


def test_context_manager_skips_meta_kernel_type(tmp_path):
    """META-typed entries in spiceypy pool are skipped during snapshot."""
    mk = _make_local_mk(tmp_path)
    sm = _make_sm(tmp_path, mk=mk)

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        mock_spice.ktotal.return_value = 1
        mock_spice.kdata.return_value = ("/some/tempfile.tm", "META", "file", 1)

        with sm:
            assert sm._saved_kernels == []  # META entry was skipped


def test_context_manager_skips_nonexistent_kernel_on_restore(tmp_path):
    """If a saved kernel file no longer exists on exit, re-furnsh is skipped."""
    phantom = tmp_path / "gone.bsp"  # intentionally not created
    mk = _make_local_mk(tmp_path)
    sm = _make_sm(tmp_path, mk=mk)

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        # phantom does not exist, so it should NOT be snapshotted on enter
        mock_spice.ktotal.return_value = 1
        mock_spice.kdata.return_value = (str(phantom), "SPK", "file", 1)

        with sm:
            saved = list(sm._saved_kernels)

        assert saved == []  # was not snapshotted
        # Only the re-furnsh of load_kernels was made; nothing re-furnished on exit
        assert mock_spice.furnsh.call_count == 1


def test_context_manager_propagates_exceptions(tmp_path):
    """Exceptions raised inside the with-block are not swallowed."""
    mk = _make_local_mk(tmp_path)
    sm = _make_sm(tmp_path, mk=mk)

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        mock_spice.ktotal.return_value = 0  # empty pool — no snapshot loop needed
        with pytest.raises(RuntimeError, match="boom"):
            with sm:
                raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# tour_config — lazy planetary_coverage import
# ---------------------------------------------------------------------------


def test_tour_config_raises_without_planetary_coverage(tmp_path):
    mk = _make_local_mk(tmp_path)
    sm = _make_sm(tmp_path, mk=mk)

    import builtins
    real_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "planetary_coverage":
            raise ImportError("No module named 'planetary_coverage'")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=mock_import):
        with pytest.raises(ImportError, match="pip install quick-spice-manager\\[planetary-coverage\\]"):
            sm.tour_config


def test_tour_config_property_delegates_to_get_tour_config_defaults(tmp_path):
    """The tour_config property is a backward-compatible convenience form of
    get_tour_config() using the documented defaults (target='Jupiter',
    instrument='JANUS') -- restored for downstream code that accesses
    `.tour_config` as a plain attribute rather than calling it."""
    sm = _make_sm(tmp_path)
    sentinel = object()

    with patch.object(
        SpiceManager, "get_tour_config", return_value=sentinel,
    ) as mock_get:
        result = sm.tour_config

    assert result is sentinel
    mock_get.assert_called_once_with()


def test_config_property_delegates_to_get_config_defaults(tmp_path):
    """The config property is a backward-compatible convenience form of
    get_config() using the documented defaults."""
    sm = _make_sm(tmp_path)
    sentinel = object()

    with patch.object(SpiceManager, "get_config", return_value=sentinel) as mock_get:
        result = sm.config

    assert result is sentinel
    mock_get.assert_called_once_with()


# ---------------------------------------------------------------------------
# Concurrency / multi-process safety
# ---------------------------------------------------------------------------


def test_pool_lock_serializes_concurrent_load_kernels(tmp_path):
    """Two threads calling load_kernels() concurrently never interleave the
    kclear()/furnsh() pair — the whole method body is one critical section."""
    events: list[tuple[int, str]] = []
    events_lock = threading.Lock()

    def record(kind: str) -> None:
        with events_lock:
            events.append((threading.get_ident(), kind))

    def make_side_effect():
        def _side_effect(*_args, **_kwargs):
            record("start")
            time.sleep(0.05)
            record("end")

        return _side_effect

    sm1 = _make_sm(tmp_path)
    sm2 = _make_sm(tmp_path)

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        mock_spice.ktotal.return_value = 1
        mock_spice.kdata.return_value = ("/kernels/dummy.bsp", "SPK", "file", 0)
        mock_spice.kclear.side_effect = make_side_effect()
        mock_spice.furnsh.side_effect = make_side_effect()

        t1 = threading.Thread(target=sm1.load_kernels)
        t2 = threading.Thread(target=sm2.load_kernels)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

    assert not t1.is_alive()
    assert not t2.is_alive()

    # At no point should both threads' kclear/furnsh calls be "in flight"
    # at the same time — that would mean the lock failed to serialize them.
    open_idents: set[int] = set()
    max_concurrent = 0
    for ident, kind in events:
        if kind == "start":
            open_idents.add(ident)
            max_concurrent = max(max_concurrent, len(open_idents))
        else:
            open_idents.discard(ident)
    assert max_concurrent == 1


def test_load_kernels_reentrant_call_does_not_deadlock(tmp_path):
    """load_kernels() calling self.unload_kernels() internally while already
    holding the pool lock must not deadlock (requires RLock, not Lock)."""
    sm = _make_sm(tmp_path)

    def run() -> None:
        with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
            mock_spice.ktotal.return_value = 1
            mock_spice.kdata.return_value = ("/kernels/dummy.bsp", "SPK", "file", 0)
            sm.load_kernels()
            sm.load_kernels()  # already loaded -> internally calls unload_kernels()

    t = threading.Thread(target=run)
    t.start()
    t.join(timeout=5)

    assert not t.is_alive()  # would still be alive if the lock deadlocked


def test_resolved_mk_concurrent_access_creates_single_temp_file(tmp_path):
    """Concurrent first-time access to .resolved_mk on one instance must not
    leak an orphaned localized temp file or hand out two different paths."""
    # Unique stem so this test's temp file can't be confused with leftovers
    # from other tests (_localize_metakernel writes into the system temp
    # dir, not tmp_path).
    unique_name = f"concurrency_test_{uuid.uuid4().hex}"
    mk = _make_local_mk(tmp_path, content=_MINIMAL_MK)
    mk = mk.rename(tmp_path / f"{unique_name}.tm")
    sm = _make_sm(tmp_path, mk=mk)

    original_localize = sm._localize_metakernel

    def slow_localize(mk_path):
        time.sleep(0.1)
        return original_localize(mk_path)

    results: list[Path] = []
    results_lock = threading.Lock()

    def worker() -> None:
        p = sm.resolved_mk
        with results_lock:
            results.append(p)

    with patch.object(SpiceManager, "_localize_metakernel", side_effect=slow_localize):
        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

    assert all(not t.is_alive() for t in threads)
    assert len(results) == 3
    assert len(set(results)) == 1  # every thread got the same resolved path

    # _localize_metakernel() writes into the system temp dir (not tmp_path);
    # only one such file should exist for this mk stem — a second one would
    # indicate a leaked orphan from a lost check-and-set race.
    localized_files = list(
        Path(tempfile.gettempdir()).glob(f"{mk.stem}-localized-*.tm")
    )
    assert len(localized_files) == 1  # no orphaned duplicate temp file

    sm._localized_mk_path.unlink(missing_ok=True)


def test_context_manager_blocks_second_thread_for_whole_duration(tmp_path):
    """with sm: holds the pool lock for the entire block, so a second
    thread's with-block cannot start until the first one fully exits."""
    mk = _make_local_mk(tmp_path)
    sm1 = _make_sm(tmp_path, mk=mk)
    sm2 = _make_sm(tmp_path, mk=mk)

    a_entered = threading.Event()
    a_may_exit = threading.Event()
    b_entered = threading.Event()

    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        mock_spice.ktotal.return_value = 0  # empty pool, no snapshot/kdata needed

        def thread_a() -> None:
            with sm1:
                a_entered.set()
                a_may_exit.wait(timeout=5)

        def thread_b() -> None:
            a_entered.wait(timeout=5)
            with sm2:
                b_entered.set()

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start()
        assert a_entered.wait(timeout=5)
        tb.start()

        # Thread A still holds the lock (hasn't hit a_may_exit yet), so
        # thread B must still be blocked trying to enter its own `with` block.
        time.sleep(0.2)
        assert not b_entered.is_set()

        a_may_exit.set()
        ta.join(timeout=5)
        tb.join(timeout=5)

    assert not ta.is_alive()
    assert not tb.is_alive()
    assert b_entered.is_set()


def test_get_cache_lock_path_outside_cache_dir(tmp_path):
    """The cache lock file lives in the cache dir's parent, never inside it,
    so shutil.rmtree(cache_dir) in clear_cache() can never delete it."""
    cache_dir = tmp_path / "kernels" / "juice"
    lock_path = get_cache_lock_path(cache_dir)

    assert lock_path.parent == cache_dir.parent
    assert lock_path.name == ".juice.cache.lock"


def test_clear_cache_lock_survives_rmtree(tmp_path):
    """clear_cache() must leave its own lock file usable after returning —
    i.e. it wasn't nested inside the directory tree it just rmtree'd."""
    cache_dir = tmp_path / "cache" / "juice"
    cache_dir.mkdir(parents=True)
    (cache_dir / "dummy_kernel.bsp").write_bytes(b"data")

    sm = _make_sm(tmp_path)

    with patch.object(
        type(sm),
        "user_kernels_cache_directory",
        new_callable=PropertyMock,
        return_value=cache_dir,
    ):
        sm.clear_cache()

    assert cache_dir.exists()
    assert list(cache_dir.iterdir()) == []

    lock_path = get_cache_lock_path(cache_dir)
    lock = filelock.FileLock(str(lock_path), timeout=1)
    lock.acquire()  # would raise Timeout if clear_cache() left it held
    lock.release()


def test_load_kernels_uses_furnish_slot(tmp_path):
    """load_kernels() acquires a cross-process furnish slot for its
    kernels_dir before touching the pool."""
    import contextlib

    mk = _make_local_mk(tmp_path)
    sm = _make_sm(tmp_path, mk=mk)

    with (
        patch.object(
            spice_manager_module, "_furnish_slot",
            return_value=contextlib.nullcontext(),
        ) as mock_slot,
        patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice,
    ):
        mock_spice.ktotal.return_value = 1
        mock_spice.kdata.return_value = ("/kernels/dummy.bsp", "SPK", "file", 0)
        sm.load_kernels()

    mock_slot.assert_called_once_with(sm._kernels_dir)


def test_furnish_slot_bounds_concurrent_managers(tmp_path):
    """No more than _MAX_CONCURRENT_FURNISH_SLOTS managers can be "inside"
    _furnish_slot() for the same kernels_dir at once; extras wait their turn."""
    max_slots = spice_manager_module._MAX_CONCURRENT_FURNISH_SLOTS
    release = threading.Event()
    concurrent_count = 0
    max_concurrent_seen = 0
    count_lock = threading.Lock()

    def hold_slot():
        nonlocal concurrent_count, max_concurrent_seen
        with spice_manager_module._furnish_slot(tmp_path):
            with count_lock:
                concurrent_count += 1
                max_concurrent_seen = max(max_concurrent_seen, concurrent_count)
            release.wait(timeout=5)
            with count_lock:
                concurrent_count -= 1

    threads = [threading.Thread(target=hold_slot) for _ in range(max_slots + 2)]
    for t in threads:
        t.start()

    time.sleep(0.3)  # let every thread attempt acquisition
    assert concurrent_count == max_slots  # extras are waiting, not "inside"

    release.set()
    for t in threads:
        t.join(timeout=5)

    assert all(not t.is_alive() for t in threads)
    assert max_concurrent_seen == max_slots


def test_furnish_slot_scoped_per_kernels_dir(tmp_path):
    """Two different kernels_dir paths get independent furnish-slot pools --
    unrelated missions never wait on each other."""
    max_slots = spice_manager_module._MAX_CONCURRENT_FURNISH_SLOTS
    dir_a = tmp_path / "juice"
    dir_b = tmp_path / "rosetta"

    held = [spice_manager_module._furnish_slot(dir_a) for _ in range(max_slots)]
    for cm in held:
        cm.__enter__()
    try:
        # dir_a is fully saturated, but dir_b's pool is untouched and free.
        with spice_manager_module._furnish_slot(dir_b):
            pass
    finally:
        for cm in held:
            cm.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# kernel_provenance
# ---------------------------------------------------------------------------


def test_kernel_provenance_manager_reports_metakernel_and_kernels(tmp_path):
    k1 = "/kernels/juice_spk.bsp"
    sm, _ = _sm_loaded(tmp_path, [k1])

    info = sm.kernel_provenance()

    assert info["metakernel"] == sm._mk
    assert info["spacecraft"] == "JUICE"
    assert info["version"] == "latest"
    assert info["resolved_at"] is not None
    assert info["all_kernels"] == [k1]
    assert info["extra_kernels"] == []


def test_kernel_provenance_manager_reports_added_extra_kernels(tmp_path):
    k1 = "/kernels/juice_spk.bsp"
    extra = tmp_path / "custom.bc"
    extra.write_text("placeholder")
    sm, _ = _sm_loaded(tmp_path, [k1])

    with patch("quick_spice_manager.spice_manager.spiceypy"):
        sm.add_kernel(extra)

    info = sm.kernel_provenance()

    assert info["extra_kernels"] == [str(extra.resolve())]
    assert info["all_kernels"] == sorted([k1, str(extra.resolve())])


def test_kernel_provenance_manager_raises_before_load(tmp_path):
    sm = _make_sm(tmp_path)
    with pytest.raises(RuntimeError, match=r"load_kernels\(\)"):
        sm.kernel_provenance()


def test_kernel_provenance_manager_raises_when_superseded(tmp_path):
    sm_juice, _ = _sm_loaded(tmp_path, ["/kernels/juice_spk.bsp"])
    _sm_loaded(tmp_path, ["/kernels/ros_spk.bsp"])  # supersedes juice
    with pytest.raises(RuntimeError, match="no longer the active pool owner"):
        sm_juice.kernel_provenance()


def test_kernel_provenance_invalid_source_raises(tmp_path):
    sm, _ = _sm_loaded(tmp_path, ["/kernels/juice_spk.bsp"])
    with pytest.raises(ValueError, match="source must be"):
        sm.kernel_provenance(source="bogus")


def _write_pool_metakernel(tmp_path, kernels_dir, relative_kernels):
    """Write a real, minimal, already-"localized"-style .tm file so
    _kernels_referenced_by_metakernel() can parse it independent of any
    manager -- exactly like the pool mode is meant to work."""
    tm = tmp_path / "pool_meta.tm"
    kernels_block = "\n".join(f"    '$KERNELS/{k}'" for k in relative_kernels)
    tm.write_text(
        textwrap.dedent(f"""\
            KPL/MK

            \\begindata
            PATH_VALUES = ( '{kernels_dir}' )
            PATH_SYMBOLS = ( 'KERNELS' )
            KERNELS_TO_LOAD = (
            {kernels_block}
            )
            \\begintext
        """),
        encoding="utf-8",
    )
    return tm


def test_kernel_provenance_pool_reports_metakernel_and_extra_kernels(tmp_path):
    kernels_dir = tmp_path / "kernels" / "juice"
    tm = _write_pool_metakernel(tmp_path, kernels_dir, ["fk/juice_v45.tf"])

    metakernel_kernel = str(kernels_dir / "fk/juice_v45.tf")
    extra_kernel = "/extra/custom.bc"

    sm = _make_sm(tmp_path)
    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        mock_spice.ktotal.return_value = 3
        mock_spice.kdata.side_effect = [
            (str(tm), "META", "file", 0),
            (metakernel_kernel, "TEXT", "file", 1),
            (extra_kernel, "CK", "file", 2),
        ]
        info = sm.kernel_provenance(source="pool")

    assert info["metakernel"] == str(tm)
    assert info["spacecraft"] is None
    assert info["mk"] is None
    assert info["version"] is None
    assert info["resolved_at"] is None
    assert info["all_kernels"] == sorted([metakernel_kernel, extra_kernel])
    assert info["extra_kernels"] == [extra_kernel]


def test_kernel_provenance_pool_no_metakernel_returns_none(tmp_path):
    sm = _make_sm(tmp_path)
    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        mock_spice.ktotal.return_value = 1
        mock_spice.kdata.return_value = ("/kernels/juice_spk.bsp", "SPK", "file", 0)
        info = sm.kernel_provenance(source="pool")

    assert info["metakernel"] is None
    assert info["all_kernels"] == ["/kernels/juice_spk.bsp"]
    assert info["extra_kernels"] == ["/kernels/juice_spk.bsp"]


def test_kernel_provenance_pool_works_without_loading(tmp_path):
    """source='pool' needs no load_kernels() call at all -- pure pool inspection."""
    sm = _make_sm(tmp_path)
    with patch("quick_spice_manager.spice_manager.spiceypy") as mock_spice:
        mock_spice.ktotal.return_value = 0
        info = sm.kernel_provenance(source="pool")

    assert info["metakernel"] is None
    assert info["all_kernels"] == []
    assert info["extra_kernels"] == []
