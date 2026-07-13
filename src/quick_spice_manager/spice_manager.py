"""
Manages SPICE kernels for spacecraft operations.

Provides standalone kernel loading, context manager support for temporary
kernel pools, and optional integration with planetary_coverage.TourConfig.
"""

import os
import re
import shutil
import threading
import time
import warnings
import weakref
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any

import filelock
import pandas as pd
import spiceypy
from attrs import define, field
from attrs import setters as _setters
from dotenv import load_dotenv
from loguru import logger as log

from .dirs import (
    get_cache_lock_path,
    get_load_lock_dir,
    get_user_kernels_cache_directory,
)
from .ftp import (
    _parse_mk_kernel_paths,
    download_kernels_via_ftp,
    list_metakernels_via_ftp,
)
from .locking import bounded_slot

if TYPE_CHECKING:
    from contextlib import AbstractContextManager


def _invalidate_resolved_mk(instance: "QuickSpiceManager", _attrib, new_value):  # type: ignore[type-arg]
    """attrs on_setattr hook: delete the cached localized metakernel when any
    resolution-affecting attribute (_mk, _spacecraft, _version, _kernels_dir)
    is changed after construction."""
    cached = getattr(instance, "_localized_mk_path", None)
    if cached is not None:
        object.__setattr__(instance, "_localized_mk_path", None)
        # _loaded_mk_path points to the same temp file after load_kernels().
        # Don't delete it now — unload_kernels() owns cleanup in that case;
        # spiceypy.unload() needs to re-read the file to know what to unload.
        loaded = getattr(instance, "_loaded_mk_path", None)
        if cached != loaded:
            try:
                cached.unlink(missing_ok=True)
            except OSError:
                pass
    # The real resolved identity is stale too -- it no longer corresponds to
    # the (now-changed) configuration.
    object.__setattr__(instance, "_resolved_mk_path", None)
    object.__setattr__(instance, "_resolved_at", None)
    return new_value

# NAIF constraint: maximum number of characters in a SPICE PATH_VALUES entry.
# See planetary_coverage.spice.metakernel.KERNEL_MAX_LENGTH.
_KERNEL_MAX_LENGTH = 255

# Tracks the most recently active SpiceManager (the one that last called
# load_kernels).  A weakref is used so that managers which go out of scope
# are collected normally without keeping them alive.
_active_manager_ref: "weakref.ref | None" = None

# Guards every CSPICE call (furnsh/unload/kclear/ktotal/kdata) and the
# _active_manager_ref global against concurrent access from multiple threads
# in *this* process. Re-entrant because load_kernels() calls unload_kernels()
# internally and context managers may nest on the same thread.
#
# This lock intentionally does NOT coordinate across OS processes: CSPICE's
# kernel pool is a per-process global, so separate processes never need to
# serialize furnsh/unload/query calls against each other. Only the on-disk
# kernel cache directory (shared across processes) needs cross-process
# locking -- see ftp.py's use of filelock for that.
_pool_lock = threading.RLock()

# Small bounded pool of cross-process "furnish slots" per kernels_dir. Caps
# how many processes may be resolving+furnishing kernels from the same
# cache directory at once, so that a burst of managers/processes cold-
# starting together take turns instead of all hammering the same kernel
# files at once. By the time a later manager gets its turn, the metakernel
# and its referenced kernels are typically already cached by an earlier
# holder, so its own resolve/download-check step is a fast, local no-op.
# Scoped per kernels_dir (not global) so unrelated missions never wait on
# each other.
_MAX_CONCURRENT_FURNISH_SLOTS = 2

# How long to wait for a free furnish slot before giving up.
_FURNISH_SLOT_TIMEOUT_SECONDS = 300


def _furnish_slot(kernels_dir: Path) -> "AbstractContextManager[None]":
    """Cross-process bounded semaphore around resolving+furnishing kernels
    from *kernels_dir* -- see :data:`_MAX_CONCURRENT_FURNISH_SLOTS`."""
    return bounded_slot(
        get_load_lock_dir(kernels_dir),
        _MAX_CONCURRENT_FURNISH_SLOTS,
        timeout=_FURNISH_SLOT_TIMEOUT_SECONDS,
    )


# Matches the PATH_VALUES rewritten by _localize_metakernel() -- a single
# quoted path, same simple single-value assumption _localize_metakernel
# itself makes (this library never writes more than one PATH_VALUES entry).
_PATH_VALUES_RE = re.compile(r"PATH_VALUES\s*=\s*\(\s*'([^']+)'\s*\)")


def _kernels_referenced_by_metakernel(tm_path: Path) -> frozenset[str]:
    """Return the absolute paths of every non-META kernel *tm_path* itself
    references, by parsing its own (already-localized) ``PATH_VALUES`` /
    ``KERNELS_TO_LOAD`` -- self-contained, independent of any manager's
    bookkeeping. Used by :meth:`QuickSpiceManager.kernel_provenance` in
    ``source='pool'`` mode.

    Returns an empty ``frozenset`` if *tm_path* can't be read or has no
    ``PATH_VALUES`` to resolve relative paths against.
    """
    try:
        content = tm_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return frozenset()

    match = _PATH_VALUES_RE.search(content)
    if match is None:
        return frozenset()
    kernels_dir = Path(match.group(1))

    rel_paths = _parse_mk_kernel_paths(content)
    return frozenset(str(kernels_dir / rel) for rel in rel_paths)


def sizeof_fmt(num: float, suffix: str = "B") -> str:
    """
    Human-readable file size.

    from https://stackoverflow.com/questions/1094841/get-human-readable-version-of-file-size
    """
    for unit in ["", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"]:
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}{suffix}"
        num /= 1024.0
    return f"{num:.1f}Yi {suffix}"


@define
class QuickSpiceManager:
    """
    Manages SPICE kernel loading for spacecraft operations.

    Provides standalone kernel loading (no ``planetary_coverage`` required),
    context manager usage for temporary kernel pools, and optional integration
    with ``planetary_coverage.TourConfig``.

    Examples
    --------
    Standalone load/unload::

        sm = SpiceManager(mk="/path/to/kernels.tm")
        sm.load_kernels()
        # ... use SPICE ...
        sm.unload_kernels()

    As a context manager (saves and restores the kernel pool on exit)::

        with SpiceManager(mk="/path/to/kernels.tm") as sm:
            # kernels are loaded here; original pool restored on exit
            ...

    As an exclusive context manager (pool contains *exactly* the metakernel
    kernels during the block; original pool fully restored on exit)::

        with SpiceManager(mk="/path/to/kernels.tm", exclusive=True) as sm:
            # pool is cleared before loading; only this metakernel is active
            ...

    With planetary_coverage TourConfig (requires optional extra)::

        tc = SpiceManager().tour_config  # default target='Jupiter', instrument='JANUS'
        tc = SpiceManager().get_tour_config(target="Mars", instrument="OTHER")
        # install with: pip install quick-spice-manager[planetary-coverage]
    """

    # Exposes the module-level CSPICE pool lock for advanced users who call
    # spiceypy directly and want to participate in the same in-process
    # critical section as this manager (e.g. `with QuickSpiceManager.pool_lock:`).
    # Plain class attribute (no type annotation) so attrs' auto_attribs mode
    # does not turn it into an instance field.
    pool_lock = _pool_lock

    _spacecraft: str = field(default="JUICE", on_setattr=_invalidate_resolved_mk)
    _download_kernels: bool = field(default=True)
    _version: str = field(default="latest", on_setattr=_invalidate_resolved_mk)
    _mk: str = field(default="plan", on_setattr=_invalidate_resolved_mk)
    _kernels_dir: Path | None = field(
        default=None,
        converter=lambda x: Path(x) if x is not None else None,
        on_setattr=_setters.pipe(_setters.convert, _invalidate_resolved_mk),
    )
    _exclusive: bool = field(default=True)

    _localized_mk_path: Path | None = field(default=None, init=False)
    _saved_kernels: list[str] = field(factory=list, init=False)
    _loaded_mk_path: Path | None = field(default=None, init=False)
    _expected_kernels: frozenset[str] | None = field(default=None, init=False)
    # The real, resolved metakernel path (before localization) -- e.g. the
    # FTP-cached file with its actual filename/SKD version baked in, as
    # opposed to _localized_mk_path's temp copy. Set in resolved_mk(); used
    # by kernel_provenance() to document what was actually used.
    _resolved_mk_path: Path | None = field(default=None, init=False)
    # When this manager last (re-)resolved the metakernel (unix timestamp).
    # For version="latest" this is the closest thing to a version string --
    # exactly which moment's snapshot of "latest" was used.
    _resolved_at: float | None = field(default=None, init=False)
    # Snapshot of _expected_kernels taken immediately after furnsh() in
    # load_kernels(), before any add_kernel() calls -- i.e. exactly what the
    # metakernel itself specifies. The delta (_expected_kernels minus this)
    # is "extra" kernels added on top of the metakernel.
    _metakernel_kernels: frozenset[str] | None = field(default=None, init=False)
    # Stores the active manager ref that was in place when __enter__ was called,
    # so __exit__ can restore it (correct LIFO ordering for nested context managers).
    _prior_active_ref: "weakref.ref | None" = field(default=None, init=False)




    def __del__(self) -> None:
        """Delete the localized temp metakernel file when the instance is garbage-collected."""
        path = getattr(self, "_localized_mk_path", None)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    def _process_metakernel_override(self):
        """
        Process the SPICE_METAKERNEL environment variable override
        """

        log.debug("Processing metakernel override from environment variables")

        env_status = load_dotenv()
        log.debug(f".env file loaded: {env_status}")

        # these variables can be used to override the default behavior
        spice_metakernel = os.environ.get("SPICE_METAKERNEL", None)
        spice_directory = os.environ.get("SPICE_DIRECTORY", None)

        if spice_metakernel is not None:
            log.warning(
                f"Overriding metakernel with SPICE_METAKERNEL={spice_metakernel}. Also disabling automatic download of kernels.",
            )
            self._mk = spice_metakernel

            self._download_kernels = False

        if spice_directory is not None:
            log.warning(
                f"Overriding kernels directory with SPICE_DIRECTORY={spice_directory}",
            )
            self._kernels_dir = Path(spice_directory)

    def __attrs_post_init__(self) -> None:
        log.debug("Initializing SpiceManager")

        # Catch the common mistake of passing a file path as the first positional
        # argument (which maps to _spacecraft, not _mk).
        _sc = self._spacecraft
        if _sc.endswith(".tm") or _sc.endswith(".TM") or Path(_sc).is_file():
            raise ValueError(
                f"'spacecraft' looks like a file path ({_sc!r}). "
                "Did you mean: SpiceManager(mk=...)?  "
                "Pass the metakernel as a keyword argument.",
            )

        log.info(
            f"Using user kernels cache directory at {self.user_kernels_cache_directory}",
        )

        self._process_metakernel_override()

        # Resolve _mk to an absolute path at construction time while the CWD is
        # still what the caller intended.  This prevents relative paths like
        # './kernels.tm' from failing later if the CWD changes (common in notebooks).
        _mk_path = Path(self._mk)
        if _mk_path.is_file():
            self._mk = str(_mk_path.resolve())
            log.debug(f"Resolved local metakernel to absolute path: {self._mk}")

        if self._mk is None:
            self._mk = self.metakernels[0]
        log.warning(f"Using as default meta-kernel {self._mk}")

        if self._kernels_dir is None:
            self._kernels_dir = self.user_kernels_cache_directory

    def _resolve_metakernel(self) -> Path:
        """Locate the metakernel file, downloading it via FTP if not already local.

        If :attr:`_download_kernels` is ``False``, resolution never touches
        the network -- it's satisfied entirely from the local resolution
        cache, raising ``FileNotFoundError`` if no complete cached
        resolution exists.
        """
        mk_path = Path(self._mk)
        if mk_path.is_file():
            log.info(f"Metakernel {self._mk} is a local file, skipping FTP resolution.")
            return mk_path
        log.info(f"Resolving metakernel {self._mk!r} via FTP.")
        return download_kernels_via_ftp(
            spacecraft=self._spacecraft,
            mk=self._mk,
            kernels_dir=self._kernels_dir,
            version=self._version,
            download_kernels=self._download_kernels,
        )

    def _localize_metakernel(self, mk_path: Path) -> Path:
        """Create a temporary copy of the metakernel with PATH_VALUES rewritten to
        point to the local kernels directory.

        The returned temp file is **not** automatically deleted — it must be
        removed by the caller (``unload_kernels`` / ``__exit__``) because
        ``spiceypy.unload()`` needs to re-read the file to determine which
        individual kernels to unload.

        Raises
        ------
        ValueError
            If ``kernels_dir`` exceeds the NAIF 255-character path limit.
        """
        kernels_dir_str = str(self._kernels_dir)
        if len(kernels_dir_str) > _KERNEL_MAX_LENGTH:
            raise ValueError(
                f"kernels_dir path exceeds NAIF {_KERNEL_MAX_LENGTH}-char limit "
                f"(len={len(kernels_dir_str)}): {kernels_dir_str}",
            )

        content = mk_path.read_text(encoding="utf-8")

        # Rewrite PATH_VALUES = ( '...' ) with the local kernels_dir.
        # Matches a single-quoted path between PATH_VALUES = ( and ), including
        # surrounding whitespace variants as used by ESA JUICE metakernels.
        localized = re.sub(
            r"(PATH_VALUES\s*=\s*\(\s*')([^']+)('\s*\))",
            lambda m: m.group(1) + kernels_dir_str + m.group(3),
            content,
        )

        tmp = NamedTemporaryFile(
            mode="w",
            prefix=mk_path.stem + "-localized-",
            suffix=".tm",
            delete=False,
            encoding="utf-8",
        )
        tmp.write(localized)
        tmp.close()
        return Path(tmp.name)

    def _pool_files(self) -> frozenset[str]:
        """Return the set of non-META kernel file paths currently in the SPICE pool."""
        with _pool_lock:
            result: list[str] = []
            for i in range(spiceypy.ktotal("ALL")):
                fname, ftype, _source, _handle = spiceypy.kdata(i, "ALL")
                if ftype.strip().upper() != "META":
                    result.append(fname)
            return frozenset(result)

    def _snapshot_pool_entries(self) -> list[str]:
        """Snapshot current non-META kernel entries that still exist on disk."""
        with _pool_lock:
            n = spiceypy.ktotal("ALL")
            saved: list[str] = []
            for i in range(n):
                fname, ftype, _source, _handle = spiceypy.kdata(i, "ALL")
                # Skip META entries — these are often temporary metakernel files.
                if ftype.strip().upper() == "META":
                    continue
                if Path(fname).exists():
                    saved.append(fname)
                else:
                    log.debug(
                        f"Snapshot pool: skipping non-existent pool entry: {fname}",
                    )
            return saved

    @property
    def is_active(self) -> bool:
        """Whether this manager is the current SPICE pool owner.

        The pool owner is the last manager to call ``load_kernels()``.
        When a second manager loads its kernels it becomes the active owner
        and this property returns ``False`` for the first manager.

        Use this to detect that the pool has been taken over by a different
        configuration rather than merely dirtied by kernel additions.
        """
        global _active_manager_ref
        with _pool_lock:
            if _active_manager_ref is None:
                return False
            active = _active_manager_ref()
            return active is self

    @property
    def is_dirty(self) -> bool | None:
        """Whether the live SPICE pool differs from the state after ``load_kernels()``.

        Returns
        -------
        bool
            ``True`` if kernels have been added or removed since loading,
            ``False`` if the pool exactly matches the expected state.
        None
            If ``load_kernels()`` has not been called yet, or if this manager
            is no longer the active pool owner (use ``is_active`` to distinguish).
        """
        with _pool_lock:
            if self._expected_kernels is None:
                return None
            if not self.is_active:
                return None
            return self._pool_files() != self._expected_kernels

    def clean_pool(self) -> dict[str, list[str]]:
        """Restore the SPICE pool to match the metakernel that was loaded.

        - Kernels added after ``load_kernels()`` are **unloaded**.
        - Kernels that were expected but are no longer in the pool are
          **re-furnished** (provided their files still exist on disk).

        Returns
        -------
        dict
            ``{"unloaded": [...], "restored": [...]}`` listing the paths
            that were acted on.

        Raises
        ------
        RuntimeError
            If ``load_kernels()`` has not been called (no expected state).
        """
        with _pool_lock:
            if self._expected_kernels is None:
                raise RuntimeError(
                    "load_kernels() has not been called; no expected state to clean to.",
                )
            if not self.is_active:
                raise RuntimeError(
                    "This manager is no longer the active pool owner (another manager "
                    "loaded its kernels after this one). Call load_kernels() again to "
                    "reclaim the pool, or use 'with SpiceManager(...) as sm:' instead.",
                )
            current = self._pool_files()
            extras = current - self._expected_kernels
            missing = self._expected_kernels - current

            unloaded: list[str] = []
            for fname in extras:
                log.info(f"clean_pool: unloading extra kernel {fname}")
                spiceypy.unload(fname)
                unloaded.append(fname)

            restored: list[str] = []
            for fname in missing:
                if Path(fname).exists():
                    log.info(f"clean_pool: restoring missing kernel {fname}")
                    spiceypy.furnsh(fname)
                    restored.append(fname)
                else:
                    log.warning(
                        f"clean_pool: expected kernel no longer on disk, "
                        f"skipping: {fname}",
                    )

            if unloaded or restored:
                log.debug(
                    f"clean_pool: unloaded {len(unloaded)}, "
                    f"restored {len(restored)} kernels",
                )
            return {"unloaded": unloaded, "restored": restored}

    @property
    def resolved_mk(self) -> Path:
        """Path to a localized, ``spiceypy``-furnshable copy of the metakernel.

        Accessing this property triggers FTP download of any kernel files that
        are not yet present in the local cache, then produces a temporary
        metakernel file with ``PATH_VALUES`` rewritten to point at the local
        kernels directory.  The result is cached on the instance; the same path
        is returned on subsequent calls without repeating the work.

        This does **not** load any kernels into the SPICE pool.  Use it when
        you want full control over kernel loading::

            sm = QuickSpiceManager(spacecraft="JUICE", mk="plan")
            spiceypy.furnsh(str(sm.resolved_mk))

        Call :meth:`load_kernels` (or use the context manager) when you want
        the manager to own the pool and handle cleanup automatically.
        """
        with _pool_lock:
            if self._localized_mk_path is None:
                original = self._resolve_metakernel()
                self._localized_mk_path = self._localize_metakernel(original)
                self._resolved_mk_path = original
                self._resolved_at = time.time()
                log.debug(
                    f"Resolved and localized metakernel to {self._localized_mk_path}",
                )
            return self._localized_mk_path

    def load_kernels(self) -> Path:
        """Download (if needed), localize, and furnish the metakernel.

        Returns the path to the localized temporary metakernel file that was
        furnished.  This is the same path exposed by :attr:`resolved_mk`.

        Acquires a cross-process furnish slot for :attr:`_kernels_dir` first
        (see :data:`_MAX_CONCURRENT_FURNISH_SLOTS`) — held for the resolve
        *and* furnish steps, so a burst of managers/processes cold-starting
        against the same cache directory take turns rather than all hitting
        the same kernel files at once — then the in-process pool lock.
        """
        global _active_manager_ref
        kernels_dir = self._kernels_dir
        assert kernels_dir is not None  # noqa: S101 - set in __attrs_post_init__
        with _furnish_slot(kernels_dir), _pool_lock:
            if self._loaded_mk_path is not None:
                log.warning(
                    "load_kernels() called while this manager is already loaded; "
                    "unloading current state first.",
                )
                self.unload_kernels()

            # If we are superseding another SpiceManager that owns the pool,
            # preserve its expected kernel set so unload_kernels() can restore it.
            # This avoids an extra live pool scan in load_kernels().
            active_mgr = (
                _active_manager_ref() if _active_manager_ref is not None else None
            )
            if not self._saved_kernels:
                if (
                    active_mgr is not None
                    and active_mgr is not self
                    and active_mgr._expected_kernels is not None
                ):
                    self._saved_kernels = sorted(active_mgr._expected_kernels)
                    log.debug(
                        "Captured pre-load pool from prior active manager "
                        f"({len(self._saved_kernels)} kernels)",
                    )
                else:
                    self._saved_kernels = []

            # triggers download + localization if not already done
            tmp_path = self.resolved_mk

            if self._exclusive:
                log.debug("Exclusive mode: clearing pool before loading metakernel")
                spiceypy.kclear()

            log.info(f"Furnishing metakernel {tmp_path}")
            spiceypy.furnsh(tmp_path.as_posix())
            self._loaded_mk_path = tmp_path
            self._expected_kernels = self._pool_files()
            # Snapshot before any add_kernel() calls -- this is exactly what
            # the metakernel itself specifies, used by kernel_provenance()
            # to separate "from the metakernel" from "added afterward".
            self._metakernel_kernels = self._expected_kernels

            # Save who was active before taking ownership (for unload restore).
            self._prior_active_ref = _active_manager_ref
            _active_manager_ref = weakref.ref(self)
            return tmp_path

    def add_kernel(
        self, path: "Path | str | Iterable[Path | str]",
    ) -> "Path | list[Path]":
        """Furnish one or more additional kernels and register them as expected.

        Unlike loading kernels externally (which would mark the pool dirty),
        kernels added via this method are treated as intentional additions:
        ``is_dirty`` remains ``False`` after the call and ``snapshot_pool``
        will include them.

        Parameters
        ----------
        path:
            A single kernel path **or** an iterable of paths (list, tuple,
            generator, etc.).  Paths may be absolute or relative.

        Returns
        -------
        Path | list[Path]
            The resolved absolute path(s). Returns a single ``Path`` when a
            single path was given, otherwise a ``list[Path]``.

        Raises
        ------
        FileNotFoundError
            If any kernel file does not exist (checked before any are furnished).
        RuntimeError
            If this manager is not the active pool owner.
        """
        # Distinguish scalar from iterable input (str is iterable but means scalar)
        if isinstance(path, (str, Path)):
            paths = [path]
            scalar = True
        else:
            paths = list(path)
            scalar = False

        # Resolve and validate all paths before furnishing any (all-or-nothing)
        resolved: list[Path] = []
        for p in paths:
            rp = Path(p).resolve()
            if not rp.exists():
                raise FileNotFoundError(f"Kernel file not found: {rp}")
            resolved.append(rp)

        with _pool_lock:
            if self._expected_kernels is not None and not self.is_active:
                raise RuntimeError(
                    "This manager is not the active pool owner. "
                    "Call load_kernels() to reclaim the pool before adding kernels.",
                )

            for rp in resolved:
                log.info(f"Furnishing additional kernel: {rp}")
                spiceypy.furnsh(str(rp))

            # Update expected set so additions are considered intentional (not dirty).
            if self._expected_kernels is not None:
                self._expected_kernels = self._expected_kernels | {
                    str(rp) for rp in resolved
                }

        return resolved[0] if scalar else resolved

    def snapshot_pool(self, path: "Path | str") -> Path:
        """Write the current SPICE kernel pool to a metakernel file.

        Creates a standard KPL/MK file listing every currently loaded kernel
        (excluding META-type entries) so the exact pool state can be reproduced
        later by furnishing the output file.

        Parameters
        ----------
        path:
            Destination path for the snapshot metakernel (typically ``*.tm``).
            Parent directories are created if they do not exist.

        Returns
        -------
        Path
            The path that was written.

        Raises
        ------
        ValueError
            If any kernel path in the pool exceeds the NAIF 255-character limit.
        """
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)

        with _pool_lock:
            n = spiceypy.ktotal("ALL")
            kernels: list[str] = []
            for i in range(n):
                fname, ftype, _source, _handle = spiceypy.kdata(i, "ALL")
                if ftype.strip().upper() == "META":
                    continue
                if len(fname) > _KERNEL_MAX_LENGTH:
                    raise ValueError(
                        f"Kernel path exceeds NAIF {_KERNEL_MAX_LENGTH}-char limit "
                        f"(len={len(fname)}): {fname}",
                    )
                kernels.append(fname)

        # Factor out the longest common directory prefix as PATH_VALUES so that
        # each KERNELS_TO_LOAD entry uses the $KERNELS symbol instead of the
        # full absolute path — this keeps entries well within the NAIF 255-char
        # limit and makes the snapshot more portable.
        common_prefix = ""
        if kernels:
            # os.path.commonpath works on the directory parts
            import os
            common_prefix = os.path.commonpath(kernels)
            # commonpath may return a file path if all kernels share the same
            # parent; ensure we always get a directory.
            if not Path(common_prefix).is_dir():
                common_prefix = str(Path(common_prefix).parent)

        if common_prefix:
            rel_kernels = [
                "$KERNELS/" + Path(k).relative_to(common_prefix).as_posix()
                for k in kernels
            ]
            path_block = (
                f"PATH_VALUES = ( '{common_prefix}' )\n"
                f"PATH_SYMBOLS = ( 'KERNELS' )\n"
            )
        else:
            rel_kernels = kernels
            path_block = ""

        # Validate that no entry exceeds the limit after symbol substitution
        for entry in rel_kernels:
            resolved_len = len(entry.replace("$KERNELS", common_prefix))
            if resolved_len > _KERNEL_MAX_LENGTH:
                raise ValueError(
                    f"Kernel path exceeds NAIF {_KERNEL_MAX_LENGTH}-char limit "
                    f"(resolved len={resolved_len}): {entry}",
                )

        lines = [
            "KPL/MK",
            "",
            "\\begindata",
            path_block,
            "KERNELS_TO_LOAD = (",
            *[f"    '{k}'" for k in rel_kernels],
            ")",
            "\\begintext",
            "",
        ]
        out.write_text("\n".join(lines), encoding="utf-8")
        log.info(
            f"Snapshotted {len(kernels)} kernels to {out}"
            + (f" (common prefix: {common_prefix})" if common_prefix else ""),
        )
        return out

    def kernel_provenance(self, source: str = "manager") -> dict[str, Any]:
        """
        Report the minimum kernel set needed to document/reproduce the
        current state -- e.g. when generating PDS4 labels for processing
        performed on downlinked data.

        Rather than flattening every individual kernel file, this cites the
        metakernel itself (its real, resolved filename -- with the SKD
        version baked in when pinned) plus only the kernels added on top of
        it (e.g. via :meth:`add_kernel`): the minimum information needed to
        reproduce the exact kernel set that was active.

        Parameters
        ----------
        source:
            ``'manager'`` (default) reports this manager's own bookkeeping
            -- requires :meth:`load_kernels` to have been called and this
            manager to still be the active pool owner. ``'pool'`` instead
            inspects the live SPICE pool directly, independent of any
            particular manager -- useful when kernels were furnished by
            another manager instance or via raw ``spiceypy`` calls. If
            :attr:`is_dirty` is ``True``, ``'manager'`` reports the
            *intended* set (per :meth:`load_kernels`/:meth:`add_kernel`),
            which may not exactly match the live pool; use ``'pool'`` to
            see the actual current state instead.

        Returns
        -------
        dict
            ``metakernel``: str path to the resolved metakernel file, or
            ``None`` if none is identifiable.
            ``spacecraft``, ``mk``, ``version``, ``resolved_at``: the
            request that produced it (``source='pool'``: always ``None``,
            since the live pool carries no such metadata). ``resolved_at``
            is an ISO-8601 UTC timestamp of when this manager last
            resolved the metakernel -- the closest thing to a version
            string when ``version='latest'`` was used.
            ``extra_kernels``: sorted list of kernel paths present beyond
            what the metakernel itself specifies.
            ``all_kernels``: sorted list of every kernel currently loaded.

        Raises
        ------
        ValueError
            If *source* is not ``'manager'`` or ``'pool'``.
        RuntimeError
            ``source='manager'``: if :meth:`load_kernels` hasn't been
            called, or this manager is no longer the active pool owner.
        """
        if source == "manager":
            return self._kernel_provenance_from_manager()
        if source == "pool":
            return self._kernel_provenance_from_pool()
        raise ValueError(f"source must be 'manager' or 'pool', got {source!r}")

    def _kernel_provenance_from_manager(self) -> dict[str, Any]:
        with _pool_lock:
            if self._expected_kernels is None:
                raise RuntimeError(
                    "load_kernels() has not been called; no kernel set to document.",
                )
            if not self.is_active:
                raise RuntimeError(
                    "This manager is no longer the active pool owner (another "
                    "manager loaded its kernels after this one). Call "
                    "load_kernels() again to reclaim the pool.",
                )
            metakernel_kernels = self._metakernel_kernels or frozenset()
            extra = sorted(self._expected_kernels - metakernel_kernels)
            resolved_at = (
                datetime.fromtimestamp(
                    self._resolved_at, tz=timezone.utc,
                ).isoformat(timespec="seconds")
                if self._resolved_at is not None
                else None
            )
            return {
                "metakernel": (
                    str(self._resolved_mk_path) if self._resolved_mk_path else None
                ),
                "spacecraft": self._spacecraft,
                "mk": self._mk,
                "version": self._version,
                "resolved_at": resolved_at,
                "extra_kernels": extra,
                "all_kernels": sorted(self._expected_kernels),
            }

    def _kernel_provenance_from_pool(self) -> dict[str, Any]:
        with _pool_lock:
            n = spiceypy.ktotal("ALL")
            meta_paths: list[str] = []
            all_kernels: list[str] = []
            for i in range(n):
                fname, ftype, _source, _handle = spiceypy.kdata(i, "ALL")
                if ftype.strip().upper() == "META":
                    meta_paths.append(fname)
                else:
                    all_kernels.append(fname)

        if len(meta_paths) > 1:
            log.debug(
                f"kernel_provenance: {len(meta_paths)} META entries in pool, "
                "using the most recently furnished one",
            )
        metakernel = meta_paths[-1] if meta_paths else None

        metakernel_kernels: frozenset[str] = frozenset()
        if metakernel is not None:
            metakernel_kernels = _kernels_referenced_by_metakernel(Path(metakernel))

        extra = sorted(set(all_kernels) - metakernel_kernels)
        return {
            "metakernel": metakernel,
            "spacecraft": None,
            "mk": None,
            "version": None,
            "resolved_at": None,
            "extra_kernels": extra,
            "all_kernels": sorted(all_kernels),
        }

    def unload_kernels(self) -> None:
        """Unload this manager and restore the pre-load SPICE pool snapshot."""
        global _active_manager_ref
        with _pool_lock:
            has_snapshot = bool(self._saved_kernels)

            if has_snapshot:
                log.debug("Restoring SPICE kernel pool to pre-load snapshot")
                spiceypy.kclear()
                for fname in self._saved_kernels:
                    if Path(fname).exists():
                        log.debug(f"Re-furnishing saved kernel: {fname}")
                        spiceypy.furnsh(fname)
                    else:
                        log.warning(
                            f"Saved kernel no longer exists on disk, skipping: {fname}",
                        )
                self._saved_kernels = []
            elif self._loaded_mk_path is not None:
                # Fallback path for managers created before pool snapshots were used.
                log.info(f"Unloading metakernel from temp file {self._loaded_mk_path}")
                spiceypy.unload(self._loaded_mk_path.as_posix())

            if self._loaded_mk_path is not None:
                try:
                    self._loaded_mk_path.unlink()
                except OSError:
                    log.warning(
                        f"Could not delete temp metakernel file {self._loaded_mk_path}",
                    )
                self._loaded_mk_path = None
                self._localized_mk_path = None  # invalidate resolved_mk cache
            elif self._localized_mk_path is not None:
                # resolved_mk was accessed without load_kernels() —
                # clean up the temp file.
                try:
                    self._localized_mk_path.unlink(missing_ok=True)
                except OSError:
                    pass
                self._localized_mk_path = None

            self._expected_kernels = None
            self._metakernel_kernels = None

            # Restore previous active manager reference for nested/manual stacking.
            if self._prior_active_ref is not None:
                _active_manager_ref = self._prior_active_ref
                self._prior_active_ref = None
            elif _active_manager_ref is not None and _active_manager_ref() is self:
                _active_manager_ref = None

    def __enter__(self) -> "QuickSpiceManager":
        """Load kernels and remember current pool so __exit__ can restore it.

        Holds the module-level CSPICE pool lock for the *entire* duration of
        the ``with`` block (released in :meth:`__exit__`), so the whole
        load -> use -> unload lifecycle is one critical section with respect
        to other threads in this process. This is a per-process lock only;
        it does not and should not coordinate across OS processes, since each
        process owns an independent CSPICE kernel pool.
        """
        _pool_lock.acquire()
        try:
            self._saved_kernels = self._snapshot_pool_entries()
            log.debug(
                f"Snapshotted {len(self._saved_kernels)} kernels "
                "from current SPICE pool",
            )
            self.load_kernels()
        except BaseException:
            _pool_lock.release()
            raise
        return self

    def __exit__(self, *_exc) -> bool:
        """Unload kernels, restore the SPICE pool, and release the pool lock."""
        try:
            self.unload_kernels()
        finally:
            _pool_lock.release()
        return False

    def get_tour_config(
        self,
        target: str = "Jupiter",
        instrument: str | None = "JANUS",
        kernels: "list[str] | None" = None,
    ):
        """Download kernels via ESA FTP and return a ``TourConfig`` using local files.

        This manager itself carries no target/instrument/kernels-subset state
        -- those are ``planetary_coverage.TourConfig``'s concerns, not this
        package's, so they're accepted here as call-time parameters rather
        than stored on the instance. Use this method (instead of the
        :attr:`tour_config` property) when you need a target/instrument other
        than the defaults.

        Requires the ``planetary-coverage`` optional extra::

            pip install quick-spice-manager[planetary-coverage]

        Parameters
        ----------
        target:
            Target body passed to ``TourConfig`` (e.g. ``'Jupiter'``).
        instrument:
            Instrument passed to ``TourConfig``, or ``None`` for no instrument.
        kernels:
            Optional subset of kernel names to restrict ``TourConfig`` to.

        Raises
        ------
        ImportError
            If ``planetary_coverage`` is not installed.
        """
        try:
            from planetary_coverage import TourConfig
        except ImportError as exc:
            raise ImportError(
                "planetary_coverage is not installed. "
                "Install it with: pip install quick-spice-manager[planetary-coverage]",
            ) from exc

        resolved = self._resolve_metakernel()
        return TourConfig(
            spacecraft=self._spacecraft,
            kernels_dir=self._kernels_dir.as_posix(),
            download_kernels=False,
            mk=resolved.as_posix(),
            version=self._version,
            target=target,
            instrument="none" if instrument is None else instrument,
            load_kernels=True,
            kernels=kernels,
        )

    @property
    def tour_config(self):
        """``TourConfig`` built with the default target/instrument.

        .. deprecated::
            Kept only for backward compatibility with code that accesses
            ``.tour_config`` as a plain attribute. Emits a
            ``DeprecationWarning`` -- use :meth:`get_tour_config` instead
            (same defaults, called explicitly: ``sm.get_tour_config()``).

        Requires the ``planetary-coverage`` optional extra::

            pip install quick-spice-manager[planetary-coverage]
        """
        warnings.warn(
            "QuickSpiceManager.tour_config (property) is deprecated and will "
            "be removed in a future release. Use get_tour_config() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.get_tour_config()

    @property
    def user_kernels_cache_directory(self) -> Path:
        """
        The user default kernels cache directory
        """
        kd = get_user_kernels_cache_directory().joinpath(self._spacecraft.lower())
        kd.mkdir(parents=True, exist_ok=True)
        return kd

    @property
    def metakernels(self):
        """List available metakernels for the current spacecraft via ESA FTP."""
        return list_metakernels_via_ftp(self._spacecraft)

    @property
    def cache_size(self):
        """
        Get the size of the kernels cache directory
        """

        s = sum(
            f.stat().st_size
            for f in self.user_kernels_cache_directory.glob("**/*")
            if f.is_file()
        )
        return sizeof_fmt(s)

    def clear_cache(self):
        """
        Clear the cache.

        Serialized (via a cross-process file lock living *outside* the
        cache directory) against other ``clear_cache()`` calls from other
        processes. This does **not** coordinate against concurrently
        in-flight downloads from other processes into the same cache
        directory — avoid calling ``clear_cache()`` while other processes
        may be actively loading kernels from this cache.
        """
        cache_dir = self.user_kernels_cache_directory
        log.warning(
            f"Clearing cache at {cache_dir}. \
                It will re-download the kernels at next usage",
        )

        with filelock.FileLock(str(get_cache_lock_path(cache_dir)), timeout=300):
            shutil.rmtree(cache_dir, ignore_errors=True)
            cache_dir.mkdir(parents=True, exist_ok=True)

    def _repr_html_(self) -> str:
        """Rich HTML representation for Jupyter notebooks."""
        # --- configuration rows ---
        cfg_rows = "".join(
            f"<tr><td style='padding:3px 8px;font-weight:bold;white-space:nowrap'>{k}</td>"
            f"<td style='padding:3px 8px;font-family:monospace'>{v}</td></tr>"
            for k, v in [
                ("spacecraft", self._spacecraft),
                ("metakernel", self._mk),
                ("kernels_dir", self._kernels_dir),
                ("version", self._version),
                ("exclusive", self._exclusive),
            ]
        )

        # --- pool status ---
        with _pool_lock:
            n_pool = spiceypy.ktotal("ALL")
        loaded = self._loaded_mk_path is not None
        active = self.is_active
        dirty = self.is_dirty  # None | True | False

        if loaded:
            if not active:
                status_color = "#7f8c8d"
                status_text = "&#9676; superseded"
            elif dirty is True:
                status_color, status_text = "#e67e00", "&#9679; loaded (dirty)"
            else:
                status_color, status_text = "#2e7d32", "&#9679; loaded (clean)"
        else:
            status_color, status_text = "#888", "&#9675; not loaded"

        dirty_badge = ""
        if loaded and not active:
            dirty_badge = (
                "<br><span style='font-size:0.85em;color:#7f8c8d'>"
                "Pool ownership passed to another manager &mdash; "
                "call <code>load_kernels()</code> to reclaim.</span>"
            )
        elif dirty is True:
            current = self._pool_files()
            extras = current - self._expected_kernels
            missing = self._expected_kernels - current
            dirty_badge = (
                f"<br><span style='font-size:0.85em;color:#e67e00'>"
                f"+{len(extras)} extra, -{len(missing)} missing vs metakernel</span>"
            )

        # --- kernel list ---
        pool_files = self._pool_files()
        is_expected = (
            (lambda f: f in self._expected_kernels) if self._expected_kernels is not None
            else (lambda f: True)
        )
        kernel_rows = ""
        for f in sorted(pool_files):
            mark = (
                "" if self._expected_kernels is None
                else ("&#10003;" if is_expected(f) else "<b style='color:#e67e00'>&#43;</b>")
            )
            kernel_rows += (
                f"<tr><td style='padding:2px 6px;font-family:monospace;font-size:0.85em'>"
                f"{f}</td>"
                f"<td style='padding:2px 6px;text-align:center'>{mark}</td></tr>"
            )
        if self._expected_kernels is not None:
            for f in sorted(self._expected_kernels - pool_files):
                kernel_rows += (
                    f"<tr style='color:#c0392b'>"
                    f"<td style='padding:2px 6px;font-family:monospace;font-size:0.85em'>{f}</td>"
                    f"<td style='padding:2px 6px;text-align:center'><b>&#8722;</b></td></tr>"
                )

        kernel_legend = ""
        if self._expected_kernels is not None:
            kernel_legend = (
                "<div style='font-size:0.8em;color:#555;margin-top:4px'>"
                "&#10003; from metakernel &nbsp; <b style='color:#e67e00'>&#43;</b> extra &nbsp; "
                "<b style='color:#c0392b'>&#8722;</b> missing</div>"
            )

        kernel_section = (
            f"<details><summary style='cursor:pointer;font-weight:bold'>"
            f"SPICE pool &mdash; {n_pool} kernel(s)</summary>"
            f"<table style='border-collapse:collapse;margin-top:6px'><thead>"
            f"<tr><th style='padding:2px 6px;text-align:left'>path</th>"
            f"<th style='padding:2px 6px'>&#10003;</th></tr></thead>"
            f"<tbody>{kernel_rows}</tbody></table>"
            f"{kernel_legend}</details>"
        )

        return (
            f"<div style='border:1px solid #ccc;border-radius:6px;padding:10px;"
            f"font-family:sans-serif;max-width:800px'>"
            f"<b style='font-size:1.05em'>SpiceManager</b>"
            f"<span style='float:right;color:{status_color}'>{status_text}</span>"
            f"{dirty_badge}"
            f"<table style='border-collapse:collapse;margin:8px 0'>{cfg_rows}</table>"
            f"{kernel_section}"
            f"</div>"
        )

    @property
    def config(self) -> pd.DataFrame:
        """
        Get the current configuration as a pandas DataFrame for display in
        Jupyter notebooks.

        Resolved entirely from this manager's own state (triggering the
        same download-if-needed resolution as :attr:`resolved_mk` to report
        the real metakernel path) -- unlike :attr:`tour_config`, this does
        not build a ``TourConfig`` and does not require the
        ``planetary-coverage`` optional extra. Target/instrument are
        deliberately not included here: they're ``TourConfig``'s concerns,
        not this manager's -- see :meth:`get_tour_config`.
        """
        resolved = self._resolve_metakernel()
        table = pd.DataFrame()
        table["key"] = [
            "spacecraft",
            "version",
            "metakernel",
            "kernels_dir",
        ]
        values: list[Any] = [
            self._spacecraft,
            self._version,
            str(resolved),
            self._kernels_dir,
        ]
        table["value"] = values

        table.set_index("key", inplace=True)
        return table



SpiceManager = QuickSpiceManager  # deprecated alias — warning issued via __init__.__getattr__
