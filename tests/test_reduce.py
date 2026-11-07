"""Tests for :func:`quick_spice_manager.reduce_kernels`, on real CSPICE.

A small dataset (LSK, SCLK, FK, SPK, CK, binary PCK) is written in the test,
reduced, and the reduced set compared with the full one.
"""

from __future__ import annotations

import gc
import os
from pathlib import Path

import numpy as np
import pytest
import spiceypy
from click.testing import CliRunner
from spiceypy.utils.exceptions import SpiceyError

from quick_spice_manager import reduce_kernels
from quick_spice_manager import spice_manager as spice_manager_module
from quick_spice_manager.cli import cli

DAY = 86400.0
T0 = 946_728_000.0  # 2030-01-01 about noon TDB
BODY, CENTER = -999, 399
INST = -999000  # its SCLK is -999 (CK_-999000_SCLK is not needed)
FRAME_ID = 1_999_999

LSK = r"""
KPL/LSK
\begindata
DELTET/DELTA_T_A = 32.184
DELTET/K         = 1.657D-3
DELTET/EB        = 1.671D-2
DELTET/M         = ( 6.239996D0 1.99096871D-7 )
DELTET/DELTA_AT  = ( 37, @2017-JAN-1 )
\begintext
"""

SCLK = r"""
KPL/SCLK
\begindata
SCLK_KERNEL_ID           = ( @2000-01-01 )
SCLK_DATA_TYPE_999       = ( 1 )
SCLK01_TIME_SYSTEM_999   = ( 1 )
SCLK01_N_FIELDS_999      = ( 2 )
SCLK01_MODULI_999        = ( 4294967296 65536 )
SCLK01_OFFSETS_999       = ( 0 0 )
SCLK01_OUTPUT_DELIM_999  = ( 1 )
SCLK_PARTITION_START_999 = ( 0.0 )
SCLK_PARTITION_END_999   = ( 2.81474976710655D14 )
SCLK01_COEFFICIENTS_999  = ( 0.0 0.0 1.0 )
\begintext
"""

FK = rf"""
KPL/FK
\begindata
FRAME_TEST_FIXED          = {FRAME_ID}
FRAME_{FRAME_ID}_NAME     = 'TEST_FIXED'
FRAME_{FRAME_ID}_CLASS    = 2
FRAME_{FRAME_ID}_CLASS_ID = {FRAME_ID}
FRAME_{FRAME_ID}_CENTER   = {CENTER}
\begintext
"""

KERNELS = [
    "lsk/test.tls",
    "sclk/test.tsc",
    "fk/test.tf",
    "ik/test.ti",
    "spk/test.bsp",
    "spk/elsewhere.bsp",
    "ck/test.bc",
    "pck/test.bpc",
]


def _states(ets: np.ndarray) -> np.ndarray:
    """A smooth orbit-like trajectory, km and km/s."""
    w = 2 * np.pi / (3 * DAY)
    x = 7000 * np.cos(w * (ets - T0))
    y = 7000 * np.sin(w * (ets - T0))
    return np.column_stack(
        [
            x,
            y,
            0 * x,
            -7000 * w * np.sin(w * (ets - T0)),
            7000 * w * np.cos(w * (ets - T0)),
            0 * x,
        ],
    )


def _write_spk(path: Path, start: float, stop: float, comment: str) -> None:
    ets = np.arange(start, stop + 1, 600.0)
    handle = spiceypy.spkopn(str(path), "test", 1000)
    spiceypy.dafac(handle, [comment])
    spiceypy.spkw13(
        handle,
        BODY,
        CENTER,
        "J2000",
        ets[0],
        ets[-1],
        "test",
        7,
        len(ets),
        _states(ets),
        ets,
    )
    spiceypy.spkcls(handle)


def _write_ck(path: Path) -> None:
    """Pointing every 60 s for 30 days, with a one-hour gap after day 15."""
    gap_start, gap_stop = T0 + 15 * DAY, T0 + 15 * DAY + 3600
    sclk = np.array(
        [
            spiceypy.sce2c(-999, t)
            for t in np.arange(T0, T0 + 30 * DAY + 1, 60.0)
            if not gap_start < t < gap_stop
        ],
    )
    angles = 1e-4 * (sclk - sclk[0]) / 65536.0
    quats = np.array([spiceypy.m2q(spiceypy.rotate(a, 3)) for a in angles])
    avvs = np.tile([0.0, 0.0, 1e-4], (len(sclk), 1))
    starts = np.array(
        [sclk[0], sclk[np.searchsorted(sclk, spiceypy.sce2c(-999, gap_stop))]],
    )
    handle = spiceypy.ckopn(str(path), "test", 1000)
    spiceypy.ckw03(
        handle,
        sclk[0],
        sclk[-1],
        INST,
        "J2000",
        True,
        "test",
        len(sclk),
        sclk,
        quats,
        avvs,
        len(starts),
        starts,
    )
    spiceypy.ckcls(handle)


def _write_pck(path: Path) -> None:
    n, degree, intlen = 30, 3, DAY
    rng = np.random.default_rng(0)
    coeffs = rng.normal(scale=1e-3, size=(n, 3, degree + 1))
    coeffs[:, :, 0] += [0.1, 0.2, 0.3]
    handle = spiceypy.pckopn(str(path), "test", 1000)
    spiceypy.pckw02(
        handle,
        FRAME_ID,
        "J2000",
        T0,
        T0 + n * intlen,
        "test",
        intlen,
        n,
        degree,
        coeffs.ravel(),
        T0,
    )
    spiceypy.pckcls(handle)


@pytest.fixture(autouse=True)
def real_pool(monkeypatch, tmp_path):
    monkeypatch.delenv("SPICE_DIRECTORY", raising=False)
    monkeypatch.delenv("SPICE_METAKERNEL", raising=False)
    monkeypatch.setattr(spice_manager_module, "load_dotenv", lambda: False)
    monkeypatch.setattr(
        spice_manager_module,
        "get_user_kernels_cache_directory",
        lambda: tmp_path / "cache",
    )
    spiceypy.kclear()
    yield
    gc.collect()
    spiceypy.kclear()


@pytest.fixture
def dataset(tmp_path) -> Path:
    """``<tmp>/full/mk/test.tm`` and its kernels (30 days from T0)."""
    root = tmp_path / "full"
    for rel in KERNELS:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / "lsk/test.tls").write_text(LSK, encoding="ascii")
    (root / "sclk/test.tsc").write_text(SCLK, encoding="ascii")
    (root / "fk/test.tf").write_text(FK, encoding="ascii")
    (root / "ik/test.ti").write_text(
        "KPL/IK\n\\begindata\nINS-999000_X = 1\n\\begintext\n",
        encoding="ascii",
    )
    spiceypy.furnsh(str(root / "lsk/test.tls"))
    spiceypy.furnsh(str(root / "sclk/test.tsc"))
    _write_spk(root / "spk/test.bsp", T0, T0 + 30 * DAY, "The source comment.")
    _write_spk(root / "spk/elsewhere.bsp", T0 + 100 * DAY, T0 + 110 * DAY, "Elsewhere.")
    _write_ck(root / "ck/test.bc")
    _write_pck(root / "pck/test.bpc")
    spiceypy.kclear()
    entries = "\n".join(f"    '$KERNELS/{rel}'" for rel in KERNELS)
    (root / "mk").mkdir()
    (root / "mk/test.tm").write_text(
        "KPL/MK\n\\begindata\nPATH_VALUES = ( '..' )\nPATH_SYMBOLS = ( 'KERNELS' )\n"
        f"KERNELS_TO_LOAD = (\n{entries}\n)\nSKD_VERSION = 'v001'\n\\begintext\n",
        encoding="ascii",
    )
    return root / "mk/test.tm"


def _furnsh_mk(mk: Path) -> None:
    """Load a metakernel with PATH_VALUES = '..' (relative to the working directory)."""
    cwd = Path.cwd()
    os.chdir(mk.parent)
    try:
        spiceypy.furnsh(mk.name)
    finally:
        os.chdir(cwd)


def _sample(ets: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    states = np.array(
        [spiceypy.spkezr(str(BODY), t, "J2000", "NONE", str(CENTER))[0] for t in ets],
    )
    pointing = np.array(
        [spiceypy.ckgpav(INST, spiceypy.sce2c(-999, t), 0, "J2000")[0] for t in ets],
    )
    pck = np.array([spiceypy.sxform("J2000", "TEST_FIXED", t) for t in ets])
    return states, pointing, pck


def _window(day0: float, day1: float) -> tuple[str, str]:
    return tuple(spiceypy.et2utc(T0 + d * DAY, "ISOC", 3) for d in (day0, day1))


def _reduce(dataset: Path, out: Path, windows, **kwargs):
    _furnsh_mk(dataset)  # str2et for the window strings, in the test
    utc = [_window(*w) for w in windows]
    spiceypy.kclear()
    return reduce_kernels(dataset, utc, out, **kwargs)


def test_reduced_set_gives_the_same_answers_inside_the_window(dataset, tmp_path):
    result = _reduce(dataset, tmp_path / "red", [(10, 12)], pad=0)

    ets = np.random.default_rng(1).uniform(T0 + 10 * DAY + 1, T0 + 12 * DAY - 1, 300)
    _furnsh_mk(dataset)
    full = _sample(ets)
    spiceypy.kclear()
    _furnsh_mk(result.metakernel)
    reduced = _sample(ets)

    np.testing.assert_array_equal(reduced[0], full[0])  # SPK: identical
    np.testing.assert_array_equal(reduced[1], full[1])  # CK: identical
    np.testing.assert_allclose(
        reduced[2],
        full[2],
        rtol=0,
        atol=1e-12,
    )  # PCK: to rounding
    assert result.size_out < result.size_in / 5


def test_outside_the_windows_there_is_no_data(dataset, tmp_path):
    result = _reduce(dataset, tmp_path / "red", [(10, 12)], pad=0)
    _furnsh_mk(result.metakernel)

    with pytest.raises(SpiceyError, match="SPKINSUFFDATA"):
        spiceypy.spkezr(str(BODY), T0 + 20 * DAY, "J2000", "NONE", str(CENTER))
    with pytest.raises(SpiceyError):
        spiceypy.ckgpav(INST, spiceypy.sce2c(-999, T0 + 20 * DAY), 0, "J2000")


def test_ck_gaps_are_kept(dataset, tmp_path):
    """A window over the CK's gap: still no pointing inside the gap."""
    result = _reduce(dataset, tmp_path / "red", [(14.9, 15.2)], pad=0)
    _furnsh_mk(result.metakernel)
    in_gap = spiceypy.sce2c(-999, T0 + 15 * DAY + 1800)

    with pytest.raises(SpiceyError):
        spiceypy.ckgpav(INST, in_gap, 0, "J2000")
    spiceypy.ckgpav(INST, spiceypy.sce2c(-999, T0 + 15.1 * DAY), 0, "J2000")


def test_report_drop_no_data_and_metakernel(dataset, tmp_path):
    result = _reduce(dataset, tmp_path / "red", [(10, 12)], drop=["ik/*"], name="small")

    actions = {k.relative: k.action for k in result.kernels}
    assert actions == {
        "lsk/test.tls": "copied",
        "sclk/test.tsc": "copied",
        "fk/test.tf": "copied",
        "ik/test.ti": "dropped",
        "spk/test.bsp": "subset",
        "spk/elsewhere.bsp": "no data",
        "ck/test.bc": "subset",
        "pck/test.bpc": "subset",
    }
    assert result.metakernel == tmp_path / "red/mk/small.tm"
    text = result.metakernel.read_text(encoding="ascii")
    assert "'$KERNELS/spk/test.bsp'" in text
    assert (
        "elsewhere" not in text.split("\\begindata")[1]
    )  # left out, named in the comments
    assert "ik/test.ti (dropped)" in text
    assert "SKD_VERSION = 'v001'" in text
    assert not (tmp_path / "red/spk/elsewhere.bsp").exists()
    assert "subset" in result.summary()


def test_disjoint_windows_make_one_segment_each_and_overlapping_ones_merge(
    dataset,
    tmp_path,
):
    two = _reduce(dataset, tmp_path / "two", [(2, 3), (20, 21)], pad=0)
    merged = _reduce(dataset, tmp_path / "one", [(2, 3), (2.5, 4)], pad=0)

    assert len(two.windows_et) == 2
    assert len(merged.windows_et) == 1
    spk = {k.relative: k.note for k in two.kernels}["spk/test.bsp"]
    assert spk == "2 segments"


def test_padding_widens_the_windows(dataset, tmp_path):
    result = _reduce(dataset, tmp_path / "red", [(10, 12)])
    ((lo, hi),) = result.windows_et
    assert lo == pytest.approx(T0 + 9 * DAY, abs=1e-3)
    assert hi == pytest.approx(T0 + 13 * DAY, abs=1e-3)


def test_comments_carry_provenance_and_the_source_comments(dataset, tmp_path):
    result = _reduce(dataset, tmp_path / "red", [(10, 12)])
    handle = spiceypy.dafopr(str(tmp_path / "red/spk/test.bsp"))
    try:
        _n, lines, _done = spiceypy.dafec(handle, 50, 200)
    finally:
        spiceypy.dafcls(handle)
    text = "\n".join(lines)
    assert "Reduced by quick-spice-manager" in text
    assert "from test.bsp" in text
    assert "The source comment." in text
    assert result.metakernel.exists()


def test_the_pool_is_left_as_it_was(dataset, tmp_path):
    _reduce(dataset, tmp_path / "red", [(10, 12)])
    assert spiceypy.ktotal("ALL") == 0


def test_windows_are_validated(dataset, tmp_path):
    with pytest.raises(ValueError, match="no time window"):
        reduce_kernels(dataset, [], tmp_path / "red")
    with pytest.raises(ValueError, match="ends before it starts"):
        reduce_kernels(dataset, [(T0 + DAY, T0)], tmp_path / "red")


def test_ephemeris_times_are_accepted(dataset, tmp_path):
    result = reduce_kernels(
        dataset,
        [(T0 + 10 * DAY, T0 + 12 * DAY)],
        tmp_path / "red",
        pad=0,
    )
    assert result.windows_et == [(T0 + 10 * DAY, T0 + 12 * DAY)]


def test_cli_reduce(dataset, tmp_path):
    out = tmp_path / "red"
    run = CliRunner().invoke(
        cli,
        [
            "reduce",
            str(dataset),
            "-w",
            "2030-01-11T12:00",
            "2030-01-13T12:00",
            "-o",
            str(out),
            "--drop",
            "ik/*",
        ],
    )
    assert run.exit_code == 0, run.output
    assert (out / "mk/test_reduced.tm").exists()
    assert "dropped" in run.output


def test_ck_without_its_sclk_is_copied_whole(dataset, tmp_path):
    """Like juice_swi_scan_zero_v02.bc: no SCLK for the instrument, so the
    windows can't be converted to its ticks; the kernel is kept as it is."""
    root = dataset.parent.parent
    handle = spiceypy.ckopn(str(root / "ck/nosclk.bc"), "test", 0)
    ticks = np.array([1.0e13, 2.0e13])
    spiceypy.ckw03(
        handle,
        ticks[0],
        ticks[1],
        -888000,
        "J2000",
        False,
        "nosclk",
        2,
        ticks,
        np.tile([1.0, 0, 0, 0], (2, 1)),
        np.zeros((2, 3)),
        1,
        ticks[:1],
    )
    spiceypy.ckcls(handle)
    text = dataset.read_text(encoding="ascii").replace(
        "'$KERNELS/ck/test.bc'",
        "'$KERNELS/ck/test.bc'\n    '$KERNELS/ck/nosclk.bc'",
    )
    dataset.write_text(text, encoding="ascii")

    result = _reduce(dataset, tmp_path / "red", [(10, 12)])

    report = {k.relative: k for k in result.kernels}["ck/nosclk.bc"]
    assert report.action == "copied"
    assert "no SCLK -888" in report.note
    assert report.size_out == report.size_in
