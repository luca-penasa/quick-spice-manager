"""
Manages SPICE kernels for spacecraft operations.

Provides standalone kernel loading, context manager support for temporary
kernel pools, and optional integration with planetary_coverage.TourConfig.
"""

import os
import re
import shutil
import weakref
from collections.abc import Iterable
from pathlib import Path
from tempfile import NamedTemporaryFile

import pandas as pd
import spiceypy
from attrs import define, field
from dotenv import load_dotenv
from loguru import logger as log

from .dirs import get_user_kernels_cache_directory
from .ftp import download_kernels_via_ftp, list_metakernels_via_ftp

# NAIF constraint: maximum number of characters in a SPICE PATH_VALUES entry.
# See planetary_coverage.spice.metakernel.KERNEL_MAX_LENGTH.
_KERNEL_MAX_LENGTH = 255

# Tracks the most recently active SpiceManager (the one that last called
# load_kernels).  A weakref is used so that managers which go out of scope
# are collected normally without keeping them alive.
_active_manager_ref: "weakref.ref | None" = None


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

        tc = SpiceManager().tour_config
        # install with: pip install quick-spice-manager[planetary-coverage]
    """

    # _tour_config: TourConfig = field(default=None)
    _spacecraft: str = field(default="JUICE")
    _download_kernels: bool = field(default=True)
    _version: str = field(default="latest")
    _target: str = field(default="Jupiter")
    _instrument: str = field(
        default="JANUS",
        converter=lambda x: "none" if x is None else x,
    )
    _mk: str = field(default="plan")
    _kernels_dir: Path | None = field(
        default=None,
        converter=lambda x: Path(x) if x is not None else None,
    )
    _kernels = field(default=None)
    _exclusive: bool = field(default=True)

    resolved_mk: Path | None = field(default=None, converter=lambda x: Path(x) if x is not None else None, init=False)
    _saved_kernels: list[str] = field(factory=list, init=False)
    _loaded_mk_path: Path | None = field(default=None, init=False)
    _expected_kernels: frozenset[str] | None = field(default=None, init=False)
    # Stores the active manager ref that was in place when __enter__ was called,
    # so __exit__ can restore it (correct LIFO ordering for nested context managers).
    _prior_active_ref: "weakref.ref | None" = field(default=None, init=False)


        

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
                "Pass the metakernel as a keyword argument."
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
        """Locate the metakernel file, downloading it via FTP if it is not already local."""
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
                f"(len={len(kernels_dir_str)}): {kernels_dir_str}"
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
        result: list[str] = []
        for i in range(spiceypy.ktotal("ALL")):
            fname, ftype, _source, _handle = spiceypy.kdata(i, "ALL")
            if ftype.strip().upper() != "META":
                result.append(fname)
        return frozenset(result)

    def _snapshot_pool_entries(self) -> list[str]:
        """Snapshot current non-META kernel entries that still exist on disk."""
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
                log.debug(f"Snapshot pool: skipping non-existent pool entry: {fname}")
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
        if self._expected_kernels is None:
            raise RuntimeError(
                "load_kernels() has not been called; no expected state to clean to."
            )
        if not self.is_active:
            raise RuntimeError(
                "This manager is no longer the active pool owner (another manager "
                "loaded its kernels after this one). Call load_kernels() again to "
                "reclaim the pool, or use 'with SpiceManager(...) as sm:' instead."
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
                    f"clean_pool: expected kernel no longer on disk, skipping: {fname}"
                )

        if unloaded or restored:
            log.debug(
                f"clean_pool: unloaded {len(unloaded)}, restored {len(restored)} kernels"
            )
        return {"unloaded": unloaded, "restored": restored}

    def load_kernels(self) -> Path:
        """Download (if needed), localize, and furnish the metakernel.

        Returns the resolved (original, non-temp) metakernel path.
        """
        if self._loaded_mk_path is not None:
            log.warning(
                "load_kernels() called while this manager is already loaded; "
                "unloading current state first."
            )
            self.unload_kernels()

        # If we are superseding another SpiceManager that owns the pool,
        # preserve its expected kernel set so unload_kernels() can restore it.
        # This avoids an extra live pool scan in load_kernels().
        global _active_manager_ref
        active_mgr = _active_manager_ref() if _active_manager_ref is not None else None
        if not self._saved_kernels:
            if (
                active_mgr is not None
                and active_mgr is not self
                and active_mgr._expected_kernels is not None
            ):
                self._saved_kernels = sorted(active_mgr._expected_kernels)
                log.debug(
                    "Captured pre-load pool from prior active manager "
                    f"({len(self._saved_kernels)} kernels)"
                )
            else:
                self._saved_kernels = []

        resolved = self._resolve_metakernel()
        self.resolved_mk = resolved
        tmp_path = self._localize_metakernel(resolved)

        if self._exclusive:
            log.debug("Exclusive mode: clearing pool before loading metakernel")
            spiceypy.kclear()

        log.info(f"Furnishing metakernel {resolved} (localized temp: {tmp_path})")
        spiceypy.furnsh(tmp_path.as_posix())
        self._loaded_mk_path = tmp_path
        self._expected_kernels = self._pool_files()

        # Save who was active before taking ownership (for unload restore).
        self._prior_active_ref = _active_manager_ref
        _active_manager_ref = weakref.ref(self)
        return resolved

    def add_kernel(
        self, path: "Path | str | Iterable[Path | str]"
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

        if self._expected_kernels is not None and not self.is_active:
            raise RuntimeError(
                "This manager is not the active pool owner. "
                "Call load_kernels() to reclaim the pool before adding kernels."
            )

        # Resolve and validate all paths before furnishing any (all-or-nothing)
        resolved: list[Path] = []
        for p in paths:
            rp = Path(p).resolve()
            if not rp.exists():
                raise FileNotFoundError(f"Kernel file not found: {rp}")
            resolved.append(rp)

        for rp in resolved:
            log.info(f"Furnishing additional kernel: {rp}")
            spiceypy.furnsh(str(rp))

        # Update expected set so additions are considered intentional (not dirty).
        if self._expected_kernels is not None:
            self._expected_kernels = self._expected_kernels | {str(rp) for rp in resolved}

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

        n = spiceypy.ktotal("ALL")
        kernels: list[str] = []
        for i in range(n):
            fname, ftype, _source, _handle = spiceypy.kdata(i, "ALL")
            if ftype.strip().upper() == "META":
                continue
            if len(fname) > _KERNEL_MAX_LENGTH:
                raise ValueError(
                    f"Kernel path exceeds NAIF {_KERNEL_MAX_LENGTH}-char limit "
                    f"(len={len(fname)}): {fname}"
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
                    f"(resolved len={resolved_len}): {entry}"
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
            + (f" (common prefix: {common_prefix})" if common_prefix else "")
        )
        return out

    def unload_kernels(self) -> None:
        """Unload this manager and restore the pre-load SPICE pool snapshot."""
        has_snapshot = bool(self._saved_kernels)

        if has_snapshot:
            log.debug("Restoring SPICE kernel pool to pre-load snapshot")
            spiceypy.kclear()
            for fname in self._saved_kernels:
                if Path(fname).exists():
                    log.debug(f"Re-furnishing saved kernel: {fname}")
                    spiceypy.furnsh(fname)
                else:
                    log.warning(f"Saved kernel no longer exists on disk, skipping: {fname}")
            self._saved_kernels = []
        elif self._loaded_mk_path is not None:
            # Fallback path for managers created before pool snapshots were used.
            log.info(f"Unloading metakernel from temp file {self._loaded_mk_path}")
            spiceypy.unload(self._loaded_mk_path.as_posix())

        if self._loaded_mk_path is not None:
            try:
                self._loaded_mk_path.unlink()
            except OSError:
                log.warning(f"Could not delete temp metakernel file {self._loaded_mk_path}")
            self._loaded_mk_path = None

        self.resolved_mk = None
        self._expected_kernels = None

        # Restore previous active manager reference for nested/manual stacking.
        global _active_manager_ref
        if self._prior_active_ref is not None:
            _active_manager_ref = self._prior_active_ref
            self._prior_active_ref = None
        elif _active_manager_ref is not None and _active_manager_ref() is self:
            _active_manager_ref = None

    def __enter__(self) -> "QuickSpiceManager":
        """Load kernels and remember current pool so __exit__ can restore it."""
        self._saved_kernels = self._snapshot_pool_entries()
        log.debug(f"Snapshotted {len(self._saved_kernels)} kernels from current SPICE pool")
        self.load_kernels()
        return self

    def __exit__(self, *_exc) -> bool:
        """Unload kernels and restore the SPICE pool to the pre-entry state."""
        self.unload_kernels()
        return False

    @property
    def metakernel(self):
        return self.tour_config.kernels[0]

    @property
    def tour_config(self):
        """Download kernels via ESA FTP and return a ``TourConfig`` using local files.

        Requires the ``planetary-coverage`` optional extra::

            pip install quick-spice-manager[planetary-coverage]

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
                "Install it with: pip install quick-spice-manager[planetary-coverage]"
            ) from exc

        resolved = self._resolve_metakernel()
        self.resolved_mk = resolved
        return TourConfig(
            spacecraft=self._spacecraft,
            kernels_dir=self._kernels_dir.as_posix(),
            download_kernels=False,
            mk=resolved.as_posix(),
            version=self._version,
            target=self._target,
            instrument=self._instrument,
            load_kernels=True,
            kernels=self._kernels,
        )

    @property
    def user_kernels_cache_directory(self) -> Path:
        """
        The user default kernels cache directory
        """
        kd = get_user_kernels_cache_directory().joinpath(self._spacecraft.lower())
        kd.mkdir(parents=True, exist_ok=True)
        return kd

    def coverage_table(self):
        """
        Get the coverage table for the current spacecraft and the different metakernels
        """
        return details_coverage_from_metakernels2(
            kernels_dir=self.user_kernels_cache_directory.as_posix(),
            mission=self._spacecraft,
            version=self._version,
        )

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
        Clear the cache
        """
        log.warning(
            f"Clearing cache at {self.user_kernels_cache_directory}. \
                It will re-download the kernels at next usage",
        )

        shutil.rmtree(self.user_kernels_cache_directory)
        self.user_kernels_cache_directory.mkdir(parents=True, exist_ok=True)

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
        Get the current configuration as a pandas DataFrame for display in Jupyter notebooks.
        """
        tour = self.tour_config  # get a tour config with current configuration
        table = pd.DataFrame()
        table["key"] = [
            "spacecraft",
            "skd_version",
            "target",
            "instrument",
            "metakernel",
            "kernels_dir",
        ]
        table["value"] = [
            tour.spacecraft,
            tour.skd_version,
            tour.target,
            tour.instrument,
            tour.mk,
            self._kernels_dir,
        ]

        table.set_index("key", inplace=True)
        return table



SpiceManager = QuickSpiceManager  # alias for backwards compatibility