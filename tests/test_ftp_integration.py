"""
Integration tests for FTP kernel downloads.

These tests make real network requests to spiftp.esac.esa.int and are
skipped by default.  Run with::

    pytest --integration tests/test_ftp_integration.py

Strategy
--------
Rather than downloading an entire metakernel (which can be hundreds of MB),
each test patches ``_parse_mk_kernel_paths`` to return only a tiny subset of
known-small kernel files.  The TM file itself is always fetched for real so
the resolution logic is exercised end-to-end.

Small reference files from ``juice_s011_tr03_v461_20260121_001.tm``
(confirmed on 2026-03-10):

    lsk/naif0012.tls        ~5 KB   (standard NAIF leapseconds kernel)
    pck/de-403-masses.tpc   ~2 KB   (planetary constants)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from quick_spice_manager import SpiceManager
from quick_spice_manager.ftp_fallback import download_kernels_via_ftp

# Two tiny kernels known to be in the TR03/v461 metakernel.
_SMALL_KERNELS = [
    "lsk/naif0012.tls",
    "pck/de-403-masses.tpc",
]

# The version that lives only in former_versions/ (not on Bitbucket).
_VERSION = "v461_20260121_001"
_MK = "tr03"
_SPACECRAFT = "JUICE"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _patch_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restrict kernel list to the two tiny test files."""
    monkeypatch.setattr(
        "quick_spice_manager.ftp_fallback._parse_mk_kernel_paths",
        lambda _content: _SMALL_KERNELS,
    )


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_download_kernels_via_ftp_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    ``download_kernels_via_ftp`` downloads the versioned TM from
    ``former_versions/`` and the two small kernel files.

    Verifies
    --------
    * The returned path points to the correct ``.tm`` file.
    * The ``.tm`` file is non-empty and contains the expected mission text.
    * Each small kernel file exists and is non-empty.
    """
    _patch_parse(monkeypatch)

    local_tm = download_kernels_via_ftp(
        spacecraft=_SPACECRAFT,
        mk=_MK,
        kernels_dir=tmp_path,
        version=_VERSION,
    )

    # --- TM file ----------------------------------------------------------
    expected_tm = tmp_path / "mk" / f"juice_s011_tr03_{_VERSION}.tm"
    assert local_tm == expected_tm, f"Unexpected TM path: {local_tm}"
    assert local_tm.exists(), "TM file was not downloaded"
    tm_text = local_tm.read_text(encoding="utf-8", errors="replace")
    assert len(tm_text) > 100, "TM file appears empty"
    assert "KPL/MK" in tm_text, "TM file does not look like a SPICE metakernel"

    # --- Kernel files -----------------------------------------------------
    for rel in _SMALL_KERNELS:
        kernel_path = tmp_path / rel
        assert kernel_path.exists(), f"Kernel '{rel}' was not downloaded"
        assert kernel_path.stat().st_size > 0, f"Kernel '{rel}' is empty"


@pytest.mark.integration
def test_download_kernels_caching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A second call with the same ``kernels_dir`` must skip already-present files
    and not raise any error.
    """
    _patch_parse(monkeypatch)

    kwargs = dict(spacecraft=_SPACECRAFT, mk=_MK, kernels_dir=tmp_path, version=_VERSION)

    # First download: everything fetched fresh.
    local_tm = download_kernels_via_ftp(**kwargs)
    mtimes_after_first = {
        rel: (tmp_path / rel).stat().st_mtime for rel in _SMALL_KERNELS
    }

    # Second download: files already cached, mtimes must be unchanged.
    download_kernels_via_ftp(**kwargs)
    for rel in _SMALL_KERNELS:
        assert (tmp_path / rel).stat().st_mtime == mtimes_after_first[rel], (
            f"'{rel}' was re-downloaded on second call (caching broken)"
        )

    assert local_tm.exists()


@pytest.mark.integration
def test_spice_manager_ftp_fallback_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    End-to-end: ``SpiceManager.tour_config`` triggers the FTP fallback when
    planetary_coverage raises ``FileNotFoundError`` for a version that only
    exists on FTP (``former_versions/``), downloads the real TM and two small
    kernels, and calls ``TourConfig`` with the local ``.tm`` path.

    The second ``TourConfig`` call is mocked with a ``MagicMock`` so we don't
    need all mission kernels present — the focus is the FTP download and the
    correct arguments forwarded to ``TourConfig``.
    """
    from unittest.mock import MagicMock, patch as _patch

    _patch_parse(monkeypatch)

    man = SpiceManager(
        mk=_MK,
        version=_VERSION,
        kernels_dir=tmp_path,
        download_kernels=True,
    )

    fake_tour = MagicMock()

    with _patch(
        "quick_spice_manager.spice_manager.TourConfig",
        side_effect=[
            # First call: Bitbucket 404 (version only on FTP)
            FileNotFoundError(f"`juice_tr03.tm` at `{_VERSION}` does not exist."),
            # Second call: return a mock (all kernels would be needed for real)
            fake_tour,
        ],
    ) as mock_tc:
        tour = man.tour_config

    # FTP download verification
    local_tm = tmp_path / "mk" / f"juice_s011_tr03_{_VERSION}.tm"
    assert local_tm.exists(), "TM file was not written to kernels_dir"
    assert local_tm.stat().st_size > 0, "TM file is empty"

    for rel in _SMALL_KERNELS:
        assert (tmp_path / rel).exists(), f"Kernel '{rel}' missing after fallback"
        assert (tmp_path / rel).stat().st_size > 0, f"Kernel '{rel}' is empty"

    # Second TourConfig call should use the local .tm path and no downloading
    assert mock_tc.call_count == 2
    second_kwargs = mock_tc.call_args_list[1][1]
    assert second_kwargs["mk"].endswith(".tm"), "Second call must pass local .tm path"
    assert second_kwargs["download_kernels"] is False

    assert tour is fake_tour
