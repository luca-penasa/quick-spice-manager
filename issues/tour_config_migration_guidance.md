# Migration from `SpiceManager` / `tour_config` needs a note: `load_kernels()` clears the pool

**Version:** 0.2.0
**Found:** 2026-10-24, migrating ptr-editor to the 0.2.0 API (ptr-editor
`issues/38_quick_spice_manager_api.md`)
**Severity:** low (documentation), but the wrong migration fails silently.

## What happens

Much code written for 0.1.x uses `SpiceManager(mk=...).tour_config` **only to load kernels**,
and throws the `TourConfig` away. ptr-editor's test fixtures and tutorials do this. The
deprecation message points to `get_tour_config()`, but the natural replacement in that case is
`load_kernels()`, and the two do not behave the same:

- `tour_config` / `get_tour_config()` load through `planetary_coverage.TourConfig`, which **adds**
  to the pool;
- `load_kernels()` defaults to `exclusive=True` and **clears** the pool first.

So code that loads two metakernels one after the other ends up with both in the pool through
`tour_config`, and with only the last one through `load_kernels()`. Checked with the cached
kernels:

| Calls | Pool entries |
|---|---:|
| `Q().load_kernels()` (`plan`), then `Q(mk="5_1_150lb_23_1_a3").load_kernels()` | 104 (only a3) |
| the same with `exclusive=False` | 218 (both) |

The README documents `exclusive`. The CHANGELOG's "Deprecated" section and the
`DeprecationWarning` text do not mention it.

## Also outdated

The `QuickSpiceManager` class docstring still shows the deprecated forms:
`sm = SpiceManager(mk=...)`, `tc = SpiceManager().tour_config`.

## Proposed fix

- In the CHANGELOG (0.2.0 "Deprecated") and the README, add a short migration table:
  | 0.1.x | 0.2.0 |
  |---|---|
  | `SpiceManager` | `QuickSpiceManager` |
  | `sm.tour_config` to get a `TourConfig` | `sm.get_tour_config()` |
  | `sm.tour_config` only to load kernels | `sm.load_kernels()`; pass `exclusive=False` to keep what was loaded before |
- Make the `tour_config` warning say the same in one line, for example: "… use
  get_tour_config(), or load_kernels() if you only need the kernels loaded (note: it clears the
  pool unless exclusive=False)".
- Update the class docstring examples to `QuickSpiceManager` and `get_tour_config()`.
