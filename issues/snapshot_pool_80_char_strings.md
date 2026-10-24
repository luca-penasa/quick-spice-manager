# `snapshot_pool` writes entries longer than 80 characters when kernels are in several directories

**Version:** 0.2.0 (also current `main`, `src/quick_spice_manager/spice_manager.py:683`)
**Found:** 2026-10-24, from ptr-editor: solving a PTR with ptwrapper against a snapshot of the
session's pool (ptr-editor `issues/38_quick_spice_manager_api.md`)
**Severity:** high for `snapshot_pool`. Any pool with one kernel outside the download cache
gives a metakernel that `furnsh` cannot load.

## What happens

`snapshot_pool` puts the longest common directory of all kernels into one `PATH_VALUES` and
writes each kernel as `$KERNELS/<rest of the path>`. It checks each resolved path against
NAIF's 255-character limit on file names (`_KERNEL_MAX_LENGTH`).

But in a text kernel, one quoted string can have at most **80 characters**. A longer value
has to be split with the `+` continuation marker. SPICE does not report an error: it cuts the
string at 80 characters.

When every kernel is in the cache, the common prefix is
`~/.cache/quick-spice-manager/kernels/juice` and the entries are short (`$KERNELS/ck/…`),
so the bug does not show. One kernel in another tree (a user's CK, an extra SPK, a solver
output) is enough to make the common prefix `/`. Then every entry contains the full path and
most are over 80 characters:

```text
PATH_VALUES = ( '/' )
...
'$KERNELS/data/.cache/quick-spice-manager/kernels/juice/ck/juice_sc_crema_5_1_150lb_23_1_a3_default_v01.bc'
```

`furnsh` of the snapshot:

```text
SPICE(NOSUCHFILE) -- The first file '//data/.cache/quick-spice-manager/kernels/juice/ck/juice_sc_crema_5_1_150'
specified by KERNELS_TO_LOAD in the file /tmp/qsm_eqtv6usg/snap.tm could not be located.
```

## Reproduction

With the a3 kernels in the cache:

```python
import shutil, tempfile
from pathlib import Path
import spiceypy as sp
from quick_spice_manager import QuickSpiceManager

work = Path(tempfile.mkdtemp())
sm = QuickSpiceManager(mk="5_1_150lb_23_1_a3", download_kernels=False)
sm.load_kernels()

# one kernel outside the cache: a copy of the LSK
lsk = next(sp.kdata(i, "ALL")[0] for i in range(sp.ktotal("ALL"))
           if "/lsk/" in sp.kdata(i, "ALL")[0])
extra = work / "user" / "my_extra.tls"
extra.parent.mkdir()
shutil.copy(lsk, extra)
sm.add_kernel(extra)

snap = sm.snapshot_pool(work / "snap.tm")
sp.kclear()
sp.furnsh(str(snap))      # SpiceNOSUCHFILE
```

Result: 104 kernels, `PATH_VALUES = ( '/' )`, 52 entries over 80 characters, and `furnsh`
fails as above.

Without the `add_kernel` call the same snapshot loads, and ptwrapper produces the same CK
as with the original metakernel (checked from ptr-editor).

## Proposed fix

- Use one `PATH_SYMBOLS` entry per kernel directory (or per distinct root) instead of one
  common prefix, so that each `KERNELS_TO_LOAD` entry is `$Dn/<file name>`.
- Split any string longer than 80 characters with `+` continuation. That covers
  `PATH_VALUES` entries (directory names are often longer than 80 characters) and file names.
  `furnsh` supports continuation in both `PATH_VALUES` and `KERNELS_TO_LOAD`.
- Keep lines at 132 characters or less (see the related note in
  [local_metakernel_uses_cache_kernels_dir.md](local_metakernel_uses_cache_kernels_dir.md)).
- Keep the kernel order. The pool order is the load order, and the last kernel loaded takes
  priority.

The ptr-editor experiment used this writer, and the snapshot then gave the same ptwrapper result
as the original metakernel:

```python
dirs = list(dict.fromkeys(str(Path(k).parent) for k in kernels))
sym = {d: f"D{i}" for i, d in enumerate(dirs)}
def split(s, n=78):
    return [s[i:i + n] + ("+" if i + n < len(s) else "") for i in range(0, len(s), n)]
# PATH_VALUES   = ( every chunk of split(d), quoted, for d in dirs )
# PATH_SYMBOLS  = ( sym[d] for d in dirs )
# KERNELS_TO_LOAD = ( f"$" + sym[parent] + "/" + name for each kernel )
```

## Also worth adding to the snapshot

These are not needed by `furnsh`, but useful to anyone reading the file:

- copy `SKD_VERSION` and `MK_IDENTIFIER` from the pool's metakernel (`_read_mk_keywords`
  exists on `main`);
- list the kernels that are not in that metakernel (`kernel_provenance()["extra_kernels"]`)
  in the `\begintext` part.

## Tests

- kernels in two trees (cache plus a temp dir): the snapshot loads, with the same kernels in the
  same order;
- a kernel directory longer than 80 characters;
- a file name longer than 80 characters;
- a round trip: `snapshot_pool` → `kclear` → `furnsh` → the same `kdata` list.
