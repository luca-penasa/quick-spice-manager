# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

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
