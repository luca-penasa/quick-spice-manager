# Reduced kernel sets: cut a metakernel down to a time window and the kernels a workload needs

**Version:** 0.2.2 (current `main`)
**Found:** 2026-11-07, from ptr-editor CI (TASK-85): every CI job downloads the 0.9 GB
`juice_crema_5_1_150lb_23_1_a3` set from the ESA FTP server. A busy server leaves partial sets
(fixed separately on branch `fail-on-missing-kernels`), costs minutes per job, and the macOS and
Windows runners pay for it in billed minutes.
**Kind:** feature. qsm writes the reduced set, and ptr-editor (or any other package) uses it to
make test data that is committed to the repository, so its tests never download anything.

## Goal

```python
from quick_spice_manager import reduce_kernels

reduce_kernels(
    mk="5_1_150lb_23_1_a3",
    windows=[("2032-12-17", "2033-01-10"), ("2032-08-31", "2032-09-02")],
    out_dir="tests/data/spice",          # kernels + a relative metakernel (PATH_VALUES = '..')
    drop=["dsk/*"],                      # globs of kernels the workload does not need
)
```

and a `qsm reduce` command with the same options. The output is an ordinary metakernel and its
kernels, so `QuickSpiceManager(mk="tests/data/spice/mk/reduced.tm")` and
`furnsh` both load it. It must be small enough to commit (target: a few MB), and give the same
answers as the full set inside the windows (bit-identical for the subsetted segments).

## What can be reduced, and how (measured on the a3 set, 103 kernels, 882 MB)

| kind | size | segment types | how |
|------|------|---------------|-----|
| DSK  | 487 MB (39 files) | – | drop, when the workload does not use them (see below) |
| SPK  | 338 MB (17) | 2, 3, 8, 13, 21 | `spiceypy.spksub`: exact copy of the records covering each window; all these types are supported |
| CK   | 47 MB (13) | 3 (12 files), 6 (the 24.5 MB `…_baseline_v02.bc`) | type 3: read with `ckgr03`/`cknr03` and the segment's interval starts (DAF data), write with `ckw03`, so exact. Type 6: spiceypy has no `ckw06`; either drop it or resample to type 3 at its own epochs (state the tolerance) |
| binary PCK | 8 MB (2) | 2 | Chebyshev records like SPK type 2: read with `dafgda`, write with `pckw02` (no `pcksub` in CSPICE) |
| text kernels (FK, IK, LSK, SCLK, text PCK) | 3 MB | – | copy as they are |

Prototype (2026-11-07): `spksub` of `jup365_19900101_20500101.bsp` over 2032-12-17 → 2033-01-10 went
from **113.7 MB to 133 KB**, with all 13 bodies kept and a maximum difference of **0.0 km** against the
full file (500 epochs, Ganymede state). Sizes scale with the window length, so a few short windows
of a month or less stay well under 1 MB per large SPK.

Points to handle:

- **Windows**: several disjoint windows are written as several segments per body (one `spksub`
  per window and segment). Pad each window (e.g. a day) so interpolation near the edges still
  has its neighbouring records, and so a solver's extrapolation at block edges works.
- **Bodies/frames filter** (optional): keep only segments whose target/centre is in a list. Not
  needed for size once the windows are cut; useful for very long windows.
- **Coverage report**: after writing, list per body/frame the coverage (`spkcov`, `ckcov`,
  `pckcov`) against the requested windows, and fail if a requested window is not covered by a
  source that covered it.
- **Provenance**: the output metakernel's comments name the source metakernel, its
  `SKD_VERSION`/`MK_IDENTIFIER`, the windows, what was dropped, and the qsm version. Each reduced
  binary kernel keeps the source file name and records it in its comment area (`dafac`).
- **SPICE limits** (see `snapshot_pool_80_char_strings.md`): relative `$KERNELS/...` entries keep
  every string under 80 characters.
- **Licensing**: the kernels are ESA/NAIF public data; check the conditions for redistributing
  derived subsets in another project's repository, and keep their comment areas (which
  carry the credits).

## Which kernels a workload needs

"Remove everything unneeded" cannot be read off the metakernel: OSVE/AGM query SPICE from C, so
there is no Python-level trace of the bodies and frames used. Proposal: an optional, slow
**pruning** pass, `prune_kernels(mk, check)`, that drops one group of kernels at a time (by
directory, then by file) and keeps the drop when `check(metakernel_path)` still passes. Here
`check` is a callable the caller supplies (for ptr-editor: solve the test PTRs and compare the
quaternions with the full set). This runs once, when the test data is regenerated, not in CI.

First measurement for ptr-editor (2026-11-07): the full `-m spice` test suite was run with the
a3 metakernel minus its 39 DSKs (`SPICE_METAKERNEL` pointing at the reduced copy):
**674 passed, 0 failed** (2 min 20 s). Checked: the solver's metakernel and the
fixture's pool both had 0 DSKs (64 kernels in the solver's metakernel; 65 in the pool, the
metakernel file itself included). So the DSKs, more than half the size, are not needed for
ptr-editor's tests, and pruning by file should take most of the remaining kernels too (Mars,
minor bodies, ground stations).

## Use in ptr-editor (follow-up there, once this is released)

- `tests/data/spice/make_reduced_kernels.py` calls `reduce_kernels` with the windows the tests and
  tutorials use: 2032-12-18 → 2033-01-09 (the S008 PTRs that `test_agm_oracle` solves), 2032-06-01,
  2032-09-01 → 2032-10-01, 2033-11-24 → 2033-11-27, 2033-12-12, 2035-10-05, and the result is
  committed together with the script.
- `session_spice_kernels` and the default solver metakernel in tests use the reduced set; one
  `network`-marked smoke test keeps checking the full download path.
- Tests that pick dates outside the windows fail with a clear "not covered" message, which also
  documents the windows.

## Tasks

1. `reduce_kernels()` for SPK (`spksub`), CK type 3, binary PCK type 2, text kernels; drop list.
2. CK type 6: decide drop vs. resample to type 3, and document it.
3. Coverage report and provenance comments; `qsm reduce` command.
4. Optional `prune_kernels(mk, check)`.
5. Tests: subset of a small synthetic SPK/CK written in the test, bit-identical states inside the
   window, `SPICE(SPKINSUFFDATA)` outside it.
