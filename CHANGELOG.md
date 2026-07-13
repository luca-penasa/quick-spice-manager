# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- `kernel_provenance()`: reports the minimal kernel set needed to reproduce the currently active kernel set (e.g. for PDS4 label generation) — the resolved metakernel itself (real filename, with the SKD version baked in when pinned) plus only the kernels added on top of it (e.g. via `add_kernel()`). Pass `source="pool"` to inspect the live SPICE pool directly instead of this manager's own bookkeeping.
- `sm.config`: a pandas DataFrame (spacecraft, version, resolved metakernel path, kernels directory) resolved directly from the manager's own state. Unlike `tour_config`, it does not require the `planetary-coverage` extra and does not report target/instrument.
- `get_tour_config()`: explicit method taking `target`/`instrument` as call-time parameters, replacing stored target/instrument state on the manager.
- Offline resilience: a small on-disk resolution cache lets pinned SKD versions skip the network entirely once fully downloaded, and lets `version="latest"` fall back to the last verified local resolution (with a warning) when ESA's FTP server is unreachable.
- Multi-process/thread safety: an in-process re-entrant lock serializes all CSPICE pool operations (`with QuickSpiceManager(...)` holds it for the whole load-use-unload lifecycle); kernel downloads use atomic temp-file+rename with per-file cross-process locks; a bounded pool of FTP connection slots and per-`kernels_dir` furnish slots keep bursts of concurrent managers/processes from overwhelming ESA's FTP server or each other.
- `QuickSpiceManager` is now the primary class name. `SpiceManager` remains available as a deprecated alias (emits a `DeprecationWarning`).
- The kernel cache directory is now dedicated to this package instead of being shared with `planetary-coverage`'s cache.

### Changed

- **`resolved_mk` is now a self-resolving property** instead of a plain attribute only ever set as a side effect of `load_kernels()`. Simply accessing `sm.resolved_mk` now triggers FTP download/localization on demand and returns a ready-to-furnish path — without loading anything into the SPICE pool. The result is cached on the instance.
- `load_kernels()` now returns the localized temporary metakernel path (the same path exposed by `resolved_mk`) rather than the original, non-temp metakernel path.
- `download_kernels=False` is now actually honored: resolution is satisfied entirely from the local cache, raising `FileNotFoundError` if nothing complete is cached, instead of silently reaching the network anyway.
- Changing `spacecraft`/`version`/`mk`/`kernels_dir` on an existing manager instance now invalidates any already-resolved metakernel, forcing re-resolution on next access.

### Deprecated

- `tour_config` (property) is deprecated in favor of `get_tour_config()`. It still works with the same defaults (`target='Jupiter'`, `instrument='JANUS'`) for backward compatibility, but emits a `DeprecationWarning`.

### Removed

- `_target`/`_instrument`/`_kernels` stored fields, `coverage_table()`, and the `metakernel` property — redundant with `resolved_mk`, and required building a whole `TourConfig` just to read one path.
- Orphaned, unused `utils.py` (imported `planetary_coverage` unconditionally, crashing on import without the optional dependency; its own docstring admitted the functions were buggy).

### Fixed

- Pinning an SKD version that can't be found on the FTP server now raises a clear `FileNotFoundError` (naming the version and where it looked) instead of silently falling back to the unversioned metakernel.

## 0.1.3 - 2026-04-23

## 0.1.2 - 2026-03-11

## 0.1.1 - 2026-03-11

## 0.1.0 - 2026-03-10

### Added

- **ESA FTP downloads** (`ftp.py`): kernel downloads now go directly to the ESA public FTP server at `ftp://spiftp.esac.esa.int/data/SPICE/`. `planetary_coverage` is only used to load already-local kernels and query coverage — it no longer handles downloading.
- **Parallel kernel downloads**: missing kernel files are fetched concurrently using a `ThreadPoolExecutor` (default: 4 simultaneous FTP connections). Each worker opens its own connection since `ftplib.FTP` is not thread-safe.
- **Versioned metakernel support via FTP**: passing a specific version tag (e.g. `version='v461_20260121_001'`) resolves the versioned `.tm` file first in `kernels/mk/`, then in `kernels/mk/former_versions/`, before falling back to the unversioned alias.
- **`metakernels` property**: `SpiceManager.metakernels` lists available metakernels directly from FTP.
- **Integration tests** (`tests/test_ftp_integration.py`): real-network tests that download the metakernel and two small kernel files (≈7 KB total) from the ESA FTP. Enabled by default via `--integration` in `pytest` options.
- **`integration` pytest marker** and `--integration` CLI flag via `tests/conftest.py`: allows selectively skipping or running network tests.

## 0.0.4 - 2026-02-16

### Added

- Allow users to override automatic kernel downloads by specifying custom `mk` and SPICE kernel folders using env variables. This enables using local kernel caches, custom kernel versions, or bypassing the automatic download mechanism from ESA repositories.

## 0.0.3 - 2025-06-12

## 0.0.2 - 2024-11-14
