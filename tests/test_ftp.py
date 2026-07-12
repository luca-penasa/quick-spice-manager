"""
Tests for the FTP module (ftp.py).
All network I/O is mocked — no real FTP connection is made.
"""

from __future__ import annotations

import contextlib
import ftplib
import textwrap
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import filelock
import pytest

from quick_spice_manager import ftp as ftp_module
from quick_spice_manager.ftp import (
    _MISSION_FTP_BASE,
    _canonical_mission,
    _download_one_kernel,
    _ftp_base,
    _ftp_download_file,
    _locked_download,
    _parse_mk_kernel_paths,
    _resolve_tm_on_ftp,
    download_kernels_via_ftp,
    list_metakernels_via_ftp,
)

# ---------------------------------------------------------------------------
# _canonical_mission / _ftp_base
# ---------------------------------------------------------------------------


def test_canonical_mission_juice():
    assert _canonical_mission("JUICE") == "JUICE"
    assert _canonical_mission("juice") == "JUICE"


def test_canonical_mission_alias():
    assert _canonical_mission("SOLO") == "SOLAR-ORBITER"
    assert _canonical_mission("MEX") == "MARS-EXPRESS"
    assert _canonical_mission("TGO") == "EXOMARS2016"


def test_ftp_base_known():
    base = _ftp_base("JUICE")
    assert base == "/data/SPICE/JUICE/"


def test_ftp_base_alias():
    assert _ftp_base("SOLO") == _MISSION_FTP_BASE["SOLAR-ORBITER"]


def test_ftp_base_unknown():
    with pytest.raises(ValueError, match="No FTP path known"):
        _ftp_base("UNKNOWN_MISSION_XYZ")


# ---------------------------------------------------------------------------
# _parse_mk_kernel_paths
# ---------------------------------------------------------------------------

_SAMPLE_TM = textwrap.dedent(
    r"""
    KPL/MK

    \begindata

         PATH_VALUES       = ( '..' )

         PATH_SYMBOLS      = ( 'KERNELS' )

         KERNELS_TO_LOAD   = (

                               '$KERNELS/ck/juice_sc_default_v01.bc'
                               '$KERNELS/fk/juice_v45.tf'
                               '$KERNELS/lsk/naif0012.tls'

                             )

    \begintext
    """
)


def test_parse_mk_kernel_paths_basic():
    paths = _parse_mk_kernel_paths(_SAMPLE_TM)
    assert paths == [
        "ck/juice_sc_default_v01.bc",
        "fk/juice_v45.tf",
        "lsk/naif0012.tls",
    ]


def test_parse_mk_kernel_paths_empty():
    assert _parse_mk_kernel_paths("KPL/MK\n\n\\begindata\n\n\\begintext\n") == []


def test_parse_mk_kernel_paths_no_symbol():
    tm = textwrap.dedent(
        r"""
        KPL/MK
        \begindata
             KERNELS_TO_LOAD = ( 'naif0012.tls' )
        \begintext
        """
    )
    # When there are no PATH_SYMBOLS, the entry is returned as-is
    assert _parse_mk_kernel_paths(tm) == ["naif0012.tls"]


# ---------------------------------------------------------------------------
# _resolve_tm_on_ftp
# ---------------------------------------------------------------------------


def _make_ftp_mock(nlst_return: list[str]) -> MagicMock:
    ftp = MagicMock()
    ftp.nlst.return_value = nlst_return
    return ftp


def test_resolve_tm_exact_spacecraft_prefix():
    ftp = _make_ftp_mock([
        "/data/SPICE/JUICE/kernels/mk/juice_plan.tm",
        "/data/SPICE/JUICE/kernels/mk/juice_crema_5_0.tm",
    ])
    path = _resolve_tm_on_ftp(ftp, "JUICE", "plan", "/data/SPICE/JUICE/")
    assert path == "/data/SPICE/JUICE/kernels/mk/juice_plan.tm"


def test_resolve_tm_bare_name():
    ftp = _make_ftp_mock([
        "/data/SPICE/JUICE/kernels/mk/juice_plan.tm",
        "/data/SPICE/JUICE/kernels/mk/plan.tm",  # bare match
    ])
    # "plan.tm" is a bare-name match for mk="plan"
    path = _resolve_tm_on_ftp(ftp, "JUICE", "plan.tm", "/data/SPICE/JUICE/")
    assert path == "/data/SPICE/JUICE/kernels/mk/juice_plan.tm"


def test_resolve_tm_fuzzy_fallback():
    ftp = _make_ftp_mock([
        "/data/SPICE/JUICE/kernels/mk/juice_crema_5_0.tm",
        "/data/SPICE/JUICE/kernels/mk/juice_crema_5_0_v462_20260223_001.tm",
    ])
    # Neither exact name; fuzzy picks shorter one
    path = _resolve_tm_on_ftp(ftp, "JUICE", "crema_5_0", "/data/SPICE/JUICE/")
    assert path == "/data/SPICE/JUICE/kernels/mk/juice_crema_5_0.tm"


def test_resolve_tm_not_found():
    ftp = _make_ftp_mock(["/data/SPICE/JUICE/kernels/mk/juice_plan.tm"])
    with pytest.raises(FileNotFoundError, match="Cannot find a metakernel"):
        _resolve_tm_on_ftp(ftp, "JUICE", "nonexistent_mk", "/data/SPICE/JUICE/")


def test_resolve_tm_versioned_in_mk_dir():
    """Versioned TM found in the top-level mk/ when the stem resolves first."""
    ftp = MagicMock()
    # mk/ contains both the unversioned alias and the specific versioned file
    ftp.nlst.return_value = [
        "/data/SPICE/JUICE/kernels/mk/juice_plan.tm",
        "/data/SPICE/JUICE/kernels/mk/juice_plan_v462_20260223_001.tm",
    ]
    path = _resolve_tm_on_ftp(
        ftp, "JUICE", "plan", "/data/SPICE/JUICE/", version="v462_20260223_001"
    )
    assert path == "/data/SPICE/JUICE/kernels/mk/juice_plan_v462_20260223_001.tm"
    # former_versions should NOT have been listed (found in mk/)
    ftp.nlst.assert_called_once()


def test_resolve_tm_versioned_in_former_versions():
    """Versioned TM found in former_versions/ via the unversioned stem."""
    fv_path = (
        "/data/SPICE/JUICE/kernels/mk/former_versions/"
        "juice_s011_tr03_v461_20260121_001.tm"
    )
    ftp = MagicMock()
    ftp.nlst.side_effect = [
        # mk/ — has unversioned alias but NOT the v461 file
        ["/data/SPICE/JUICE/kernels/mk/juice_s011_tr03.tm"],
        # former_versions/
        [fv_path],
    ]
    path = _resolve_tm_on_ftp(
        ftp, "JUICE", "tr03", "/data/SPICE/JUICE/", version="v461_20260121_001"
    )
    assert path == fv_path


def test_resolve_tm_versioned_fallback_raises(tmp_path):
    """When version tag not found anywhere, raise FileNotFoundError rather than
    silently returning the wrong TM."""
    ftp = MagicMock()
    ftp.nlst.side_effect = [
        ["/data/SPICE/JUICE/kernels/mk/juice_plan.tm"],  # mk/
        [],  # former_versions/ — nothing for this version
    ]
    with pytest.raises(FileNotFoundError, match="v999_99999999_001"):
        _resolve_tm_on_ftp(
            ftp, "JUICE", "plan", "/data/SPICE/JUICE/", version="v999_99999999_001"
        )


@pytest.mark.parametrize("version", ["latest", "LATEST", "Latest"])
def test_resolve_tm_version_latest_returns_unversioned(version: str):
    """'latest' (any casing) is treated as unversioned — no Step 2 lookup."""
    ftp = MagicMock()
    ftp.nlst.return_value = [
        "/data/SPICE/JUICE/kernels/mk/juice_plan.tm",
        "/data/SPICE/JUICE/kernels/mk/juice_plan_v462_20260223_001.tm",
    ]
    path = _resolve_tm_on_ftp(ftp, "JUICE", "plan", "/data/SPICE/JUICE/", version=version)
    assert path == "/data/SPICE/JUICE/kernels/mk/juice_plan.tm"
    # former_versions/ must never be listed for 'latest'
    ftp.nlst.assert_called_once()


def test_resolve_tm_version_all_returns_unversioned():
    """'all' is also treated as unversioned (same as 'latest')."""
    ftp = MagicMock()
    ftp.nlst.return_value = [
        "/data/SPICE/JUICE/kernels/mk/juice_plan.tm",
        "/data/SPICE/JUICE/kernels/mk/juice_plan_v462_20260223_001.tm",
    ]
    path = _resolve_tm_on_ftp(ftp, "JUICE", "plan", "/data/SPICE/JUICE/", version="all")
    assert path == "/data/SPICE/JUICE/kernels/mk/juice_plan.tm"
    ftp.nlst.assert_called_once()


def test_resolve_tm_version_default_is_unversioned():
    """Omitting version (default) behaves the same as version='latest'."""
    ftp = MagicMock()
    ftp.nlst.return_value = ["/data/SPICE/JUICE/kernels/mk/juice_plan.tm"]
    path = _resolve_tm_on_ftp(ftp, "JUICE", "plan", "/data/SPICE/JUICE/")
    assert path == "/data/SPICE/JUICE/kernels/mk/juice_plan.tm"
    ftp.nlst.assert_called_once()


def test_resolve_tm_former_versions_permission_error_raises():
    """If former_versions/ raises error_perm (directory absent) and the versioned
    TM is not in mk/ either, raise FileNotFoundError rather than silently
    falling back to the wrong TM."""
    ftp = MagicMock()
    ftp.nlst.side_effect = [
        ["/data/SPICE/JUICE/kernels/mk/juice_plan.tm"],  # mk/ — no versioned file
        ftplib.error_perm("550 No such directory"),       # former_versions/ absent
    ]
    with pytest.raises(FileNotFoundError, match="v461_20260121_001"):
        _resolve_tm_on_ftp(
            ftp, "JUICE", "plan", "/data/SPICE/JUICE/", version="v461_20260121_001"
        )


# ---------------------------------------------------------------------------
# list_metakernels_via_ftp
# ---------------------------------------------------------------------------


def test_list_metakernels_via_ftp():
    fake_entries = [
        "/data/SPICE/JUICE/kernels/mk/aareadme.txt",
        "/data/SPICE/JUICE/kernels/mk/juice_plan.tm",
        "/data/SPICE/JUICE/kernels/mk/juice_crema_5_0.tm",
        "/data/SPICE/JUICE/kernels/mk/juice_plan_v462_20260226_001.tm",
    ]
    mock_ftp = MagicMock()
    mock_ftp.nlst.return_value = fake_entries

    with patch("quick_spice_manager.ftp.ftplib.FTP", return_value=mock_ftp):
        result = list_metakernels_via_ftp("JUICE")

    assert "juice_plan" in result
    assert "juice_crema_5_0" in result
    # Non-.tm files should not appear
    assert not any("aareadme" in r for r in result)


# ---------------------------------------------------------------------------
# download_kernels_via_ftp
# ---------------------------------------------------------------------------


def test_download_kernels_via_ftp(tmp_path: Path):
    """
    Full round-trip: TM + kernel files are downloaded via a mocked FTP.
    Files not yet present should be written; already-present ones skipped.
    """
    # Pre-create one kernel so it is skipped
    existing_kernel = tmp_path / "fk" / "juice_v45.tf"
    existing_kernel.parent.mkdir(parents=True)
    existing_kernel.write_bytes(b"pre-existing")

    # TM content that references two kernels
    tm_content = textwrap.dedent(
        r"""
        KPL/MK
        \begindata
             PATH_VALUES       = ( '..' )
             PATH_SYMBOLS      = ( 'KERNELS' )
             KERNELS_TO_LOAD   = (
                                   '$KERNELS/ck/juice_sc_default_v01.bc'
                                   '$KERNELS/fk/juice_v45.tf'
                                 )
        \begintext
        """
    ).encode()

    tm_remote = "/data/SPICE/JUICE/kernels/mk/juice_plan.tm"
    ck_remote = "/data/SPICE/JUICE/kernels/ck/juice_sc_default_v01.bc"
    ck_content = b"ck-kernel-data"

    def fake_retrbinary(cmd: str, callback):
        remote = cmd.split(" ", 1)[1]
        data = {
            tm_remote: tm_content,
            ck_remote: ck_content,
        }
        callback(data[remote])

    mock_ftp = MagicMock()
    mock_ftp.nlst.return_value = [tm_remote]
    mock_ftp.retrbinary.side_effect = fake_retrbinary

    with patch("quick_spice_manager.ftp.ftplib.FTP", return_value=mock_ftp):
        local_tm = download_kernels_via_ftp("JUICE", "plan", tmp_path)

    # TM file should be written
    assert local_tm == tmp_path / "mk" / "juice_plan.tm"
    assert local_tm.exists()
    assert local_tm.read_bytes() == tm_content

    # CK kernel should have been downloaded
    ck_local = tmp_path / "ck" / "juice_sc_default_v01.bc"
    assert ck_local.exists()
    assert ck_local.read_bytes() == ck_content

    # FK kernel was already present — content unchanged
    assert existing_kernel.read_bytes() == b"pre-existing"


def test_download_kernels_latest_refreshes_cached_tm(tmp_path: Path):
    """For version='latest', the TM should be refreshed even when cached."""
    tm_remote = "/data/SPICE/JUICE/kernels/mk/juice_plan.tm"
    old_tm_content = textwrap.dedent(
        r"""
        KPL/MK
        \begindata
             KERNELS_TO_LOAD   = ()
        \begintext
        """
    ).encode()
    new_tm_content = textwrap.dedent(
        r"""
        KPL/MK
        \begindata
                         KERNELS_TO_LOAD   = ()
        \begintext
        """
    ).encode()

    local_tm = tmp_path / "mk" / "juice_plan.tm"
    local_tm.parent.mkdir(parents=True)
    local_tm.write_bytes(old_tm_content)

    mock_ftp = MagicMock()
    mock_ftp.nlst.return_value = [tm_remote]
    mock_ftp.retrbinary.side_effect = lambda cmd, cb: cb(new_tm_content)

    with patch("quick_spice_manager.ftp.ftplib.FTP", return_value=mock_ftp):
        resolved_tm = download_kernels_via_ftp("JUICE", "plan", tmp_path, version="latest")

    assert resolved_tm == local_tm
    assert local_tm.read_bytes() == new_tm_content
    assert mock_ftp.retrbinary.call_count == 1


def test_download_kernels_versioned_keeps_cached_tm(tmp_path: Path):
    """For pinned versions, an already-cached TM should not be re-fetched."""
    version = "v462_20260223_001"
    tm_name = f"juice_plan_{version}.tm"
    tm_remote = f"/data/SPICE/JUICE/kernels/mk/{tm_name}"
    tm_content = textwrap.dedent(
        r"""
        KPL/MK
        \begindata
             KERNELS_TO_LOAD   = ()
        \begintext
        """
    ).encode()

    local_tm = tmp_path / "mk" / tm_name
    local_tm.parent.mkdir(parents=True)
    local_tm.write_bytes(tm_content)

    mock_ftp = MagicMock()
    mock_ftp.nlst.return_value = [
        "/data/SPICE/JUICE/kernels/mk/juice_plan.tm",
        tm_remote,
    ]
    mock_ftp.retrbinary.side_effect = lambda cmd, cb: cb(tm_content)

    with patch("quick_spice_manager.ftp.ftplib.FTP", return_value=mock_ftp):
        resolved_tm = download_kernels_via_ftp(
            "JUICE", "plan", tmp_path, version=version
        )

    assert resolved_tm == local_tm
    assert local_tm.read_bytes() == tm_content
    assert mock_ftp.retrbinary.call_count == 0


# ---------------------------------------------------------------------------
# Atomic writes + cross-process per-file locking
# ---------------------------------------------------------------------------


def test_ftp_download_file_writes_full_content_atomically(tmp_path: Path):
    """A successful download leaves the destination with the full content and
    no stray temp (.part) files behind."""
    local = tmp_path / "kernel.bsp"
    mock_ftp = MagicMock()
    mock_ftp.retrbinary.side_effect = lambda cmd, cb: cb(b"kernel-bytes")

    _ftp_download_file(mock_ftp, "/remote/kernel.bsp", local)

    assert local.read_bytes() == b"kernel-bytes"
    assert list(tmp_path.glob("*.part")) == []


def test_ftp_download_file_atomic_no_partial_on_exception(tmp_path: Path):
    """If the transfer fails mid-write, no partial/destination file is left
    behind and the exception propagates."""
    local = tmp_path / "kernel.bsp"
    mock_ftp = MagicMock()

    def fail_mid_write(cmd, cb):
        cb(b"partial-bytes")
        raise OSError("connection reset")

    mock_ftp.retrbinary.side_effect = fail_mid_write

    with pytest.raises(OSError, match="connection reset"):
        _ftp_download_file(mock_ftp, "/remote/kernel.bsp", local)

    assert not local.exists()
    assert list(tmp_path.glob("*.part")) == []


def test_locked_download_skips_if_already_present(tmp_path: Path):
    """If the destination already exists, no network call is made."""
    local = tmp_path / "kernel.bsp"
    local.write_bytes(b"already-here")
    mock_ftp = MagicMock()

    downloaded = _locked_download(mock_ftp, "/remote/kernel.bsp", local)

    assert downloaded is False
    mock_ftp.retrbinary.assert_not_called()
    assert local.read_bytes() == b"already-here"


def test_locked_download_force_redownloads_even_if_present(tmp_path: Path):
    """force=True (used for version='latest' metakernels) always re-fetches."""
    local = tmp_path / "juice_plan.tm"
    local.write_bytes(b"old-content")
    mock_ftp = MagicMock()
    mock_ftp.retrbinary.side_effect = lambda cmd, cb: cb(b"new-content")

    downloaded = _locked_download(mock_ftp, "/remote/juice_plan.tm", local, force=True)

    assert downloaded is True
    assert local.read_bytes() == b"new-content"


def test_locked_download_releases_lock_after_call(tmp_path: Path):
    """The per-file lock is released once _locked_download returns."""
    local = tmp_path / "kernel.bsp"
    mock_ftp = MagicMock()
    mock_ftp.retrbinary.side_effect = lambda cmd, cb: cb(b"data")

    _locked_download(mock_ftp, "/remote/kernel.bsp", local)

    lock_path = local.parent / f"{local.name}.lock"
    lock = filelock.FileLock(str(lock_path), timeout=1)
    lock.acquire()  # would raise Timeout if the original lock were still held
    lock.release()


def test_locked_download_concurrent_same_file_downloads_once(tmp_path: Path):
    """Two threads racing to fetch the same missing file result in exactly
    one real download; the loser reuses the file the winner wrote."""
    local = tmp_path / "kernel.bsp"
    call_count = 0
    call_lock = threading.Lock()

    def fake_retrbinary(cmd, cb):
        nonlocal call_count
        with call_lock:
            call_count += 1
        time.sleep(0.2)  # widen the window so both threads overlap
        cb(b"kernel-bytes")

    results: list[bool] = []
    results_lock = threading.Lock()

    def worker():
        ftp = MagicMock()
        ftp.retrbinary.side_effect = fake_retrbinary
        downloaded = _locked_download(ftp, "/remote/kernel.bsp", local)
        with results_lock:
            results.append(downloaded)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert all(not t.is_alive() for t in threads)
    assert call_count == 1
    assert sorted(results) == [False, True]
    assert local.read_bytes() == b"kernel-bytes"


def test_download_one_kernel_routes_through_locked_download(tmp_path: Path):
    """_download_one_kernel skips the download if the file already exists,
    proving it goes through _locked_download rather than an unconditional
    _ftp_download_file call."""
    local = tmp_path / "kernel.bsp"
    local.write_bytes(b"already-here")
    mock_ftp = MagicMock()

    with patch("quick_spice_manager.ftp.ftplib.FTP", return_value=mock_ftp):
        _download_one_kernel("spiftp.esac.esa.int", "/remote/kernel.bsp", local)

    mock_ftp.retrbinary.assert_not_called()
    assert local.read_bytes() == b"already-here"


# ---------------------------------------------------------------------------
# Cross-process connection-count bounding
#
# Note: tests/conftest.py's autouse _isolate_ftp_connection_lock_dir fixture
# redirects get_ftp_connection_lock_dir() into a throwaway tmp dir for every
# test, so these never touch the real user cache directory or contend with
# a real concurrent process's connection slots.
# ---------------------------------------------------------------------------


def test_ftp_connection_slot_bounds_concurrent_connections():
    """No more than _MAX_CONCURRENT_FTP_CONNECTIONS slots can be held at
    once; extra contenders wait until one is released."""
    max_slots = ftp_module._MAX_CONCURRENT_FTP_CONNECTIONS
    release = threading.Event()
    concurrent_count = 0
    max_concurrent_seen = 0
    count_lock = threading.Lock()

    def hold_slot():
        nonlocal concurrent_count, max_concurrent_seen
        with ftp_module._ftp_connection_slot():
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


def test_ftp_connection_slot_releases_on_exception(monkeypatch):
    """A slot must be released even when the body raises, otherwise repeated
    failures would permanently exhaust every slot."""
    monkeypatch.setattr(ftp_module, "_CONNECTION_SLOT_TIMEOUT_SECONDS", 1)
    max_slots = ftp_module._MAX_CONCURRENT_FTP_CONNECTIONS

    for _ in range(max_slots + 3):
        with pytest.raises(RuntimeError, match="boom"):
            with ftp_module._ftp_connection_slot():
                raise RuntimeError("boom")


def test_download_one_kernel_uses_connection_slot(tmp_path: Path):
    """_download_one_kernel acquires a connection slot around its FTP call."""
    local = tmp_path / "kernel.bsp"
    mock_ftp = MagicMock()
    mock_ftp.retrbinary.side_effect = lambda cmd, cb: cb(b"data")

    with (
        patch.object(
            ftp_module, "_ftp_connection_slot",
            return_value=contextlib.nullcontext(),
        ) as mock_slot,
        patch("quick_spice_manager.ftp.ftplib.FTP", return_value=mock_ftp),
    ):
        _download_one_kernel("host", "/remote/kernel.bsp", local)

    mock_slot.assert_called_once()


def test_list_metakernels_uses_connection_slot():
    """list_metakernels_via_ftp acquires a connection slot around its FTP call."""
    mock_ftp = MagicMock()
    mock_ftp.nlst.return_value = ["/data/SPICE/JUICE/kernels/mk/juice_plan.tm"]

    with (
        patch.object(
            ftp_module, "_ftp_connection_slot",
            return_value=contextlib.nullcontext(),
        ) as mock_slot,
        patch("quick_spice_manager.ftp.ftplib.FTP", return_value=mock_ftp),
    ):
        list_metakernels_via_ftp("JUICE")

    mock_slot.assert_called_once()


def test_download_kernels_via_ftp_uses_connection_slot(tmp_path: Path):
    """download_kernels_via_ftp's main connection acquires a connection slot,
    in addition to the per-worker slots taken by _download_one_kernel."""
    tm_content = textwrap.dedent(
        r"""
        KPL/MK
        \begindata
             KERNELS_TO_LOAD   = ()
        \begintext
        """,
    ).encode()
    mock_ftp = MagicMock()
    mock_ftp.nlst.return_value = ["/data/SPICE/JUICE/kernels/mk/juice_plan.tm"]
    mock_ftp.retrbinary.side_effect = lambda cmd, cb: cb(tm_content)

    with (
        patch.object(
            ftp_module, "_ftp_connection_slot",
            return_value=contextlib.nullcontext(),
        ) as mock_slot,
        patch("quick_spice_manager.ftp.ftplib.FTP", return_value=mock_ftp),
    ):
        download_kernels_via_ftp("JUICE", "plan", tmp_path)

    mock_slot.assert_called_once()


# ---------------------------------------------------------------------------
# Offline / connectivity-failure fallback
#
# Note: tests/conftest.py's autouse fixture redirects
# get_metakernel_listing_cache_dir() into a fresh tmp dir per test, so the
# list_metakernels_via_ftp() cache tests below never touch the real user
# cache directory and never see stale data from other tests.
# ---------------------------------------------------------------------------

_EMPTY_TM = textwrap.dedent(
    r"""
    KPL/MK
    \begindata
         KERNELS_TO_LOAD   = ()
    \begintext
    """,
).encode()


def test_download_kernels_via_ftp_falls_back_to_cache_on_connection_error(
    tmp_path: Path,
):
    """A prior successful resolution is reused (with a warning, not an
    error) when a later call can't reach the FTP server at all."""
    tm_remote = "/data/SPICE/JUICE/kernels/mk/juice_plan.tm"
    mock_ftp = MagicMock()
    mock_ftp.nlst.return_value = [tm_remote]
    mock_ftp.retrbinary.side_effect = lambda cmd, cb: cb(_EMPTY_TM)

    with patch("quick_spice_manager.ftp.ftplib.FTP", return_value=mock_ftp):
        first = download_kernels_via_ftp("JUICE", "plan", tmp_path)

    with patch(
        "quick_spice_manager.ftp.ftplib.FTP",
        side_effect=OSError("Network is unreachable"),
    ):
        second = download_kernels_via_ftp("JUICE", "plan", tmp_path)

    assert second == first
    assert second.exists()


def test_download_kernels_via_ftp_raises_clear_error_when_offline_and_uncached(
    tmp_path: Path,
):
    """With no prior successful resolution to fall back to, a connectivity
    failure raises a clear, actionable ConnectionError."""
    with (
        patch(
            "quick_spice_manager.ftp.ftplib.FTP",
            side_effect=OSError("Network is unreachable"),
        ),
        pytest.raises(ConnectionError, match="Could not reach the ESA FTP server"),
    ):
        download_kernels_via_ftp("JUICE", "plan", tmp_path)


def test_download_kernels_via_ftp_pinned_version_skips_network_when_cached(
    tmp_path: Path,
):
    """Once a pinned (non-'latest') version is fully resolved and cached,
    later calls for the same (spacecraft, mk, version) never touch the
    network at all -- pinned versions never change once published."""
    version = "v462_20260223_001"
    tm_name = f"juice_plan_{version}.tm"
    tm_remote = f"/data/SPICE/JUICE/kernels/mk/{tm_name}"
    mock_ftp = MagicMock()
    mock_ftp.nlst.return_value = [
        "/data/SPICE/JUICE/kernels/mk/juice_plan.tm",
        tm_remote,
    ]
    mock_ftp.retrbinary.side_effect = lambda cmd, cb: cb(_EMPTY_TM)

    with patch("quick_spice_manager.ftp.ftplib.FTP", return_value=mock_ftp):
        first = download_kernels_via_ftp("JUICE", "plan", tmp_path, version=version)

    with patch("quick_spice_manager.ftp.ftplib.FTP") as mock_ftp_cls:
        second = download_kernels_via_ftp("JUICE", "plan", tmp_path, version=version)

    assert second == first
    mock_ftp_cls.assert_not_called()


def test_download_kernels_via_ftp_download_kernels_false_uses_cache(tmp_path: Path):
    """download_kernels=False resolves entirely from the local cache, never
    touching the network -- even for version='latest', which would
    otherwise always attempt a live check."""
    tm_remote = "/data/SPICE/JUICE/kernels/mk/juice_plan.tm"
    mock_ftp = MagicMock()
    mock_ftp.nlst.return_value = [tm_remote]
    mock_ftp.retrbinary.side_effect = lambda cmd, cb: cb(_EMPTY_TM)

    with patch("quick_spice_manager.ftp.ftplib.FTP", return_value=mock_ftp):
        first = download_kernels_via_ftp("JUICE", "plan", tmp_path)

    with patch("quick_spice_manager.ftp.ftplib.FTP") as mock_ftp_cls:
        second = download_kernels_via_ftp(
            "JUICE", "plan", tmp_path, download_kernels=False,
        )

    assert second == first
    mock_ftp_cls.assert_not_called()


def test_download_kernels_via_ftp_download_kernels_false_raises_when_uncached(
    tmp_path: Path,
):
    """With nothing cached, download_kernels=False raises immediately
    instead of falling back to the network."""
    with patch("quick_spice_manager.ftp.ftplib.FTP") as mock_ftp_cls:
        with pytest.raises(FileNotFoundError, match="download_kernels=False"):
            download_kernels_via_ftp(
                "JUICE", "plan", tmp_path, download_kernels=False,
            )

    mock_ftp_cls.assert_not_called()


def test_download_kernels_via_ftp_unresolvable_mk_is_not_treated_as_offline(
    tmp_path: Path,
):
    """A legitimate 'no such metakernel' answer from a server that responded
    just fine must propagate as FileNotFoundError, not be swallowed into a
    ConnectionError or offline fallback."""
    mock_ftp = MagicMock()
    mock_ftp.nlst.return_value = ["/data/SPICE/JUICE/kernels/mk/juice_other.tm"]

    with (
        patch("quick_spice_manager.ftp.ftplib.FTP", return_value=mock_ftp),
        pytest.raises(FileNotFoundError, match="Cannot find a metakernel"),
    ):
        download_kernels_via_ftp("JUICE", "totally_bogus_shortcut", tmp_path)


def test_download_kernels_via_ftp_incomplete_cache_reports_missing_kernels(
    tmp_path: Path,
):
    """If a cached resolution's .tm file is missing some of its referenced
    kernels (e.g. a prior download was interrupted) and the server can't be
    reached, the resulting error names the missing files."""
    tm_remote = "/data/SPICE/JUICE/kernels/mk/juice_plan.tm"
    tm_with_kernel = textwrap.dedent(
        r"""
        KPL/MK
        \begindata
             PATH_VALUES       = ( '..' )
             PATH_SYMBOLS      = ( 'KERNELS' )
             KERNELS_TO_LOAD   = (
                                   '$KERNELS/fk/juice_v45.tf'
                                 )
        \begintext
        """,
    ).encode()

    mock_ftp = MagicMock()
    mock_ftp.nlst.return_value = [tm_remote]

    def fake_retrbinary(cmd, cb):
        remote = cmd.split(" ", 1)[1]
        if remote == tm_remote:
            cb(tm_with_kernel)
        else:
            raise OSError("Network is unreachable")  # kernel download drops

    mock_ftp.retrbinary.side_effect = fake_retrbinary

    with patch("quick_spice_manager.ftp.ftplib.FTP", return_value=mock_ftp):
        # The .tm downloads fine but its referenced kernel fails -- the
        # existing partial-failure-tolerant behavior still "succeeds"
        # overall and records a resolution cache entry.
        download_kernels_via_ftp("JUICE", "plan", tmp_path)

    assert not (tmp_path / "fk" / "juice_v45.tf").exists()

    with (
        patch(
            "quick_spice_manager.ftp.ftplib.FTP",
            side_effect=OSError("Network is unreachable"),
        ),
        pytest.raises(ConnectionError, match="incomplete"),
    ):
        download_kernels_via_ftp("JUICE", "plan", tmp_path)


def test_list_metakernels_via_ftp_falls_back_to_cached_listing():
    """A prior successful listing is reused (with a warning) when a later
    call can't reach the FTP server."""
    mock_ftp = MagicMock()
    mock_ftp.nlst.return_value = [
        "/data/SPICE/JUICE/kernels/mk/juice_plan.tm",
        "/data/SPICE/JUICE/kernels/mk/juice_ops.tm",
    ]

    with patch("quick_spice_manager.ftp.ftplib.FTP", return_value=mock_ftp):
        first = list_metakernels_via_ftp("JUICE")

    with patch(
        "quick_spice_manager.ftp.ftplib.FTP",
        side_effect=OSError("Network is unreachable"),
    ):
        second = list_metakernels_via_ftp("JUICE")

    assert second == first


def test_list_metakernels_via_ftp_raises_when_offline_and_uncached():
    """With nothing cached, a connectivity failure raises a clear error."""
    with (
        patch(
            "quick_spice_manager.ftp.ftplib.FTP",
            side_effect=OSError("Network is unreachable"),
        ),
        pytest.raises(ConnectionError, match="Could not reach the ESA FTP server"),
    ):
        list_metakernels_via_ftp("JUICE")


# ---------------------------------------------------------------------------
# SpiceManager.tour_config FTP fallback integration
# ---------------------------------------------------------------------------


def test_spice_manager_tour_config_ftp(tmp_path: Path):
    """
    SpiceManager.tour_config downloads kernels via FTP and passes the local
    .tm path to TourConfig with download_kernels=False.
    """
    pytest.importorskip("planetary_coverage")
    from unittest.mock import patch as _patch

    from quick_spice_manager import SpiceManager

    # Minimal TM content (no kernels to download)
    tm_content = textwrap.dedent(
        r"""
        KPL/MK
        \begindata
             PATH_VALUES       = ( '..' )
             PATH_SYMBOLS      = ( 'KERNELS' )
             KERNELS_TO_LOAD   = ()
        \begintext
        """
    ).encode()

    local_tm = tmp_path / "mk" / "juice_plan.tm"
    local_tm.parent.mkdir(parents=True)
    local_tm.write_bytes(tm_content)

    mock_ftp = MagicMock()
    mock_ftp.nlst.return_value = [
        f"/data/SPICE/JUICE/kernels/mk/{local_tm.name}"
    ]
    mock_ftp.retrbinary.side_effect = lambda cmd, cb: cb(tm_content)

    fake_tour = MagicMock()

    with (
        _patch(
            "planetary_coverage.TourConfig",  # lazy import inside get_tour_config
            return_value=fake_tour,
        ) as mock_tc,
        _patch(
            "quick_spice_manager.ftp.ftplib.FTP",
            return_value=mock_ftp,
        ),
    ):
        man = SpiceManager(
            kernels_dir=tmp_path,
            download_kernels=True,
            mk="plan",
        )
        man._mk = "plan"
        man._kernels_dir = tmp_path

        result = man.tour_config

    assert result is fake_tour
    # TourConfig must be called exactly once with a local .tm path
    mock_tc.assert_called_once()
    call_kwargs = mock_tc.call_args[1]
    assert call_kwargs["mk"].endswith(".tm")
    assert call_kwargs["download_kernels"] is False



def test_mk_lists():
    from quick_spice_manager.ftp import list_metakernels_via_ftp
    assert list_metakernels_via_ftp('rosetta') == ['ROS_OPS', 'ROS_OPS_V350_20220906_001']
    assert list_metakernels_via_ftp('ROSETTA') == ['ROS_OPS', 'ROS_OPS_V350_20220906_001']