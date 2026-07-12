"""
FTP fallback for SPICE kernel downloads.

Used when planetary_coverage cannot access its Bitbucket source
(e.g. HTTP 401/403 due to password protection). Downloads metakernels
and referenced kernel files anonymously from the ESA public FTP server
at ftp://spiftp.esac.esa.int/data/SPICE/.
"""

from __future__ import annotations

import ftplib
import json
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import filelock
from loguru import logger as log
from tqdm import tqdm

from .dirs import (
    get_ftp_connection_lock_dir,
    get_metakernel_listing_cache_dir,
    get_resolution_cache_path,
)
from .locking import bounded_slot

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

# Default number of parallel FTP connections for kernel downloads within a
# single download_kernels_via_ftp() call.
# Keep this conservative; ESA FTP limits simultaneous connections per IP.
_N_PARALLEL_DOWNLOADS = 4

# How long to wait to acquire a per-file download lock before giving up.
# Generous, since a peer's download of a large kernel can legitimately take
# a while; mainly a safety net against a genuinely wedged peer process (a
# crashed process releases its OS-level lock automatically, so this is
# belt-and-braces rather than a correctness requirement).
_LOCK_TIMEOUT_SECONDS = 300

# Hard cap on how many connections to the ESA FTP server may be open *at the
# same time*, across every QuickSpiceManager instance and OS process for this
# user -- not just within one download_kernels_via_ftp() call. Per-file
# locking (see _locked_download) only prevents duplicate downloads of the
# *same* file; without this, N independent managers/processes could each
# open their own _N_PARALLEL_DOWNLOADS connections at once, and ESA's FTP
# server limits simultaneous connections per IP.
_MAX_CONCURRENT_FTP_CONNECTIONS = 4

# How often to re-check for a free connection slot while waiting.
_SLOT_POLL_INTERVAL_SECONDS = 0.1

# How long to wait for a free connection slot before giving up.
_CONNECTION_SLOT_TIMEOUT_SECONDS = 300

# ---------------------------------------------------------------------------
# FTP server and mission path constants
# ---------------------------------------------------------------------------

_FTP_HOST = "spiftp.esac.esa.int"

# Maps planetary_coverage mission names (upper) to the exact FTP directory path.
# Casing follows the FTP server layout.
_MISSION_FTP_BASE: dict[str, str] = {
    "BEPICOLOMBO": "/data/SPICE/BEPICOLOMBO/",
    "COMET-INTERCEPTOR": "/data/SPICE/COMET-INTERCEPTOR/",
    "ENVISION": "/data/SPICE/ENVISION/",
    "EXOMARS2016": "/data/SPICE/ExoMars2016/",
    "EXOMARSRSP": "/data/SPICE/ExoMarsRSP/",
    "GAIA": "/data/SPICE/GAIA/",
    "HERA": "/data/SPICE/HERA/",
    "HUYGENS": "/data/SPICE/HUYGENS/",
    "INTEGRAL": "/data/SPICE/INTEGRAL/",
    "JUICE": "/data/SPICE/JUICE/",
    "JWST": "/data/SPICE/JWST/",
    "MARS-EXPRESS": "/data/SPICE/MARS-EXPRESS/",
    "ROSETTA": "/data/SPICE/ROSETTA/",
    "SMART-1": "/data/SPICE/SMART-1/",
    "SOLAR-ORBITER": "/data/SPICE/SOLAR-ORBITER/",
    "VENUS-EXPRESS": "/data/SPICE/VENUS-EXPRESS/",
}

# Common mission aliases that planetary_coverage accepts but FTP uses canonical names.
_MISSION_ALIASES: dict[str, str] = {
    "MPO": "BEPICOLOMBO",
    "MTM": "BEPICOLOMBO",
    "MMO": "BEPICOLOMBO",
    "TGO": "EXOMARS2016",
    "EDM": "EXOMARS2016",
    "MEX": "MARS-EXPRESS",
    "BEAGLE2": "MARS-EXPRESS",
    "SOLO": "SOLAR-ORBITER",
    "VEX": "VENUS-EXPRESS",
    "CASP": "HUYGENS",
}


def _ftp_connection_slot() -> AbstractContextManager[None]:
    """Block until one of a small, fixed number of cross-process connection
    "slots" is free, then hold it for the duration of the ``with`` block.

    This bounds the total number of simultaneous connections to the ESA FTP
    server across every ``QuickSpiceManager`` instance and OS process for
    this user, regardless of how many are downloading at once -- the
    per-file lock in :func:`_locked_download` only prevents duplicate
    downloads of the *same* file, it does nothing to cap how many
    *different* connections are open concurrently, which is what this
    guards instead.
    """
    return bounded_slot(
        get_ftp_connection_lock_dir(),
        _MAX_CONCURRENT_FTP_CONNECTIONS,
        timeout=_CONNECTION_SLOT_TIMEOUT_SECONDS,
        poll_interval=_SLOT_POLL_INTERVAL_SECONDS,
    )


def _canonical_mission(mission: str) -> str:
    """Return the canonical upper-case mission name, resolving aliases."""
    upper = mission.upper()
    return _MISSION_ALIASES.get(upper, upper)


def _ftp_base(mission: str) -> str:
    """Return the FTP base path for *mission*. Raises ValueError for unknowns."""
    canonical = _canonical_mission(mission)
    try:
        return _MISSION_FTP_BASE[canonical]
    except KeyError:
        raise ValueError(
            f"No FTP path known for mission '{mission}'. "
            f"Supported: {sorted(_MISSION_FTP_BASE)}",
        ) from None


# ---------------------------------------------------------------------------
# Offline / connectivity-failure fallback helpers
#
# ftplib.all_errors == (ftplib.Error, OSError, EOFError) -- the tuple ftplib
# itself recommends catching to cover connection/protocol/socket failures.
# FileNotFoundError is a subclass of OSError but is raised *deliberately* by
# this module (see _resolve_tm_on_ftp) for a legitimate "no such kernel/
# version" answer from a server that responded just fine -- that is not a
# connectivity problem, so callers re-raise it instead of falling back.
# ---------------------------------------------------------------------------


def _read_json_cache(path: Path) -> dict[str, Any]:
    """Best-effort read of a small JSON cache file.

    Returns ``{}`` if the file is missing, unreadable, or corrupt -- this is
    an optimization/fallback cache, not a source of truth, so a bad cache
    file should never itself cause a failure.
    """
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data


def _write_json_cache(path: Path, data: dict[str, Any]) -> None:
    """Atomically write *data* as JSON to *path* (temp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f"{path.name}.", suffix=".part",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _tm_kernels_missing(tm_path: Path, kernels_dir: Path) -> list[str]:
    """Return the relative paths of any kernel referenced by *tm_path* that
    are not present under *kernels_dir*.

    An empty list means *tm_path* is fully usable offline -- every kernel it
    references is already on disk.
    """
    try:
        tm_content = tm_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ["<the metakernel file itself is unreadable>"]
    rel_paths = _parse_mk_kernel_paths(tm_content)
    return [rel for rel in rel_paths if not (kernels_dir / rel).exists()]


def _resolution_cache_key(spacecraft: str, mk: str, version: str) -> str:
    """Stable cache key for a (spacecraft, mk, version) resolution."""
    canonical = _canonical_mission(spacecraft)
    mk_clean = mk.removesuffix(".tm").lower()
    return f"{canonical}|{mk_clean}|{version or 'latest'}"


def _format_checked_at(checked_at: float | None) -> str:
    """Human-readable timestamp for log messages, tolerant of missing data."""
    if checked_at is None:
        return "an earlier run"
    when = datetime.fromtimestamp(checked_at, tz=timezone.utc)
    return when.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Public: list available metakernels via FTP
# ---------------------------------------------------------------------------


def list_metakernels_via_ftp(mission: str) -> list[str]:
    """
    Return the metakernel stem names available for *mission* on the ESA FTP.

    Returns filenames with the ``.tm`` suffix stripped, matching the format
    that ``planetary_coverage.ESA_MK[mission].mks`` would normally return.

    If the ESA FTP server cannot be reached, falls back to the last
    successfully fetched listing for *mission* (with a warning logged)
    rather than raising -- this may not reflect newly published
    metakernels. If no listing has ever been cached, the original
    connectivity error is raised, wrapped in a :class:`ConnectionError`.

    Parameters
    ----------
    mission:
        Mission name understood by planetary_coverage (e.g. ``'JUICE'``).
    """
    base = _ftp_base(mission)
    mk_dir = f"{base}kernels/mk/"
    log.info(f"FTP: listing metakernels for {mission} from {_FTP_HOST}{mk_dir}")

    cache_path = (
        get_metakernel_listing_cache_dir() / f"{_canonical_mission(mission)}.json"
    )

    try:
        with _ftp_connection_slot():
            ftp = ftplib.FTP(_FTP_HOST)  # noqa: S321 - public ESA FTP, anonymous login
            try:
                ftp.login()
                entries = ftp.nlst(mk_dir)
            finally:
                ftp.quit()
    except FileNotFoundError:
        raise  # a genuine "no such directory" answer, not a connectivity issue
    except ftplib.all_errors as exc:
        log.warning(f"FTP: could not reach {_FTP_HOST} ({exc})")
        cached = _read_json_cache(cache_path)
        if cached.get("entries"):
            when = _format_checked_at(cached.get("checked_at"))
            log.warning(
                f"FTP: offline fallback -- using metakernel listing for "
                f"{mission!r} cached from {when}; "
                "this may not reflect newly published metakernels.",
            )
            return list(cached["entries"])
        raise ConnectionError(
            f"Could not reach the ESA FTP server ({exc}) and no cached "
            f"metakernel listing is available for mission {mission!r}.",
        ) from exc

    result = [Path(e).stem for e in entries if e.lower().endswith(".tm")]
    _write_json_cache(cache_path, {"entries": result, "checked_at": time.time()})
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_tm_on_ftp(
    ftp: ftplib.FTP, mission: str, mk: str, base: str, version: str = "latest",
) -> str:
    """
    Map a metakernel shortcut/name to the full FTP path of the ``.tm`` file.

    Resolution strategy
    -------------------
    1. Resolve the **unversioned** TM stem in ``kernels/mk/`` via
       spacecraft-prefix or fuzzy shortest-name matching.
       (e.g. ``'tr03'`` → ``juice_s011_tr03.tm`` → stem ``juice_s011_tr03``)

    2. When *version* is a specific tag (e.g. ``'v461_20260121_001'``):

       a. Look for ``{stem}_{version}.tm`` in ``kernels/mk/``
       b. Look for ``{stem}_{version}.tm`` in ``kernels/mk/former_versions/``
       c. If still not found, warn and fall back to the unversioned TM.

    3. Without a specific version, return the unversioned TM directly.

    Parameters
    ----------
    ftp:
        An already-logged-in ``ftplib.FTP`` connection.
    mission:
        Canonical mission name (e.g. ``'JUICE'``).
    mk:
        Metakernel shortcut or filename, with or without ``.tm``.
    base:
        FTP base path for the mission (e.g. ``'/data/SPICE/JUICE/'``).
    version:
        SKD version tag (e.g. ``'v461_20260121_001'``) or ``'latest'``.

    Returns
    -------
    str
        Full FTP path of the resolved ``.tm`` file.
    """
    mk_clean = mk.removesuffix(".tm")
    mk_dir = f"{base}kernels/mk/"

    try:
        entries = ftp.nlst(mk_dir)
    except ftplib.error_perm as exc:
        raise FileNotFoundError(
            f"Cannot list FTP directory {mk_dir}: {exc}",
        ) from exc

    tm_map: dict[str, str] = {
        Path(e).name.lower(): e for e in entries if e.lower().endswith(".tm")
    }

    # --- Step 1: resolve unversioned TM to obtain the full stem -------------
    spacecraft_prefix = _canonical_mission(mission).lower().replace("-", "_")
    unversioned_path: str | None = None

    for candidate in (
        f"{spacecraft_prefix}_{mk_clean}.tm",
        f"{mk_clean}.tm",
    ):
        if candidate in tm_map:
            unversioned_path = tm_map[candidate]
            log.debug(f"FTP: unversioned stem matched via '{candidate}'")
            break

    if unversioned_path is None:
        # Fuzzy: shortest .tm that contains mk_clean and looks unversioned
        # (i.e. the part after mk_clean in the stem has no "_v..." continuation)
        matches = [
            (name, path)
            for name, path in tm_map.items()
            if mk_clean in name
            and "_v" not in Path(name).stem.split(mk_clean, 1)[-1]
        ]
        if matches:
            matches.sort(key=lambda x: len(x[0]))
            chosen_name, chosen_path = matches[0]
            unversioned_path = chosen_path
            log.warning(f"FTP: fuzzy-matched '{mk}' to '{chosen_name}'")

    if unversioned_path is None:
        raise FileNotFoundError(
            f"Cannot find a metakernel matching '{mk}' (version={version}) on FTP "
            f"for mission '{mission}'. Available .tm files: {sorted(tm_map)}",
        )

    # --- Step 2: versioned lookup using the stem -----------------------------
    is_versioned = bool(version) and version.lower() not in ("latest", "all")
    if not is_versioned:
        return unversioned_path

    stem = Path(unversioned_path).stem.lower()  # e.g. "juice_s011_tr03"
    versioned_name = f"{stem}_{version}.tm"

    # Check top-level mk/
    if versioned_name in tm_map:
        log.debug(f"FTP: resolved versioned TM '{versioned_name}' in mk/")
        return tm_map[versioned_name]

    # Check former_versions/
    fv_dir = f"{base}kernels/mk/former_versions/"
    try:
        fv_entries = ftp.nlst(fv_dir)
        tm_map_fv: dict[str, str] = {
            Path(e).name.lower(): e for e in fv_entries if e.lower().endswith(".tm")
        }
        if versioned_name in tm_map_fv:
            log.debug(
                f"FTP: resolved versioned TM '{versioned_name}' in former_versions/",
            )
            return tm_map_fv[versioned_name]
    except ftplib.error_perm:
        log.debug(f"FTP: no former_versions directory at {fv_dir}")

    raise FileNotFoundError(
        f"Versioned metakernel '{versioned_name}' not found for mission '{mission}' "
        f"in mk/ or mk/former_versions/ on {_FTP_HOST}. "
        f"Use version='latest' to load the current unversioned TM, or check "
        f"available versions with QuickSpiceManager.metakernels.",
    )


def _parse_mk_kernel_paths(tm_content: str) -> list[str]:
    """
    Parse a SPICE metakernel text and return relative kernel paths.

    Extracts the ``PATH_SYMBOLS`` / ``PATH_VALUES`` pairs and the
    ``KERNELS_TO_LOAD`` list from the ``\\begindata`` … ``\\begintext``
    block, substitutes ``$SYMBOL`` prefixes, and returns relative paths
    (e.g. ``['ck/juice_sc_default_v02.bc', 'fk/juice_v45.tf', …]``).

    Note: the actual path substitution at runtime is performed by
    ``TourConfig`` via its ``kernels_dir`` parameter; here we only strip
    the ``$SYMBOL/`` prefix to obtain the relative path portion.
    """
    # Extract PATH_SYMBOLS  →  list of symbol names
    sym_match = re.search(
        r"PATH_SYMBOLS\s*=\s*\((.*?)\)", tm_content, re.DOTALL,
    )
    symbol_names: list[str] = (
        re.findall(r"'([^']+)'", sym_match.group(1)) if sym_match else []
    )

    # Extract KERNELS_TO_LOAD quoted entries
    ktl_match = re.search(
        r"KERNELS_TO_LOAD\s*=\s*\((.*?)\)", tm_content, re.DOTALL,
    )
    if not ktl_match:
        return []

    relative: list[str] = []
    for entry in re.findall(r"'([^']+)'", ktl_match.group(1)):
        path = entry
        for sym in symbol_names:
            prefix = f"${sym}/"
            if path.startswith(prefix):
                path = path[len(prefix):]
                break
        relative.append(path)

    return relative


def _ftp_download_file(ftp: ftplib.FTP, remote: str, local: Path) -> None:
    """Stream *remote* from an open FTP connection to *local*, atomically.

    Downloads into a temp file created in ``local``'s own parent directory
    (guaranteeing it's on the same filesystem) and only rename-replaces it
    into place once the transfer completes fully. This means a concurrent
    reader (in this process, a thread, or another OS process) never observes
    a partially-written file: it sees either the previous complete file (if
    any) or the new complete one, never a truncated in-between state. On any
    failure the temp file is removed and the exception re-raised.
    """
    local.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=local.parent, prefix=f"{local.name}.", suffix=".part",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "wb") as fh:
            ftp.retrbinary(f"RETR {remote}", fh.write)
        tmp_path.replace(local)  # atomic rename, same filesystem
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _locked_download(
    ftp: ftplib.FTP, remote: str, local: Path, *, force: bool = False,
) -> bool:
    """Download *remote* to *local* under a per-destination-file cross-process lock.

    Existence of *local* is re-checked *inside* the lock, so if two
    processes race to fetch the same missing file, whichever loses the race
    for the lock simply reuses the file the winner just wrote instead of
    downloading it again. Pass ``force=True`` to always re-download even if
    *local* already exists (used for ``version="latest"`` metakernels, which
    must be re-fetched on every call to pick up newly published kernels).

    Returns ``True`` if a download actually happened, ``False`` if the
    existing cached file was reused.

    The lock is per-destination-file (not a single global lock), so unrelated
    kernel files still download fully in parallel via the existing
    ``ThreadPoolExecutor`` — only two callers racing for the *exact same*
    file ever wait on each other.
    """
    local.parent.mkdir(parents=True, exist_ok=True)
    lock_path = local.parent / f"{local.name}.lock"
    with filelock.FileLock(str(lock_path), timeout=_LOCK_TIMEOUT_SECONDS):
        if not force and local.exists():
            return False
        _ftp_download_file(ftp, remote, local)
        return True


def _download_one_kernel(host: str, remote: str, local: Path) -> None:
    """
    Open a fresh anonymous FTP connection, download *remote* to *local*, then close.

    Each parallel worker calls this independently so threads never share a
    single ``ftplib.FTP`` object (FTP connections are not thread-safe). The
    connection is opened only once a cross-process connection slot is free,
    bounding total concurrent connections machine-wide.
    """
    with _ftp_connection_slot():
        ftp = ftplib.FTP(host)  # noqa: S321 - public ESA FTP, anonymous login
        try:
            ftp.login()
            _locked_download(ftp, remote, local)
        finally:
            ftp.quit()


# ---------------------------------------------------------------------------
# Public: download kernels via FTP
# ---------------------------------------------------------------------------


def download_kernels_via_ftp(
    spacecraft: str,
    mk: str,
    kernels_dir: Path | str,
    version: str = "latest",
    n_workers: int = _N_PARALLEL_DOWNLOADS,
) -> Path:
    """
    Download a SPICE metakernel and all referenced kernel files from the ESA FTP.

    Only files not already present locally are downloaded. For
    ``version='latest'``, the metakernel ``.tm`` file is re-fetched on every
    call so newly published kernel references are discovered.

    Kernel files are downloaded in parallel using *n_workers* simultaneous FTP
    connections (each worker opens its own connection; FTP is not thread-safe).

    After this call, constructing::

        TourConfig(
            mk=<returned_path>.as_posix(),
            kernels_dir=<kernels_dir>,
            download_kernels=False,
            ...
        )

    will succeed without any network access, because:

    * the returned path ends in ``.tm``, bypassing ``planetary_coverage``'s
      ESA API lookup;
    * all referenced kernel files are already present under *kernels_dir*.

    Offline / connectivity fallback
    --------------------------------
    A small local cache (``<kernels_dir>/.resolution_cache.json``) records
    which local ``.tm`` file each ``(spacecraft, mk, version)`` combination
    last resolved to, once fully downloaded and verified.

    * **Pinned versions** (anything other than ``'latest'``) are immutable
      once published: if a previous call already fully resolved and
      downloaded this exact ``(spacecraft, mk, version)``, the network is
      skipped entirely on subsequent calls.
    * **``version='latest'``** always attempts a live check first (its
      whole point is to discover newly published kernels). Only if the ESA
      FTP server cannot be reached at all (DNS failure, timeout, connection
      refused, etc.) does it fall back to the last fully-verified cached
      resolution -- with a warning logged, since that may not reflect the
      newest kernels.
    * If nothing usable is cached when the server is unreachable, the
      original connectivity error is raised, wrapped in a
      :class:`ConnectionError` with an actionable message (naming any
      partially-downloaded kernels that are missing, if a cache entry
      exists but is incomplete).

    Parameters
    ----------
    spacecraft:
        Mission name (e.g. ``'JUICE'``).
    mk:
        Metakernel shortcut (e.g. ``'plan'``) or filename (``'juice_plan'``
        or ``'juice_plan.tm'``).
    kernels_dir:
        Root directory where kernels will be stored.  This is the directory
        that ``TourConfig`` maps to the ``$KERNELS`` symbol.
    version:
        SKD version tag (e.g. ``'v461_20260121_001'``) or ``'latest'``.
        When a specific version is given, the versioned ``.tm`` file is looked
        up first in ``kernels/mk/`` and then in ``kernels/mk/former_versions/``.
    n_workers:
        Number of parallel FTP connections used for kernel downloads.
        Defaults to :data:`_N_PARALLEL_DOWNLOADS`.

    Returns
    -------
    Path
        Absolute path to the locally cached ``.tm`` metakernel file.

    Raises
    ------
    ConnectionError
        If the ESA FTP server cannot be reached and no usable local cache
        exists for this ``(spacecraft, mk, version)``.
    """
    kernels_dir = Path(kernels_dir)
    cache_path = get_resolution_cache_path(kernels_dir)
    cache = _read_json_cache(cache_path)
    cache_key = _resolution_cache_key(spacecraft, mk, version)
    cached_entry: dict[str, Any] | None = cache.get(cache_key)
    is_latest = bool(version) and version.lower() == "latest"

    def _usable_cached_tm() -> Path | None:
        if not cached_entry:
            return None
        candidate: Path = kernels_dir / cached_entry["local_tm"]
        if candidate.exists() and not _tm_kernels_missing(candidate, kernels_dir):
            return candidate
        return None

    # Pinned versions never change once published: reuse a fully-verified
    # local copy without touching the network at all.
    if not is_latest:
        cached_tm = _usable_cached_tm()
        if cached_tm is not None:
            log.debug(
                f"FTP: '{mk}' (version={version}) already fully cached at "
                f"{cached_tm} -- skipping network check",
            )
            return cached_tm

    try:
        local_tm = _download_kernels_via_ftp_online(
            spacecraft, mk, kernels_dir, version, n_workers,
        )
    except FileNotFoundError:
        raise  # a genuine "no such kernel/version" answer, not connectivity
    except ftplib.all_errors as exc:
        log.warning(f"FTP: could not reach {_FTP_HOST} ({exc})")
        cached_tm = _usable_cached_tm()
        if cached_tm is not None:
            assert cached_entry is not None  # noqa: S101 - implied by cached_tm above
            when = _format_checked_at(cached_entry.get("checked_at"))
            log.warning(
                f"FTP: offline fallback -- using kernels cached at {cached_tm} "
                f"(last verified online {when}); "
                "this may not reflect the latest kernels.",
            )
            return cached_tm
        if cached_entry:
            missing = _tm_kernels_missing(
                kernels_dir / cached_entry["local_tm"], kernels_dir,
            )
            raise ConnectionError(
                f"Could not reach the ESA FTP server ({exc}), and the locally "
                f"cached kernels for spacecraft={spacecraft!r} mk={mk!r} "
                f"version={version!r} are incomplete (missing: {missing}). "
                "Connect to the internet to finish downloading them.",
            ) from exc
        raise ConnectionError(
            f"Could not reach the ESA FTP server ({exc}) and no local cache "
            f"is available for spacecraft={spacecraft!r} mk={mk!r} "
            f"version={version!r}. Connect to the internet, or point "
            "kernels_dir at a directory with a pre-populated cache.",
        ) from exc

    # Success -- remember this resolution so a later call (offline, or a
    # pinned version) can reuse it without hitting the network again.
    cache[cache_key] = {
        "local_tm": str(local_tm.relative_to(kernels_dir)),
        "checked_at": time.time(),
    }
    _write_json_cache(cache_path, cache)
    return local_tm


def _download_kernels_via_ftp_online(
    spacecraft: str,
    mk: str,
    kernels_dir: Path,
    version: str,
    n_workers: int,
) -> Path:
    """Live (network-required) implementation behind
    :func:`download_kernels_via_ftp`."""
    base = _ftp_base(spacecraft)

    log.info(
        f"FTP fallback: connecting to {_FTP_HOST} "
        f"for {spacecraft} (mk='{mk}', version='{version}')",
    )
    with _ftp_connection_slot():
        ftp = ftplib.FTP(_FTP_HOST)  # noqa: S321 - public ESA FTP, anonymous login
        try:
            ftp.login()

            remote_tm = _resolve_tm_on_ftp(ftp, spacecraft, mk, base, version=version)
            tm_filename = Path(remote_tm).name
            local_tm = kernels_dir / "mk" / tm_filename

            # --- Download the .tm file ---------------------------------------
            refresh_tm = bool(version) and version.lower() == "latest"
            log.info(f"FTP: resolving metakernel {tm_filename}")
            downloaded = _locked_download(ftp, remote_tm, local_tm, force=refresh_tm)
            if downloaded:
                log.info(f"FTP: downloaded metakernel {tm_filename}")
            else:
                log.debug(f"FTP: metakernel already cached at {local_tm}")

        finally:
            ftp.quit()

    # --- Parse referenced kernel paths ---------------------------------------
    tm_content = local_tm.read_text(encoding="utf-8", errors="replace")
    rel_paths = _parse_mk_kernel_paths(tm_content)

    # Identify kernels that still need to be fetched
    to_download: list[tuple[str, Path]] = []
    n_skip = 0
    for rel in rel_paths:
        local_kernel = kernels_dir / rel
        if local_kernel.exists():
            n_skip += 1
        else:
            to_download.append((f"{base}kernels/{rel}", local_kernel))

    log.info(
        f"FTP: {len(to_download)} kernels to download, "
        f"{n_skip} already cached — using {n_workers} parallel connections",
    )

    # --- Parallel download of missing kernels --------------------------------
    n_dl = n_fail = 0
    if to_download:
        futures = {}
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            for remote_kernel, local_kernel in to_download:
                log.debug(f"FTP: queuing {local_kernel.name}")
                fut = pool.submit(
                    _download_one_kernel, _FTP_HOST, remote_kernel, local_kernel,
                )
                futures[fut] = local_kernel

            with tqdm(
                total=len(to_download),
                desc="Downloading kernels",
                unit="file",
                dynamic_ncols=True,
            ) as pbar:
                for fut in as_completed(futures):
                    local_kernel = futures[fut]
                    exc = fut.exception()
                    if exc is None:
                        log.info(f"FTP: downloaded {local_kernel.name}")
                        n_dl += 1
                        pbar.set_postfix_str(local_kernel.name, refresh=False)
                    else:
                        log.warning(
                            f"FTP: could not download '{local_kernel.name}': {exc}",
                        )
                        n_fail += 1
                    pbar.update(1)

    log.info(
        f"FTP: done - {n_dl} downloaded, {n_skip} cached, {n_fail} failed",
    )

    return local_tm
