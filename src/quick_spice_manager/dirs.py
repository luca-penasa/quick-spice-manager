"""Directories used by the application (cache directories, etc))"""

from pathlib import Path

import platformdirs


def get_quick_spice_manager_cache_directory() -> Path:
    """
    The user default cache directory to be used across the whole application.

    Dedicated to this package (``quick-spice-manager``) rather than sharing
    the ``planetary-coverage`` package's own cache directory -- the two are
    unrelated caches and sharing a directory risked confusion (or, in
    principle, collisions) between them.
    """
    return Path(
        platformdirs.user_cache_dir("quick-spice-manager", "quick-spice-manager"),
    )


def get_user_kernels_cache_directory() -> Path:
    """
    The user default kernels cache directory to be used across the whole application
    """
    return get_quick_spice_manager_cache_directory().joinpath("kernels")


def get_cache_lock_path(cache_dir: Path) -> Path:
    """
    Path to the cross-process lock file guarding directory-level operations
    (e.g. ``clear_cache()``) on *cache_dir*.

    Deliberately placed in ``cache_dir``'s parent (not inside it) so that
    removing/recreating ``cache_dir`` itself (e.g. via ``shutil.rmtree``)
    never deletes the lock file out from under a concurrent lock holder.
    """
    return cache_dir.parent / f".{cache_dir.name}.cache.lock"


def get_load_lock_dir(kernels_dir: Path) -> Path:
    """
    Directory holding the cross-process "furnish slot" lock files that bound
    how many processes may be resolving/furnishing kernels from
    *kernels_dir* at the same time.

    Deliberately placed in ``kernels_dir``'s parent (not inside it) so that
    ``clear_cache()``'s ``shutil.rmtree(kernels_dir)`` never deletes it out
    from under a concurrent lock holder.
    """
    return kernels_dir.parent / f".{kernels_dir.name}.load-locks"


def get_ftp_connection_lock_dir() -> Path:
    """
    Directory holding the cross-process "connection slot" lock files that
    bound the total number of simultaneous connections opened to the ESA
    FTP server.

    Deliberately anchored to the user-wide cache root (not under any
    particular mission's kernels directory) since the connection limit is
    per machine/user, not per mission -- every ``QuickSpiceManager``
    instance and OS process for this user shares the same slot pool
    regardless of which ``kernels_dir`` it downloads into.
    """
    return get_quick_spice_manager_cache_directory() / "locks" / "ftp-connections"


def get_resolution_cache_path(kernels_dir: Path) -> Path:
    """
    Path to the small JSON cache recording which local ``.tm`` file each
    ``(spacecraft, mk, version)`` combination last resolved to on the ESA
    FTP server.

    Used as an offline / connectivity-failure fallback so a manager doesn't
    need to reach the network again for kernels it has already fully
    downloaded and verified. Lives inside ``kernels_dir`` so it is naturally
    cleared away by ``clear_cache()``.
    """
    return kernels_dir / ".resolution_cache.json"


def get_metakernel_listing_cache_dir() -> Path:
    """
    Directory holding cached ESA FTP metakernel-name listings (one JSON file
    per mission), used as an offline fallback for
    ``list_metakernels_via_ftp()``.

    Mission-scoped rather than ``kernels_dir``-scoped, since listing which
    metakernels exist for a mission has no particular kernels directory of
    its own until one is chosen.
    """
    return get_quick_spice_manager_cache_directory() / "metakernel-listings"
