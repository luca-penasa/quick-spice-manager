"""
FTP fallback for SPICE kernel downloads.

Used when planetary_coverage cannot access its Bitbucket source
(e.g. HTTP 401/403 due to password protection). Downloads metakernels
and referenced kernel files anonymously from the ESA public FTP server
at ftp://spiftp.esac.esa.int/data/SPICE/.
"""

from __future__ import annotations

import ftplib
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from loguru import logger as log

# Default number of parallel FTP connections for kernel downloads.
# Keep this conservative; ESA FTP limits simultaneous connections per IP.
_N_PARALLEL_DOWNLOADS = 4

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
            f"Supported: {sorted(_MISSION_FTP_BASE)}"
        ) from None


# ---------------------------------------------------------------------------
# Public: list available metakernels via FTP
# ---------------------------------------------------------------------------


def list_metakernels_via_ftp(mission: str) -> list[str]:
    """
    Return the metakernel stem names available for *mission* on the ESA FTP.

    Returns filenames with the ``.tm`` suffix stripped, matching the format
    that ``planetary_coverage.ESA_MK[mission].mks`` would normally return.

    Parameters
    ----------
    mission:
        Mission name understood by planetary_coverage (e.g. ``'JUICE'``).
    """
    base = _ftp_base(mission)
    mk_dir = f"{base}kernels/mk/"
    log.info(f"FTP: listing metakernels for {mission} from {_FTP_HOST}{mk_dir}")

    ftp = ftplib.FTP(_FTP_HOST)  # noqa: S321 - public ESA FTP, anonymous login
    try:
        ftp.login()
        entries = ftp.nlst(mk_dir)
    finally:
        ftp.quit()

    return [Path(e).stem for e in entries if e.endswith(".tm")]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_tm_on_ftp(
    ftp: ftplib.FTP, mission: str, mk: str, base: str, version: str = "latest"
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
            f"Cannot list FTP directory {mk_dir}: {exc}"
        ) from exc

    tm_map: dict[str, str] = {
        Path(e).name: e for e in entries if e.endswith(".tm")
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
            f"for mission '{mission}'. Available .tm files: {sorted(tm_map)}"
        )

    # --- Step 2: versioned lookup using the stem -----------------------------
    is_versioned = bool(version) and version.lower() not in ("latest", "all")
    if not is_versioned:
        return unversioned_path

    stem = Path(unversioned_path).stem  # e.g. "juice_s011_tr03"
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
            Path(e).name: e for e in fv_entries if e.endswith(".tm")
        }
        if versioned_name in tm_map_fv:
            log.debug(
                f"FTP: resolved versioned TM '{versioned_name}' in former_versions/"
            )
            return tm_map_fv[versioned_name]
    except ftplib.error_perm:
        log.debug(f"FTP: no former_versions directory at {fv_dir}")

    log.warning(
        f"FTP: versioned TM '{versioned_name}' not found in mk/ or former_versions/; "
        f"falling back to unversioned '{Path(unversioned_path).name}'"
    )
    return unversioned_path


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
        r"PATH_SYMBOLS\s*=\s*\((.*?)\)", tm_content, re.DOTALL
    )
    symbol_names: list[str] = (
        re.findall(r"'([^']+)'", sym_match.group(1)) if sym_match else []
    )

    # Extract KERNELS_TO_LOAD quoted entries
    ktl_match = re.search(
        r"KERNELS_TO_LOAD\s*=\s*\((.*?)\)", tm_content, re.DOTALL
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
    """Stream *remote* from an open FTP connection to *local*, creating parents."""
    local.parent.mkdir(parents=True, exist_ok=True)
    with local.open("wb") as fh:
        ftp.retrbinary(f"RETR {remote}", fh.write)


def _download_one_kernel(host: str, remote: str, local: Path) -> None:
    """
    Open a fresh anonymous FTP connection, download *remote* to *local*, then close.

    Each parallel worker calls this independently so threads never share a
    single ``ftplib.FTP`` object (FTP connections are not thread-safe).
    """
    ftp = ftplib.FTP(host)  # noqa: S321 - public ESA FTP, anonymous login
    try:
        ftp.login()
        _ftp_download_file(ftp, remote, local)
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

    Only files not already present locally are downloaded.  The metakernel
    ``.tm`` file itself is always (re-)fetched when absent.

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
    """
    base = _ftp_base(spacecraft)
    kernels_dir = Path(kernels_dir)

    log.info(
        f"FTP fallback: connecting to {_FTP_HOST} "
        f"for {spacecraft} (mk='{mk}', version='{version}')"
    )
    ftp = ftplib.FTP(_FTP_HOST)  # noqa: S321 - public ESA FTP, anonymous login
    try:
        ftp.login()

        remote_tm = _resolve_tm_on_ftp(ftp, spacecraft, mk, base, version=version)
        tm_filename = Path(remote_tm).name
        local_tm = kernels_dir / "mk" / tm_filename

        # --- Download the .tm file -------------------------------------------
        if not local_tm.exists():
            log.info(f"FTP: downloading metakernel {tm_filename}")
            _ftp_download_file(ftp, remote_tm, local_tm)
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
        f"{n_skip} already cached — using {n_workers} parallel connections"
    )

    # --- Parallel download of missing kernels --------------------------------
    n_dl = n_fail = 0
    if to_download:
        futures = {}
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            for remote_kernel, local_kernel in to_download:
                log.debug(f"FTP: queuing {local_kernel.name}")
                fut = pool.submit(
                    _download_one_kernel, _FTP_HOST, remote_kernel, local_kernel
                )
                futures[fut] = local_kernel

            for fut in as_completed(futures):
                local_kernel = futures[fut]
                exc = fut.exception()
                if exc is None:
                    log.info(f"FTP: downloaded {local_kernel.name}")
                    n_dl += 1
                else:
                    log.warning(
                        f"FTP: could not download '{local_kernel.name}': {exc}"
                    )
                    n_fail += 1

    log.info(
        f"FTP: done - {n_dl} downloaded, {n_skip} cached, {n_fail} failed"
    )

    return local_tm
