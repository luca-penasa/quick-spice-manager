"""
Tests for the FTP module (ftp.py).
All network I/O is mocked — no real FTP connection is made.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from quick_spice_manager.ftp import (
    _MISSION_FTP_BASE,
    _canonical_mission,
    _ftp_base,
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


def test_resolve_tm_versioned_fallback_to_unversioned():
    """When version tag not found anywhere, falls back to unversioned TM."""
    ftp = MagicMock()
    ftp.nlst.side_effect = [
        ["/data/SPICE/JUICE/kernels/mk/juice_plan.tm"],  # mk/
        [],  # former_versions/ — nothing for this version
    ]
    path = _resolve_tm_on_ftp(
        ftp, "JUICE", "plan", "/data/SPICE/JUICE/", version="v999_99999999_001"
    )
    assert path == "/data/SPICE/JUICE/kernels/mk/juice_plan.tm"


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


# ---------------------------------------------------------------------------
# SpiceManager.tour_config FTP fallback integration
# ---------------------------------------------------------------------------


def test_spice_manager_tour_config_ftp(tmp_path: Path):
    """
    SpiceManager.tour_config downloads kernels via FTP and passes the local
    .tm path to TourConfig with download_kernels=False.
    """
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
            "quick_spice_manager.spice_manager.TourConfig",
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
