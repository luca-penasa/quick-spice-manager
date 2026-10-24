# `kernel_provenance(source="pool")` reports the temporary metakernel, and calls everything "extra" once it is deleted

**Version:** 0.2.0 (also current `main`, `_kernel_provenance_from_pool`,
`src/quick_spice_manager/spice_manager.py:865`; `__del__`, line 266)
**Found:** 2026-10-24, from ptr-editor (planned use: warn when kernels were loaded on top of a
mission metakernel before solving a PTR)
**Severity:** medium. The result is wrong only after the manager that loaded the kernels has been
garbage-collected, but that is the normal case for code that does
`QuickSpiceManager(mk=...).load_kernels()` without keeping the manager.

## What happens

`load_kernels()` furnishes a localized copy of the metakernel, a `NamedTemporaryFile` such
as `/tmp/juice_crema_5_1_150lb_23_1_a3-localized-zwnuzyru.tm`. That copy is the pool's `META`
entry. `__del__` deletes it when the manager is collected, while the kernels stay loaded.

`kernel_provenance(source="pool")` takes the last `META` entry of the pool and reads it to find
which kernels it references:

1. **`metakernel` is the temp file**, not the real metakernel. `source="manager"` reports
   `juice_crema_5_1_150lb_23_1_a3.tm` (the real resolved file); `source="pool"` reports
   `/tmp/juice_crema_5_1_150lb_23_1_a3-localized-zwnuzyru.tm`.
2. **Once the temp file is deleted**, `_kernels_referenced_by_metakernel` cannot read it, so
   it returns an empty set, and **every loaded kernel is reported as extra** (103 of 103).

```python
import gc
from quick_spice_manager import QuickSpiceManager as Q

sm = Q(mk="5_1_150lb_23_1_a3", download_kernels=False)
sm.load_kernels()
Q.kernel_provenance(sm, source="pool")
# metakernel: /tmp/juice_crema_5_1_150lb_23_1_a3-localized-zwnuzyru.tm, extra_kernels: 0

del sm; gc.collect()          # kernels are still loaded (104 pool entries)
Q.kernel_provenance(Q(mk="5_1_150lb_23_1_a3", download_kernels=False), source="pool")
# metakernel: the same, now missing file; extra_kernels: 103 of 103
```

The same applies to `_read_mk_keywords` if it is given the pool's `META` entry: `SKD_VERSION` and
`MK_IDENTIFIER` cannot be read from a deleted file.

## Proposed fix

- Keep a module-level map from each localized temp path to its real resolved metakernel
  (`_resolved_mk_path`) and to the kernels it references (computed once, when it is localized).
  `_kernel_provenance_from_pool` then reports the real metakernel and computes the extras from
  the map, without reading the temp file.
- Or write the provenance into the localized copy itself (for example
  `QSM_SOURCE_METAKERNEL = '<real path>'` in `\begindata`), and fall back to the real file
  when the temp file is gone.
- Do not delete the temp metakernel while its kernels are still in the pool. Delete it in
  `unload_kernels()` and at interpreter exit (`atexit`), not in `__del__`. A `META` entry pointing
  to a deleted file also makes `unload` of that metakernel by path impossible for other code.

## Tests

- `source="pool"` reports the real metakernel path, not the temp file;
- after `del sm; gc.collect()`, `extra_kernels` is still empty and `metakernel` still names
  the real file;
- with one kernel added via `spiceypy.furnsh`, `extra_kernels` lists exactly that kernel.
