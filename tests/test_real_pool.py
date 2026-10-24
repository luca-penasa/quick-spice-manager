"""
Tests against the real CSPICE kernel pool (no spiceypy mocks).

The unit tests in ``test_spice_manager.py`` mock ``spiceypy``, so they can't
catch metakernels that ``furnsh`` rejects or misreads (80-character string
values, 132-character lines, relative PATH_VALUES). These tests build tiny
text-kernel datasets on disk and furnish them for real.
"""

from __future__ import annotations

import gc
import textwrap
from pathlib import Path

import pytest
import spiceypy

from quick_spice_manager import spice_manager as spice_manager_module
from quick_spice_manager.spice_manager import QuickSpiceManager

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def real_pool(monkeypatch, tmp_path):
    """Empty pool, no env overrides, and a throwaway user kernels cache.

    Yields the cache root (``<cache>/<spacecraft>`` is what managers use).
    """
    monkeypatch.delenv("SPICE_DIRECTORY", raising=False)
    monkeypatch.delenv("SPICE_METAKERNEL", raising=False)
    monkeypatch.setattr(spice_manager_module, "load_dotenv", lambda: False)
    cache_root = tmp_path / "cache"
    monkeypatch.setattr(
        spice_manager_module,
        "get_user_kernels_cache_directory",
        lambda: cache_root,
    )
    monkeypatch.setattr(spice_manager_module, "_active_manager_ref", None)
    spiceypy.kclear()
    yield cache_root
    spiceypy.kclear()


def _text_kernel(path: Path, value: int) -> Path:
    """A text kernel defining ``QSM_TEST_<first 8 chars of stem>`` = *value*
    (SPICE variable names are limited to 32 characters)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"KPL/PCK\n\\begindata\nQSM_TEST_{path.stem.upper()[:8]} = ( {value} )\n"
        "\\begintext\n",
        encoding="utf-8",
    )
    return path


_REL_KERNELS = ("pck/qa.tpc", "fk/qb.tf")


def _make_dataset(
    kernels_root: Path,
    value: int,
    *,
    mk_name: str = "test.tm",
    path_values: str = "..",
    extra_data: str = "",
) -> Path:
    """ESA-style layout: ``<kernels_root>/mk/<mk_name>`` with
    ``PATH_VALUES = ( '..' )`` and two text kernels defining
    ``QSM_TEST_QA`` / ``QSM_TEST_QB`` = *value*."""
    for rel in _REL_KERNELS:
        _text_kernel(kernels_root / rel, value)
    mk = kernels_root / "mk" / mk_name
    mk.parent.mkdir(parents=True, exist_ok=True)
    ktl = "\n".join(f"        '$KERNELS/{rel}'" for rel in _REL_KERNELS)
    mk.write_text(
        textwrap.dedent("""\
            KPL/MK

            \\begindata
                PATH_VALUES = ( '{pv}' )
                PATH_SYMBOLS = ( 'KERNELS' )
                KERNELS_TO_LOAD = (
            {ktl}
                )
            {extra}
            \\begintext
        """).format(pv=path_values, ktl=ktl, extra=extra_data),
        encoding="utf-8",
    )
    return mk


def _pool_kernels() -> list[str]:
    """Non-META pool entries, in load order."""
    out = []
    for i in range(spiceypy.ktotal("ALL")):
        fname, ftype, _src, _h = spiceypy.kdata(i, "ALL")
        if ftype.strip().upper() != "META":
            out.append(fname)
    return out


def _pool_meta() -> list[str]:
    return [
        spiceypy.kdata(i, "ALL")[0]
        for i in range(spiceypy.ktotal("ALL"))
        if spiceypy.kdata(i, "ALL")[1].strip().upper() == "META"
    ]


def _value(name: str) -> int:
    return int(spiceypy.gdpool(f"QSM_TEST_{name}", 0, 1)[0])


def _long_dir(base: Path, min_len: int) -> Path:
    """A directory under *base* whose absolute path is at least *min_len*
    characters, built from short-ish components."""
    d = base
    i = 0
    while len(str(d)) < min_len:
        d = d / f"long_component_{i:02d}"
        i += 1
    return d


# ---------------------------------------------------------------------------
# Behaviour that must not change (characterization)
# ---------------------------------------------------------------------------


def test_explicit_kernels_dir_wins_for_local_metakernel(tmp_path):
    mk = _make_dataset(tmp_path / "ds" / "kernels", value=1)
    other = tmp_path / "other"
    for rel in _REL_KERNELS:
        _text_kernel(other / rel, 7)

    QuickSpiceManager(mk=str(mk), kernels_dir=other).load_kernels()

    assert _value("QA") == 7
    assert all(k.startswith(str(other)) for k in _pool_kernels())


def test_spice_directory_env_wins_for_local_metakernel(tmp_path, monkeypatch):
    mk = _make_dataset(tmp_path / "ds" / "kernels", value=1)
    other = tmp_path / "other"
    for rel in _REL_KERNELS:
        _text_kernel(other / rel, 8)
    monkeypatch.setenv("SPICE_DIRECTORY", str(other))

    QuickSpiceManager(mk=str(mk)).load_kernels()

    assert _value("QA") == 8


def test_spice_directory_env_overrides_explicit_kernels_dir(tmp_path, monkeypatch):
    """Existing precedence: the env var beats the constructor argument."""
    mk = _make_dataset(tmp_path / "ds" / "kernels", value=1)
    explicit = tmp_path / "explicit"
    env = tmp_path / "env"
    for rel in _REL_KERNELS:
        _text_kernel(explicit / rel, 5)
        _text_kernel(env / rel, 6)
    monkeypatch.setenv("SPICE_DIRECTORY", str(env))

    QuickSpiceManager(mk=str(mk), kernels_dir=explicit).load_kernels()

    assert _value("QA") == 6


def test_exclusive_load_replaces_pool_and_non_exclusive_keeps_it(tmp_path):
    mk1 = _make_dataset(tmp_path / "a" / "kernels", value=1)
    mk2 = _make_dataset(tmp_path / "b" / "kernels", value=2)

    sm1 = QuickSpiceManager(mk=str(mk1), kernels_dir=mk1.parent.parent)
    sm1.load_kernels()
    sm2 = QuickSpiceManager(mk=str(mk2), kernels_dir=mk2.parent.parent)
    sm2.load_kernels()
    assert len(_pool_kernels()) == 2  # exclusive (default): only sm2's

    sm3 = QuickSpiceManager(
        mk=str(mk1), kernels_dir=mk1.parent.parent, exclusive=False,
    )
    sm3.load_kernels()
    assert len(_pool_kernels()) == 4  # sm2's + sm3's


def test_context_manager_restores_pool(tmp_path):
    mk1 = _make_dataset(tmp_path / "a" / "kernels", value=1)
    mk2 = _make_dataset(tmp_path / "b" / "kernels", value=2)
    sm1 = QuickSpiceManager(mk=str(mk1), kernels_dir=mk1.parent.parent)
    sm1.load_kernels()
    before = _pool_kernels()

    with QuickSpiceManager(mk=str(mk2), kernels_dir=mk2.parent.parent):
        assert _value("QA") == 2

    assert _pool_kernels() == before
    assert _value("QA") == 1


def test_unload_deletes_localized_metakernel(tmp_path):
    mk = _make_dataset(tmp_path / "ds" / "kernels", value=1)
    sm = QuickSpiceManager(mk=str(mk), kernels_dir=mk.parent.parent)
    tmp_mk = sm.load_kernels()
    assert tmp_mk.exists()

    sm.unload_kernels()

    assert not tmp_mk.exists()
    assert _pool_kernels() == []


def test_unfurnished_resolved_mk_deleted_on_gc(tmp_path):
    """A localized copy that was never furnished is still cleaned up when
    its manager is garbage-collected."""
    mk = _make_dataset(tmp_path / "ds" / "kernels", value=1)
    sm = QuickSpiceManager(mk=str(mk), kernels_dir=mk.parent.parent)
    tmp_mk = sm.resolved_mk
    assert tmp_mk.exists()

    del sm
    gc.collect()

    assert not tmp_mk.exists()


def test_snapshot_round_trip_single_tree(tmp_path):
    mk = _make_dataset(tmp_path / "ds" / "kernels", value=1)
    sm = QuickSpiceManager(mk=str(mk), kernels_dir=mk.parent.parent)
    sm.load_kernels()
    before = _pool_kernels()

    snap = sm.snapshot_pool(tmp_path / "snap.tm")
    content = snap.read_text(encoding="utf-8")
    spiceypy.kclear()
    spiceypy.furnsh(str(snap))

    assert _pool_kernels() == before
    # Single-tree format is unchanged: one $KERNELS symbol.
    assert "PATH_SYMBOLS = ( 'KERNELS' )" in content
    assert "'$KERNELS/pck/qa.tpc'" in content


def test_provenance_manager_source(tmp_path):
    mk = _make_dataset(tmp_path / "ds" / "kernels", value=1)
    sm = QuickSpiceManager(mk=str(mk), kernels_dir=mk.parent.parent)
    sm.load_kernels()

    info = sm.kernel_provenance()

    assert info["metakernel"] == str(mk)
    assert info["extra_kernels"] == []
    assert info["all_kernels"] == sorted(_pool_kernels())


def test_provenance_pool_for_foreign_metakernel(tmp_path):
    """A metakernel furnished outside this library is reported as-is."""
    root = tmp_path / "ds" / "kernels"
    mk = _make_dataset(root, value=1, path_values=str(root))
    spiceypy.furnsh(str(mk))

    info = QuickSpiceManager(mk=str(mk)).kernel_provenance(source="pool")

    assert info["metakernel"] == str(mk)
    assert info["extra_kernels"] == []


# ---------------------------------------------------------------------------
# issues/local_metakernel_uses_cache_kernels_dir.md
# ---------------------------------------------------------------------------


def test_local_metakernel_loads_kernels_next_to_it_with_empty_cache(tmp_path):
    root = tmp_path / "ds" / "kernels"
    mk = _make_dataset(root, value=1)

    sm = QuickSpiceManager(mk=str(mk))
    sm.load_kernels()

    assert _value("QA") == 1
    assert _pool_kernels() == [str(root / rel) for rel in _REL_KERNELS]
    assert sm._kernels_dir == root


def test_local_metakernel_ignores_populated_cache(tmp_path, real_pool):
    root = tmp_path / "ds" / "kernels"
    mk = _make_dataset(root, value=1)
    cache = real_pool / "juice"
    for rel in _REL_KERNELS:
        _text_kernel(cache / rel, 2)  # same layout, different content

    QuickSpiceManager(mk=str(mk)).load_kernels()

    assert _value("QA") == 1
    assert not any(k.startswith(str(cache)) for k in _pool_kernels())


def test_local_metakernel_with_absolute_path_values(tmp_path):
    root = tmp_path / "ds" / "kernels"
    mk = _make_dataset(root, value=1, path_values=str(root))
    mk = mk.rename(tmp_path / "elsewhere.tm")

    sm = QuickSpiceManager(mk=str(mk))
    sm.load_kernels()

    assert _value("QA") == 1
    assert sm._kernels_dir == root


def test_local_metakernel_does_not_write_locks_into_dataset(tmp_path):
    """The kernels dir derived from a user's dataset may be read-only;
    load_kernels() must not create lock files next to it."""
    ds = tmp_path / "ds"
    mk = _make_dataset(ds / "kernels", value=1)
    before = sorted(p.name for p in ds.iterdir())

    QuickSpiceManager(mk=str(mk)).load_kernels()

    assert sorted(p.name for p in ds.iterdir()) == before


def test_local_metakernel_provenance_reports_kernels_dir(tmp_path):
    root = tmp_path / "ds" / "kernels"
    mk = _make_dataset(root, value=1)
    sm = QuickSpiceManager(mk=str(mk))
    sm.load_kernels()

    assert sm.kernel_provenance()["kernels_dir"] == str(root)
    assert sm.kernel_provenance(source="pool")["kernels_dir"] == str(root)


@pytest.mark.parametrize("min_len", [85, 140, 240])
def test_long_derived_kernels_dir_loads(tmp_path, min_len):
    """Kernels dirs over 80 (string limit) and over ~120 (132-char line
    limit) characters are written with '+' continuation and load."""
    root = _long_dir(tmp_path, min_len)
    mk = _make_dataset(root, value=1)

    QuickSpiceManager(mk=str(mk)).load_kernels()

    assert _value("QA") == 1
    assert _pool_kernels() == [str(root / rel) for rel in _REL_KERNELS]


def test_long_explicit_kernels_dir_loads(tmp_path):
    mk = _make_dataset(tmp_path / "ds" / "kernels", value=1)
    other = _long_dir(tmp_path, 150)
    for rel in _REL_KERNELS:
        _text_kernel(other / rel, 3)

    QuickSpiceManager(mk=str(mk), kernels_dir=other).load_kernels()

    assert _value("QA") == 3


def test_localized_metakernel_lines_within_spice_limits(tmp_path):
    root = _long_dir(tmp_path, 200)
    mk = _make_dataset(root, value=1)

    sm = QuickSpiceManager(mk=str(mk))
    tmp_mk = sm.resolved_mk

    lines = tmp_mk.read_text(encoding="utf-8").splitlines()
    assert max(len(line) for line in lines) <= 132  # noqa: PLR2004


# ---------------------------------------------------------------------------
# issues/pool_provenance_reports_temp_metakernel.md
# ---------------------------------------------------------------------------


def test_pool_provenance_reports_real_metakernel(tmp_path):
    root = tmp_path / "ds" / "kernels"
    mk = _make_dataset(root, value=1)
    sm = QuickSpiceManager(mk=str(mk))
    tmp_mk = sm.load_kernels()

    info = sm.kernel_provenance(source="pool")

    assert _pool_meta() == [str(tmp_mk)]  # the pool itself has the temp copy
    assert info["metakernel"] == str(mk)
    assert info["extra_kernels"] == []


def test_pool_provenance_survives_manager_gc(tmp_path):
    root = tmp_path / "ds" / "kernels"
    mk = _make_dataset(root, value=1)
    sm = QuickSpiceManager(mk=str(mk))
    tmp_mk = sm.load_kernels()

    del sm
    gc.collect()

    # Kernels stay loaded, and the temp copy their META entry points to
    # stays on disk while it is in the pool.
    assert len(_pool_kernels()) == 2  # noqa: PLR2004
    assert tmp_mk.exists()
    info = QuickSpiceManager(mk=str(mk)).kernel_provenance(source="pool")
    assert info["metakernel"] == str(mk)
    assert info["extra_kernels"] == []


def test_pool_provenance_lists_raw_furnsh_as_extra(tmp_path):
    root = tmp_path / "ds" / "kernels"
    mk = _make_dataset(root, value=1)
    sm = QuickSpiceManager(mk=str(mk))
    sm.load_kernels()
    extra = _text_kernel(tmp_path / "user" / "qx.tpc", 9)
    spiceypy.furnsh(str(extra))

    info = sm.kernel_provenance(source="pool")

    assert info["metakernel"] == str(mk)
    assert info["extra_kernels"] == [str(extra)]


def test_unload_after_gc_of_another_manager_still_possible(tmp_path):
    """The META entry's file must exist for spiceypy.unload() by path."""
    mk = _make_dataset(tmp_path / "ds" / "kernels", value=1)
    tmp_mk = QuickSpiceManager(mk=str(mk)).load_kernels()
    gc.collect()

    spiceypy.unload(str(tmp_mk))

    assert _pool_kernels() == []


def test_orphaned_localized_file_swept_once_out_of_the_pool(tmp_path):
    """A copy kept because it was in the pool when its manager was collected
    is deleted by the next load once a kclear has removed it from the pool."""
    mk1 = _make_dataset(tmp_path / "a" / "kernels", value=1)
    mk2 = _make_dataset(tmp_path / "b" / "kernels", value=2)
    tmp_mk = QuickSpiceManager(mk=str(mk1)).load_kernels()
    gc.collect()
    assert tmp_mk.exists()

    sm2 = QuickSpiceManager(mk=str(mk2))
    sm2.load_kernels()  # exclusive: kclear drops tmp_mk from the pool

    assert not tmp_mk.exists()
    assert sm2.kernel_provenance(source="pool")["metakernel"] == str(mk2)


def test_orphaned_localized_file_kept_while_in_pool(tmp_path):
    mk1 = _make_dataset(tmp_path / "a" / "kernels", value=1)
    mk2 = _make_dataset(tmp_path / "b" / "kernels", value=2)
    tmp_mk = QuickSpiceManager(mk=str(mk1)).load_kernels()
    gc.collect()

    QuickSpiceManager(mk=str(mk2), exclusive=False).load_kernels()

    assert tmp_mk.exists()  # still a META entry in the pool


def test_atexit_cleanup_removes_remaining_localized_files(tmp_path):
    mk = _make_dataset(tmp_path / "ds" / "kernels", value=1)
    tmp_mk = QuickSpiceManager(mk=str(mk)).load_kernels()
    gc.collect()
    assert tmp_mk.exists()

    spice_manager_module._cleanup_localized_metakernels()

    assert not tmp_mk.exists()


# ---------------------------------------------------------------------------
# issues/snapshot_pool_80_char_strings.md
# ---------------------------------------------------------------------------


def _snapshot_round_trip(sm: QuickSpiceManager, out: Path) -> tuple[list, list]:
    before = _pool_kernels()
    sm.snapshot_pool(out)
    spiceypy.kclear()
    spiceypy.furnsh(str(out))
    return before, _pool_kernels()


def test_snapshot_round_trip_two_trees(tmp_path):
    mk = _make_dataset(tmp_path / "ds" / "kernels", value=1)
    sm = QuickSpiceManager(mk=str(mk))
    sm.load_kernels()
    # A kernel in a different tree; the common prefix becomes much shorter.
    sm.add_kernel(_text_kernel(tmp_path.parent / f"{tmp_path.name}_x" / "qx.tpc", 9))

    before, after = _snapshot_round_trip(sm, tmp_path / "snap.tm")

    assert after == before
    assert _value("QX") == 9


def test_snapshot_round_trip_kernels_under_filesystem_root(tmp_path):
    """Kernels whose only common directory is '/' (the reported failure)."""
    mk = _make_dataset(_long_dir(tmp_path / "ds", 90), value=1)
    sm = QuickSpiceManager(mk=str(mk))
    sm.load_kernels()
    before = _pool_kernels()

    # Simulate a pool whose common prefix is '/' by snapshotting through a
    # pool listing that also contains a path under another top-level dir.
    other_top = Path("/dev/null")
    real_kdata = spiceypy.kdata
    n = spiceypy.ktotal("ALL")

    def fake_ktotal(kind):
        return n + 1

    def fake_kdata(i, kind):
        if i == n:
            return (str(other_top), "TEXT", "", 0)
        return real_kdata(i, kind)

    out = tmp_path / "snap.tm"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(spice_manager_module.spiceypy, "ktotal", fake_ktotal)
        mp.setattr(spice_manager_module.spiceypy, "kdata", fake_kdata)
        sm.snapshot_pool(out)

    content = out.read_text(encoding="utf-8")
    assert "PATH_VALUES = ( '/' )" not in content
    assert max(len(line) for line in content.splitlines()) <= 132  # noqa: PLR2004
    # Load it without the /dev/null entry: drop that line and check the rest.
    kept = "\n".join(
        line for line in content.splitlines() if "null" not in line
    )
    out.write_text(kept, encoding="utf-8")
    spiceypy.kclear()
    spiceypy.furnsh(str(out))
    assert _pool_kernels() == before


def test_snapshot_round_trip_long_directory(tmp_path):
    mk = _make_dataset(_long_dir(tmp_path, 150), value=1)
    sm = QuickSpiceManager(mk=str(mk))
    sm.load_kernels()
    sm.add_kernel(_text_kernel(tmp_path / "user" / "qx.tpc", 9))

    before, after = _snapshot_round_trip(sm, tmp_path / "snap.tm")

    assert after == before


def test_snapshot_round_trip_long_file_name(tmp_path):
    mk = _make_dataset(tmp_path / "ds" / "kernels", value=1)
    sm = QuickSpiceManager(mk=str(mk))
    sm.load_kernels()
    long_name = "q" + "y" * 100 + ".tpc"
    sm.add_kernel(_text_kernel(tmp_path / "user" / long_name, 9))

    before, after = _snapshot_round_trip(sm, tmp_path / "snap.tm")

    assert after == before


def test_snapshot_keeps_load_order(tmp_path):
    mk = _make_dataset(tmp_path / "ds" / "kernels", value=1)
    sm = QuickSpiceManager(mk=str(mk))
    sm.load_kernels()
    # Same variable, loaded last: must still win after the round trip.
    sm.add_kernel(_text_kernel(tmp_path / "user" / "qa.tpc", 42))
    assert _value("QA") == 42  # noqa: PLR2004

    before, after = _snapshot_round_trip(sm, tmp_path / "snap.tm")

    assert after == before
    assert _value("QA") == 42  # noqa: PLR2004


def test_snapshot_records_metakernel_keywords_and_extras(tmp_path):
    keywords = (
        "    SKD_VERSION = 'v999_20260101_001'\n"
        "    MK_IDENTIFIER = 'test_v999_20260101_001'"
    )
    mk = _make_dataset(tmp_path / "ds" / "kernels", value=1, extra_data=keywords)
    sm = QuickSpiceManager(mk=str(mk))
    sm.load_kernels()
    extra = sm.add_kernel(_text_kernel(tmp_path / "user" / "qx.tpc", 9))

    snap = sm.snapshot_pool(tmp_path / "snap.tm")

    content = snap.read_text(encoding="utf-8")
    data, _, text = content.partition("\\begintext")
    assert "SKD_VERSION = 'v999_20260101_001'" in data
    assert "MK_IDENTIFIER = 'test_v999_20260101_001'" in data
    assert str(mk) in text
    assert str(extra) in text
    spiceypy.kclear()
    spiceypy.furnsh(str(snap))
    assert spiceypy.gcpool("SKD_VERSION", 0, 1)[0] == "v999_20260101_001"


def test_snapshot_round_trip_single_tree_long_common_prefix(tmp_path):
    root = _long_dir(tmp_path, 120)
    mk = _make_dataset(root, value=1)
    sm = QuickSpiceManager(mk=str(mk))
    sm.load_kernels()

    snap = tmp_path / "snap.tm"
    before, after = _snapshot_round_trip(sm, snap)

    assert after == before
    content = snap.read_text(encoding="utf-8")
    assert "PATH_SYMBOLS = ( 'KERNELS' )" in content
    assert max(len(line) for line in content.splitlines()) <= 132  # noqa: PLR2004


def test_snapshot_round_trip_mixed_absolute_and_relative(tmp_path, monkeypatch):
    mk = _make_dataset(tmp_path / "ds" / "kernels", value=1)
    sm = QuickSpiceManager(mk=str(mk))
    sm.load_kernels()
    _text_kernel(tmp_path / "rel" / "qx.tpc", 9)
    monkeypatch.chdir(tmp_path)
    spiceypy.furnsh("rel/qx.tpc")  # pool entry is the relative path

    before, after = _snapshot_round_trip(sm, tmp_path / "snap.tm")

    assert after == before
    assert _value("QX") == 9
