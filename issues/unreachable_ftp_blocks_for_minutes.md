# An unreachable ESA FTP server blocks every metakernel resolution for about two minutes

**Version:** 0.2.4 (current `main`)
**Found:** 2026-11-15, from ptr-editor (TASK-120): on a network where port 21 of
`spiftp.esac.esa.int` is blocked, each `QuickSpiceManager(mk="5_1_150lb_23_1_a3").resolved_mk`
takes about 133 s, although every kernel is cached and the offline fallback then works.
**Kind:** bug (no timeout) + improvement (how often `version='latest'` goes to the network).
**Status:** done (unreleased, after 0.2.4).

## What happens

Measured with cProfile (ptr-editor's `resolve_metakernel`, which builds a `QuickSpiceManager`
and reads `resolved_mk`):

```
resolve 133.1 s, hash 0.0 s
  133.077  spice_manager.py:961(resolved_mk)
  133.075  spice_manager.py:753(_resolve_metakernel)
  133.075  ftp.py:567(download_kernels_via_ftp)
  133.073  ftp.py:743(_download_kernels_via_ftp_online)
  133.072  ftplib.py:109(__init__) -> connect -> socket.create_connection
  133.010  {method 'connect' of '_socket.socket' objects}
```

`socket.create_connection(("spiftp.esac.esa.int", 21), timeout=10)` times out on the same
network (DNS resolves fine). After the 133 s, `download_kernels_via_ftp` logs
"could not reach" and returns the cached `.tm`: the result is right, only late.

Two things combine:

1. **No timeout on the FTP connections.** `ftplib.FTP(host)` is called without `timeout=` in
   three places (`ftp.py` ~274 metakernel listing, ~554 per-kernel download, ~759 resolution),
   so a dropped SYN waits for the OS connect timeout (~2 min on Linux, longer elsewhere).
2. **`version='latest'` checks the server on every call**, by design ("its whole point is to
   discover newly published kernels"). Names such as `"5_1_150lb_23_1_a3"` and `"plan"` use
   `'latest'`, so every new manager in every process pays the check. Nothing remembers, within
   the process, that the server was just unreachable.

## Why it matters

ptr-editor builds a manager for every `solve()` (to hash the kernels for its cache key) and for
loading kernels: a notebook with a few solves waits minutes even when every solve is cached.
This was long blamed on the size of the 0.88 GB CReMA set ("about 2 minutes per solve,
whatever the length"); with a local `.tm` path (no FTP) a block solves in about a second.

## Tasks

1. **Timeout on every FTP connection.** `ftplib.FTP(host, timeout=_FTP_TIMEOUT_SECONDS)` in the
   three places; default ~15 s; overridable (`QSM_FTP_TIMEOUT` environment variable). The
   timeout also bounds reads, so keep it generous enough for slow transfers, or set a short
   connect timeout and a longer one after login (`ftp.sock.settimeout(...)`).
2. **Remember an unreachable server in the process.** After a connection failure, skip FTP
   for a while (e.g. 10 min, `QSM_FTP_RETRY_AFTER`) in every manager of the process and go
   straight to the offline fallback, with one warning. A notebook then pays the timeout at
   most once.
3. **A freshness window for `'latest'`.** Skip the live check when the cached resolution was
   verified online less than N hours ago (`checked_at` is already in
   `.resolution_cache.json`); default e.g. 6 h, `QSM_LATEST_MAX_AGE`; `0` keeps today's
   behaviour (always check). Kernels published within the window are picked up at the next
   check; say so in the docs.
4. **An explicit offline switch.** `QSM_OFFLINE=1` (or a documented global) behaving as
   `download_kernels=False` everywhere: resolve only from the cache, never touch the network,
   and fail clearly when the cache is incomplete. Useful on air-gapped machines and in CI.
5. **Say what happened.** The "could not reach" warning should give the time spent and how to
   avoid it next time (the offline switch, a local `.tm`, `SPICE_METAKERNEL`).
6. **Tests.** Point `_FTP_HOST` at an address that drops packets (or a local listening socket
   that never answers) and check that a resolution with a complete cache returns within the
   timeout, that the second manager in the process does not try again (task 2), and that a
   fresh `'latest'` cache entry skips the network (task 3). No real network in the tests.

## Notes

- The resolution cache, the pinned-version shortcut and the offline fallback already exist
  (`download_kernels_via_ftp`): the fix is mostly the timeout and how often `'latest'` checks.
- ptr-editor side: TASK-120 (resolve a name once per session, and correct the manual's
  "two minutes per solve" tip). Its attitude viewer already resolves once per viewer.

## Resolution

- A gate before any FTP connection (`_ensure_ftp_reachable` in `ftp.py`): a plain TCP connect
  to port 21 with a 3 s timeout (`QSM_FTP_PROBE_TIMEOUT`), its answer, good or bad, remembered
  in the process for 2 min (`QSM_FTP_RETRY_AFTER`); a lock makes concurrent callers share one
  probe. It runs before taking a connection slot, so a dead server holds no slot. When it fails,
  it raises `FtpUnreachableError` (an `OSError`), which takes the existing offline fallback.
- All three connections go through `_connect_ftp()`: `timeout=` 30 s per socket operation
  (`QSM_FTP_TIMEOUT`); a socket-level connect failure (refused, timed out, greeting never sent)
  also marks the server unreachable, an FTP-level refusal (`421`) does not. Kernel-download
  retries stop when the server has been marked unreachable.
- `QSM_OFFLINE=1` behaves as `download_kernels=False` and also covers the metakernel listing.
- `QSM_LATEST_MAX_AGE` (default 0, today's behaviour) reuses a complete `'latest'` resolution
  verified online less than that many seconds ago. ptr-editor can set it (e.g. 3600).
- The "could not reach" warning gives the time spent and the ways to skip the network; when the
  server is already known to be unreachable it is logged at info level, not again as a warning.
- The memo is in-process only; several processes each pay the probe once.
- Logging is disabled by default (`log.disable("quick_spice_manager")`), so the warning is only
  seen with `log_enable()`.
- `test_mk_lists` (real network) is now marked `integration`. `addopts` passes
  `--integration`, so the integration tests still run by default.
