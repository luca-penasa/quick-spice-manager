"""Reduced kernel sets: a metakernel cut down to some time windows.

:func:`reduce_kernels` writes a copy of a metakernel and its kernels that
holds only the data inside the given time windows, and leaves out the
kernels a workload does not need. The data inside the windows is copied, not
resampled, so a reduced set gives the same answers as the full set there.
The typical use is test data small enough to commit to a repository.

=================  ==========================================================
kind               what is written
=================  ==========================================================
SPK (all types)    the records covering each window (``spksub``)
CK type 3          the pointing records covering each window, with the
                   interpolation intervals they belong to (``ckw03``)
binary PCK type 2  the Chebyshev records covering each window (``pckw02``)
anything else      copied whole: text kernels, DSKs, CK types other than 3,
                   binary kernels with any unsupported segment
=================  ==========================================================

A binary kernel without data in any window is left out of the reduced
metakernel.
"""

from __future__ import annotations

import fnmatch
import shutil
import textwrap
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Literal

import numpy as np
import spiceypy
from loguru import logger as log
from spiceypy.utils.exceptions import NotFoundError, SpiceyError

from .ftp import _parse_mk_kernel_paths
from .spice_manager import (
    QuickSpiceManager,
    _lookup_localized,
    _pool_lock,
    _quoted_lines,
    _read_mk_keywords,
)

#: Seconds added before and after each window by default (one day): the
#: records next to a window's edges are kept, so are interpolation and a
#: solver's extrapolation at block edges.
DEFAULT_PAD_SECONDS = 86400.0

Window = tuple["str | float", "str | float"]
Action = Literal["subset", "copied", "dropped", "no data"]

_DAF_EXTENSIONS = {".bsp": "spk", ".bc": "ck", ".bpc": "pck"}
_TEXT_EXTENSIONS = {".tls", ".tsc", ".tf", ".ti", ".tpc", ".tm", ".txt"}
_COMMENT_LINE = 79
_KB_100 = 100_000


class _CopyWhole(Exception):  # noqa: N818 - control flow, never escapes
    """A kernel that can't be subsetted after all: copy it whole, say why."""


@dataclass
class KernelReport:
    """What :func:`reduce_kernels` did with one kernel."""

    source: Path
    relative: str
    action: Action
    output: Path | None = None
    note: str = ""

    @property
    def size_in(self) -> int:
        return self.source.stat().st_size

    @property
    def size_out(self) -> int:
        return self.output.stat().st_size if self.output else 0


@dataclass
class ReducedKernels:
    """The reduced metakernel and what happened to each source kernel."""

    metakernel: Path
    windows_et: list[tuple[float, float]]
    kernels: list[KernelReport] = field(default_factory=list)

    @property
    def size_in(self) -> int:
        return sum(k.size_in for k in self.kernels)

    @property
    def size_out(self) -> int:
        return sum(k.size_out for k in self.kernels)

    def summary(self) -> str:
        """One line per kernel, then the totals."""
        lines = []
        for k in self.kernels:
            sizes = f"{_mb(k.size_in):>9} -> {_mb(k.size_out):>9}"
            note = f"  ({k.note})" if k.note else ""
            lines.append(f"{k.action:8} {sizes}  {k.relative}{note}")
        lines.append(
            f"{len(self.kernels)} kernels: {_mb(self.size_in)} -> {_mb(self.size_out)}"
            f"; metakernel {self.metakernel}",
        )
        return "\n".join(lines)


def reduce_kernels(  # noqa: PLR0913 - keyword options of one operation
    mk: str | Path,
    windows: Iterable[Window],
    out_dir: str | Path,
    *,
    drop: Sequence[str] = (),
    pad: float = DEFAULT_PAD_SECONDS,
    name: str | None = None,
    spacecraft: str = "JUICE",
    version: str = "latest",
    kernels_dir: str | Path | None = None,
    download_kernels: bool = True,
) -> ReducedKernels:
    """Write a copy of *mk* that holds only the data inside *windows*.

    Parameters
    ----------
    mk:
        Metakernel name (resolved and downloaded like ``QuickSpiceManager(mk=...)``)
        or path.
    windows:
        ``(start, stop)`` pairs, as UTC strings (anything ``str2et`` reads) or
        ephemeris times. Overlapping windows are merged after padding.
    out_dir:
        Where to write. The kernels keep their paths relative to the source
        kernels directory (``spk/...``, ``ck/...``) and the metakernel goes to
        ``mk/<name>.tm`` with ``PATH_VALUES = ( '..' )``, the layout of ESA's
        metakernels: ``QuickSpiceManager(mk=<that path>)`` loads it from any
        directory, ``furnsh`` from ``out_dir/mk``.
    drop:
        Glob patterns, matched against each kernel's relative path (e.g.
        ``"dsk/*"``), of kernels to leave out.
    pad:
        Seconds added before and after each window.
    name:
        Stem of the reduced metakernel; default ``<source stem>_reduced``.
    spacecraft, version, kernels_dir, download_kernels:
        As for :class:`QuickSpiceManager`.

    Returns
    -------
    ReducedKernels
        The metakernel written, and a report per source kernel.

    Raises
    ------
    ValueError
        If no window is given, or a window ends before it starts.
    """
    sm = QuickSpiceManager(
        spacecraft=spacecraft,
        mk=str(mk),
        version=version,
        kernels_dir=kernels_dir,
        download_kernels=download_kernels,
    )
    return _reduce_with(sm, windows, Path(out_dir), drop=drop, pad=pad, name=name)


def _reduce_with(  # noqa: PLR0913
    sm: QuickSpiceManager,
    windows: Iterable[Window],
    out_dir: Path,
    *,
    drop: Sequence[str],
    pad: float,
    name: str | None,
) -> ReducedKernels:
    """:func:`reduce_kernels` with the manager already made (the CLI makes its own)."""
    localized = sm.resolved_mk
    info = _lookup_localized(localized)
    if info is None:  # pragma: no cover - registered by resolved_mk
        msg = f"could not resolve {localized}"
        raise RuntimeError(msg)
    source_mk, root = info.source, info.kernels_dir
    relatives = _parse_mk_kernel_paths(
        source_mk.read_text(encoding="utf-8", errors="replace"),
    )
    sources = [(rel, _source_path(root, rel)) for rel in relatives]

    with _pool_lock, _text_kernels_loaded([p for _, p in sources]):
        windows_et = _merge_windows(windows, pad)
        reports = [
            _reduce_one(rel, path, out_dir, windows_et, drop) for rel, path in sources
        ]
        result = ReducedKernels(
            metakernel=out_dir / "mk" / f"{name or source_mk.stem + '_reduced'}.tm",
            windows_et=windows_et,
            kernels=reports,
        )
        _write_metakernel(result, source_mk, drop)
    log.info(
        f"reduced {source_mk.name}: {_mb(result.size_in)} -> {_mb(result.size_out)}",
    )
    return result


# ---------------------------------------------------------------------------
# Windows and the temporary text kernels
# ---------------------------------------------------------------------------


def _source_path(root: Path, rel: str) -> Path:
    path = Path(rel)
    return path if path.is_absolute() else root / rel


def _output_relative(rel: str) -> str:
    """Relative path of a kernel in the output (absolute ones go to ``other/``)."""
    path = Path(rel)
    return f"other/{path.name}" if path.is_absolute() else path.as_posix()


@contextmanager
def _text_kernels_loaded(kernels: Sequence[Path]) -> Iterator[None]:
    """Load the text kernels (LSK, SCLK, FK, ...) needed to convert times and
    name frames; unload afterwards only those that were not loaded before."""
    added = []
    for path in kernels:
        if path.suffix.lower() not in _TEXT_EXTENSIONS or path.suffix.lower() == ".tm":
            continue
        try:
            spiceypy.kinfo(str(path))
        except NotFoundError:
            spiceypy.furnsh(str(path))
            added.append(str(path))
    try:
        yield
    finally:
        for path in reversed(added):
            spiceypy.unload(path)


def _to_et(value: str | float) -> float:
    return float(value) if isinstance(value, (int, float)) else spiceypy.str2et(value)


def _merge_windows(windows: Iterable[Window], pad: float) -> list[tuple[float, float]]:
    spans = []
    for start, stop in windows:
        lo, hi = _to_et(start), _to_et(stop)
        if hi < lo:
            msg = f"window ends before it starts: {start!r} -> {stop!r}"
            raise ValueError(msg)
        spans.append((lo - pad, hi + pad))
    if not spans:
        msg = "no time window given"
        raise ValueError(msg)
    spans.sort()
    merged = [spans[0]]
    for lo, hi in spans[1:]:
        if lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


# ---------------------------------------------------------------------------
# One kernel
# ---------------------------------------------------------------------------


def _reduce_one(
    rel: str,
    source: Path,
    out_dir: Path,
    windows: list[tuple[float, float]],
    drop: Sequence[str],
) -> KernelReport:
    out_rel = _output_relative(rel)
    if any(fnmatch.fnmatch(out_rel, pattern) for pattern in drop):
        return KernelReport(source, out_rel, "dropped")
    output = out_dir / out_rel
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)  # DAF writers refuse to overwrite

    kind = _DAF_EXTENSIONS.get(source.suffix.lower())
    if kind is not None:
        unsupported = _unsupported_segments(source, kind)
        note = f"copied whole: {kind.upper()} segment types {sorted(unsupported)} not subsetted"
        if not unsupported:
            try:
                written = _SUBSETTERS[kind](source, output, windows)
            except _CopyWhole as exc:
                output.unlink(missing_ok=True)
                written, note = -1, f"copied whole: {exc}"
            if written == 0:
                output.unlink(missing_ok=True)
                return KernelReport(
                    source,
                    out_rel,
                    "no data",
                    note="no data in the windows",
                )
            if written > 0:
                return KernelReport(
                    source,
                    out_rel,
                    "subset",
                    output,
                    f"{written} segments",
                )
    else:
        note = ""
    shutil.copyfile(source, output)
    return KernelReport(source, out_rel, "copied", output, note)


def _segments(
    handle: int,
    nd: int,
    ni: int,
) -> Iterator[tuple[np.ndarray, list[float], list[int], str]]:
    spiceypy.dafbfs(handle)
    while spiceypy.daffna():
        descr = spiceypy.dafgs(nd + (ni + 1) // 2)
        dc, ic = spiceypy.dafus(descr, nd, ni)
        yield descr, list(dc), [int(i) for i in ic], spiceypy.dafgn(100).strip()


_LAYOUT = {"spk": (2, 6), "ck": (2, 6), "pck": (2, 5)}
_TYPE_INDEX = {"spk": 3, "ck": 2, "pck": 2}
_SUPPORTED = {"spk": None, "ck": {3}, "pck": {2}}  # None: every type spksub reads


def _unsupported_segments(source: Path, kind: str) -> set[int]:
    nd, ni = _LAYOUT[kind]
    supported = _SUPPORTED[kind]
    if supported is None:
        return set()
    handle = spiceypy.dafopr(str(source))
    try:
        types = {ic[_TYPE_INDEX[kind]] for _, _, ic, _ in _segments(handle, nd, ni)}
    finally:
        spiceypy.dafcls(handle)
    return types - supported


def _overlaps(
    begin: float,
    end: float,
    windows: list[tuple[float, float]],
) -> Iterator[tuple[float, float]]:
    for lo, hi in windows:
        if lo <= end and hi >= begin:
            yield max(lo, begin), min(hi, end)


def _subset_spk(source: Path, output: Path, windows: list[tuple[float, float]]) -> int:
    with _new_daf(source, output, windows, "spk") as (handle, new, written):
        for descr, dc, _ic, name in _segments(handle, 2, 6):
            for lo, hi in _overlaps(dc[0], dc[1], windows):
                spiceypy.spksub(handle, descr, name, lo, hi, new)
                written[0] += 1
    return written[0]


def _subset_ck(source: Path, output: Path, windows: list[tuple[float, float]]) -> int:
    with _new_daf(source, output, windows, "ck") as (handle, new, written):
        for _descr, dc, ic, name in _segments(handle, 2, 6):
            inst, ref, _type, avflag, begin, end = ic
            sclk = spiceypy.ckmeta(inst, "SCLK")
            for lo_et, hi_et in windows:
                try:
                    lo, hi = spiceypy.sce2c(sclk, lo_et), spiceypy.sce2c(sclk, hi_et)
                except SpiceyError as exc:
                    msg = f"no SCLK {sclk} for CK instrument {inst} ({exc.short})"
                    raise _CopyWhole(msg) from exc
                if lo > dc[1] or hi < dc[0]:
                    continue
                written[0] += _write_ck03_window(
                    handle,
                    new,
                    (begin, end),
                    (dc[0], dc[1]),
                    (lo, hi),
                    inst=inst,
                    ref=spiceypy.frmnam(ref),
                    avflag=bool(avflag),
                    name=name,
                )
    return written[0]


def _write_ck03_window(  # noqa: PLR0913
    handle: int,
    new: int,
    addresses: tuple[int, int],
    coverage: tuple[float, float],
    window: tuple[float, float],
    *,
    inst: int,
    ref: str,
    avflag: bool,
    name: str,
) -> int:
    """Copy the type 3 records of one segment that cover *window* (SCLK ticks).

    Layout (CK Required Reading): N records of 4 (quaternion) or 7 (and angular
    velocity) values, N epochs, the epoch directory, NINT interval starts,
    their directory, NINT, N.
    """
    begin, end = addresses
    nints, n = (int(v) for v in spiceypy.dafgda(handle, end - 1, end))
    size = 7 if avflag else 4
    epochs = np.asarray(
        spiceypy.dafgda(handle, begin + n * size, begin + n * size + n - 1),
    )
    starts_at = begin + n * size + n + (n - 1) // 100
    starts = np.asarray(spiceypy.dafgda(handle, starts_at, starts_at + nints - 1))

    lo, hi = window
    i0 = max(int(np.searchsorted(epochs, lo, side="right")) - 1, 0)
    i1 = min(int(np.searchsorted(epochs, hi, side="left")), n - 1)
    if i1 < i0:
        return 0
    records = np.asarray(
        spiceypy.dafgda(handle, begin + i0 * size, begin + (i1 + 1) * size - 1),
    ).reshape(-1, size)
    sel = epochs[i0 : i1 + 1]
    # the first record starts an interval; the later starts are kept as they are
    sel_starts = np.concatenate(
        [[sel[0]], starts[(starts > sel[0]) & (starts <= sel[-1])]],
    )
    avvs = records[:, 4:] if avflag else np.zeros((len(sel), 3))
    spiceypy.ckw03(
        new,
        max(coverage[0], min(lo, sel[0])),
        min(coverage[1], max(hi, sel[-1])),
        inst,
        ref,
        avflag,
        name,
        len(sel),
        np.ascontiguousarray(sel),
        np.ascontiguousarray(records[:, :4]),
        np.ascontiguousarray(avvs),
        len(sel_starts),
        np.ascontiguousarray(sel_starts),
    )
    return 1


def _subset_pck(source: Path, output: Path, windows: list[tuple[float, float]]) -> int:
    with _new_daf(source, output, windows, "pck") as (handle, new, written):
        for _descr, dc, ic, name in _segments(handle, 2, 5):
            classid, ref, _type, begin, end = ic
            init, intlen, rsize, n = spiceypy.dafgda(handle, end - 3, end)
            rsize, n = int(rsize), int(n)
            degree = (rsize - 2) // 3 - 1
            for lo, hi in _overlaps(dc[0], dc[1], windows):
                k0 = min(max(int((lo - init) // intlen), 0), n - 1)
                k1 = min(max(int((hi - init) // intlen), 0), n - 1)
                records = np.asarray(
                    spiceypy.dafgda(
                        handle,
                        begin + k0 * rsize,
                        begin + (k1 + 1) * rsize - 1,
                    ),
                ).reshape(-1, rsize)
                spiceypy.pckw02(
                    new,
                    classid,
                    spiceypy.frmnam(ref),
                    lo,
                    hi,
                    name,
                    intlen,
                    k1 - k0 + 1,
                    degree,
                    np.ascontiguousarray(records[:, 2:]).ravel(),
                    init + k0 * intlen,
                )
                written[0] += 1
    return written[0]


_SUBSETTERS = {"spk": _subset_spk, "ck": _subset_ck, "pck": _subset_pck}


# ---------------------------------------------------------------------------
# Comments and the metakernel
# ---------------------------------------------------------------------------


def _windows_utc(windows: list[tuple[float, float]]) -> list[str]:
    return [
        f"{spiceypy.et2utc(lo, 'ISOC', 0)} -> {spiceypy.et2utc(hi, 'ISOC', 0)}"
        for lo, hi in windows
    ]


def _provenance(
    source: Path,
    windows: list[tuple[float, float]],
    *,
    more: str = "",
) -> list[str]:
    version = metadata.version("quick_spice_manager")
    lines = [
        f"Reduced by quick-spice-manager {version} from {source.name}.",
        "It holds the data of the source inside these windows (UTC, padded) only:",
        *[f"  {w}" for w in _windows_utc(windows)],
        *([more] if more else []),
        "",
    ]
    return [chunk for line in lines for chunk in _wrapped(line)]


def _comment_lines(source: Path, windows: list[tuple[float, float]]) -> list[str]:
    """Provenance, then the comment area of *source*."""
    lines = _provenance(source, windows, more="The comments of the source follow.")
    handle = spiceypy.dafopr(str(source))
    try:
        done = False
        while not done:
            n, buffer, done = spiceypy.dafec(handle, 200, 1000)
            lines.extend(buffer[:n])
    finally:
        spiceypy.dafcls(handle)
    return [line or " " for line in lines]


@contextmanager
def _new_daf(
    source: Path,
    output: Path,
    windows: list[tuple[float, float]],
    kind: str,
) -> Iterator[tuple[int, int, list[int]]]:
    """Open *source* to read and *output* to write, with its comments.

    Yields the two handles and a one-item counter of the segments written.
    """
    comments = _comment_lines(source, windows)
    opener, closer = _WRITERS[kind]
    handle = spiceypy.dafopr(str(source))
    new = opener(str(output), "reduced", sum(len(line) + 1 for line in comments))
    written = [0]
    try:
        spiceypy.dafac(new, comments)
        yield handle, new, written
    finally:
        if written[0]:
            closer(new)
        else:  # the writers' own close refuses a file without segments
            spiceypy.dafcls(new)
        spiceypy.dafcls(handle)


_WRITERS = {
    "spk": (spiceypy.spkopn, spiceypy.spkcls),
    "ck": (spiceypy.ckopn, spiceypy.ckcls),
    "pck": (spiceypy.pckopn, spiceypy.pckcls),
}


def _write_metakernel(
    result: ReducedKernels,
    source_mk: Path,
    drop: Sequence[str],
) -> None:
    kept = [k for k in result.kernels if k.output is not None]
    skd, identifier = _read_mk_keywords(source_mk)
    keywords = []
    if skd:
        keywords.append(f"SKD_VERSION = '{skd}'")
    if identifier:
        keywords.append(f"MK_IDENTIFIER = '{identifier}_reduced'")
    left_out = [k for k in result.kernels if k.output is None]
    comments = [
        *_provenance(source_mk, result.windows_et),
        *_wrapped(f"Source kernels directory: {source_mk.parent.parent}"),
        f"Dropped by pattern: {', '.join(drop) or 'none'}.",
        f"Left out ({len(left_out)}):",
        *[f"  {k.relative} ({k.action})" for k in left_out],
    ]
    lines = [
        "KPL/MK",
        "",
        *comments,
        "",
        "\\begindata",
        "",
        "PATH_VALUES  = ( '..' )",
        "PATH_SYMBOLS = ( 'KERNELS' )",
        "KERNELS_TO_LOAD = (",
        *[
            line
            for k in kept
            for line in _quoted_lines(f"$KERNELS/{k.relative}", "    ")
        ],
        ")",
        *keywords,
        "",
        "\\begintext",
        "",
    ]
    result.metakernel.parent.mkdir(parents=True, exist_ok=True)
    result.metakernel.write_text("\n".join(lines), encoding="utf-8")


def _wrapped(line: str) -> list[str]:
    return textwrap.wrap(line, _COMMENT_LINE, break_on_hyphens=False) or [""]


def _mb(size: int) -> str:
    return f"{size / 1e6:.2f} MB" if size >= _KB_100 else f"{size / 1e3:.1f} KB"
