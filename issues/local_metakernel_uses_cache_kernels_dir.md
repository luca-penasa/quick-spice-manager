# A local metakernel is rewritten to load its kernels from the download cache

**Version:** 0.2.0 (also current `main`, `src/quick_spice_manager/spice_manager.py`)
**Found:** 2026-10-06, preparing the PEP planning workshop (ptr-editor 0.4.3)
**Severity:** high for anyone who uses a local kernel dataset (e.g. ESA's `JUICE.zip`): it fails on a
fresh machine, and silently uses other kernels on a machine with a populated cache.

## What happens

```python
from quick_spice_manager import QuickSpiceManager
mk = "/home/me/juice_spice/JUICE/kernels/mk/juice_crema_5_1_150lb_23_1_a3.tm"   # from JUICE.zip
QuickSpiceManager(mk=mk).load_kernels()
```

The metakernel is a local file, so it is not downloaded (correct), but:

1. `__attrs_post_init__` sets `self._kernels_dir = self.user_kernels_cache_directory` because no
   `kernels_dir` was given (lines 334–335), regardless of where the metakernel lives;
2. `_localize_metakernel` (line 358) then rewrites the metakernel's `PATH_VALUES = ( '..' )` to that
   cache directory, e.g. `~/.cache/quick-spice-manager/kernels/juice`;
3. the kernels next to the metakernel (`.../JUICE/kernels/...`) are never used.

Result:

- **Fresh environment** (empty cache): `furnsh` fails with `SpiceNOSUCHFILE`. Nothing is downloaded,
  because the metakernel is local.
- **Populated cache** (any machine that ever used `mk="plan"` or an FTP metakernel): no error, but the
  kernels come from the cache, whatever SKD version happens to be there, not from the dataset the user
  pointed at.

The same applies to everything built on `SpiceManager(mk=...)`. ptr-editor's solver does
`SpiceManager(mk=metakernel).resolved_mk` (`ptr_solver/ptr_solver.py`, `solve_attitude`) and hands
that localized metakernel to ptwrapper, so `block.solve(mk=local_path)` solves with cache kernels too.

## Reproduction (fresh container)

`python:3.12`, `pip install ptr-editor==0.4.3` (pulls quick-spice-manager 0.2.0), unzip
<https://spiftp.esac.esa.int/data/SPICE/JUICE/misc/skd/JUICE.zip> into `/root/juice_spice`:

```text
metakernel found: True
notebook load  : FAILED: SpiceNOSUCHFILE        <- looked in ~/.cache/quick-spice-manager/kernels/juice
```

With `SPICE_DIRECTORY=/root/juice_spice/JUICE/kernels` set before creating the manager:

```text
notebook load  : 103 kernels, 103 from SPICE_DATA, 0 elsewhere
solver mk paths: ['/root/juice_spice/JUICE/kernels']
```

## Workarounds (in use in the workshop notebooks)

```python
os.environ["SPICE_DIRECTORY"] = os.path.join(SPICE_DATA, "kernels")   # read by every manager,
QuickSpiceManager(mk=MK).load_kernels()                                # including ptr-editor's solve()
```

or `QuickSpiceManager(mk=MK, kernels_dir=...)`, which does not reach managers created inside other
libraries.

## Proposed fix

When `mk` is an existing local file and no `kernels_dir` (nor `SPICE_DIRECTORY`) is given, take the
kernels directory from the metakernel itself: resolve its own `PATH_VALUES` relative to the
metakernel's folder (ESA metakernels use `'..'`, i.e. `mk.parent.parent` = `.../kernels`), and localize
to that absolute path. Use the cache directory only for FTP-resolved metakernels.

Worth logging the resolved kernels directory at INFO, and recording it in `kernel_provenance()`, so a
user can see where the kernels came from.

Tests:

- local metakernel with an empty cache loads the kernels next to it;
- local metakernel with a populated cache still loads the kernels next to it, not the cache's;
- `SPICE_DIRECTORY` and an explicit `kernels_dir` still take precedence.

## Related: the 132-character line limit

SPICE text kernels allow at most 132 characters per line. `_localize_metakernel` writes the kernels
directory into a single `PATH_VALUES = ( '...' )` line, and only checks it against the 255-character
limit of a `PATH_VALUES` value (`_KERNEL_MAX_LENGTH`). A kernels directory of about 110 characters is
enough to exceed the line limit; `furnsh` then fails with an unhelpful `SPICE(TYPEMISMATCH)`
("PATH_VALUES has been set up as a string variable ... line 114 ... is not a string value"). Seen with a
path under `/tmp/claude-.../scratchpad/...`; students who unzip into a long Windows folder
(`C:\Users\...\OneDrive\Documents\...`) would hit it too.

Suggestion: check the length of the rendered line (not only of the path) and raise a clear error that
names the path and suggests a shorter location or a symlink.
