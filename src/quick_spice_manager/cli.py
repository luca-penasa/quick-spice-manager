"""``qsm``: download, cache and resolve ESA SPICE kernels from the shell.

Results go to stdout and everything else (progress, warnings, errors) to
stderr, so ``$(qsm resolve plan)`` is safe to use in scripts.
"""

import functools
import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from shutil import copyfile
from typing import Any, NamedTuple

import click

from . import log_enable
from .dirs import get_user_kernels_cache_directory
from .ftp import (
    _MISSION_ALIASES,
    _MISSION_FTP_BASE,
    _ftp_base,
    list_metakernels_via_ftp,
)
from .spice_manager import (
    QuickSpiceManager,
    _lookup_localized,
    _read_mk_keywords,
    sizeof_fmt,
)

_MAX_MISSING_SHOWN = 10


class _Group(click.Group):
    """Report expected failures as a one-line error instead of a traceback."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except BrokenPipeError:
            raise  # click handles it: `qsm list | head` is not an error
        except (ValueError, OSError) as exc:
            raise click.ClickException(str(exc)) from exc


class _Resolved(NamedTuple):
    """A metakernel made usable locally, and what it references."""

    localized: Path  # temp copy, deleted at interpreter exit
    source: Path
    kernels_dir: Path
    kernels: frozenset[str]

    @property
    def missing(self) -> list[str]:
        return sorted(k for k in self.kernels if not Path(k).exists())

    @property
    def size(self) -> int:
        paths = (Path(k) for k in self.kernels)
        return sum(p.stat().st_size for p in paths if p.exists())


def _resolve(sm: QuickSpiceManager) -> _Resolved:
    """Download what's missing (unless offline) and localize the metakernel."""
    localized = sm.resolved_mk
    info = _lookup_localized(localized)
    assert info is not None  # noqa: S101 - registered by resolved_mk
    return _Resolved(localized, info.source, info.kernels_dir, info.kernels)


def _require_complete(resolved: _Resolved) -> None:
    missing = resolved.missing
    if not missing:
        return
    shown = "\n".join(f"  {path}" for path in missing[:_MAX_MISSING_SHOWN])
    more = len(missing) - _MAX_MISSING_SHOWN
    raise click.ClickException(
        f"{len(missing)} of {len(resolved.kernels)} kernels referenced by "
        f"{resolved.source.name} are missing:\n{shown}"
        + (f"\n  ... and {more} more" if more > 0 else ""),
    )


def _new_manager(
    spacecraft: str, mk: str, version: str, kernels_dir: Path | None, *, offline: bool,
) -> QuickSpiceManager:
    if not Path(mk).is_file():
        _ftp_base(spacecraft)  # reject an unknown mission before touching the cache
    sm = QuickSpiceManager(
        spacecraft=spacecraft,
        mk=mk,
        version=version,
        kernels_dir=kernels_dir,
        download_kernels=not offline,
    )
    # The manager's own warnings about these are silent unless -v is given,
    # and they replace what was asked for on the command line.
    for name in ("SPICE_METAKERNEL", "SPICE_DIRECTORY"):
        if name in os.environ:
            click.echo(f"note: using {name}={os.environ[name]}", err=True)
    return sm


def _kernel_options(*, offline_flag: bool = True) -> Callable[..., Any]:
    """Add the options selecting a metakernel; the command receives ``sm``."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(
            spacecraft: str,
            mk: str,
            version: str,
            kernels_dir: Path | None,
            offline: bool = True,  # noqa: FBT001, FBT002 - click passes it by name
            **kwargs: Any,
        ) -> Any:
            sm = _new_manager(spacecraft, mk, version, kernels_dir, offline=offline)
            return func(sm=sm, **kwargs)

        if offline_flag:
            wrapper = click.option(
                "--offline",
                is_flag=True,
                help="Never use the network: only use what is already cached.",
            )(wrapper)
        wrapper = click.option(
            "--kernels-dir",
            # Absolute: it ends up in a metakernel used from other directories.
            type=click.Path(file_okay=False, resolve_path=True, path_type=Path),
            help="Kernels directory, instead of the user cache.",
        )(wrapper)
        wrapper = click.option(
            "--version",
            default="latest",
            show_default=True,
            help="SKD version tag to pin, e.g. v462_20260223_001.",
        )(wrapper)
        wrapper = click.option(
            "-s",
            "--spacecraft",
            default="JUICE",
            show_default=True,
            help="Mission name or alias (see 'qsm missions').",
        )(wrapper)
        return click.argument("mk", default="plan")(wrapper)

    return decorator


@click.group(cls=_Group, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="quick-spice-manager", prog_name="qsm")
@click.option(
    "-v", "--verbose", count=True, help="Log progress (-v) or debug details (-vv).",
)
def cli(verbose: int) -> None:
    """Download, cache and resolve ESA SPICE kernels.

    MK is a metakernel shortcut ('plan'), name ('juice_plan') or the path to
    a local .tm file. It defaults to 'plan'.
    """
    if verbose:
        log_enable("DEBUG" if verbose > 1 else "INFO")
    else:
        # Failed downloads and offline fallbacks are only reported as FTP
        # warnings: never hide that the kernels may not be the latest ones.
        log_enable("WARNING", mod="quick_spice_manager.ftp")


@cli.command()
def missions() -> None:
    """List the supported missions and their aliases."""
    for mission in sorted(_MISSION_FTP_BASE):
        aliases = sorted(a for a, m in _MISSION_ALIASES.items() if m == mission)
        click.echo(" ".join([mission, *aliases]))


@cli.command("list")
@click.option(
    "-s",
    "--spacecraft",
    default="JUICE",
    show_default=True,
    help="Mission name or alias (see 'qsm missions').",
)
@click.option("-f", "--filter", "text", help="Only names containing this text.")
def list_metakernels(spacecraft: str, text: str | None) -> None:
    """List the metakernels available on the ESA FTP server."""
    for name in sorted(list_metakernels_via_ftp(spacecraft)):
        if text is None or text.lower() in name.lower():
            click.echo(name)


@cli.command()
@_kernel_options()
def download(sm: QuickSpiceManager) -> None:
    """Download a metakernel and its kernels into the cache.

    Files already cached are not downloaded again. Exits with an error if any
    kernel is still missing afterwards.
    """
    resolved = _resolve(sm)
    _require_complete(resolved)
    click.echo(f"metakernel:  {resolved.source}")
    click.echo(f"kernels_dir: {resolved.kernels_dir}")
    click.echo(f"kernels:     {len(resolved.kernels)} ({sizeof_fmt(resolved.size)})")


@cli.command()
@_kernel_options()
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Where to write the metakernel, instead of the cache.",
)
def resolve(sm: QuickSpiceManager, output: Path | None) -> None:
    """Download what is needed and print the path of a furnshable metakernel.

    The file is a copy of the metakernel with PATH_VALUES pointing at the
    local kernels, and it stays there until the cache is cleared:

        python my_script.py "$(qsm resolve plan)"
    """
    resolved = _resolve(sm)
    _require_complete(resolved)
    if output is None:
        # The original metakernel can't be used here: its PATH_VALUES is
        # relative to the working directory. Neither can resolved.localized:
        # it's deleted when this process exits.
        key = f"{resolved.source}|{resolved.kernels_dir}".encode()
        digest = hashlib.sha256(key).hexdigest()[:8]
        output = (
            sm.user_kernels_cache_directory
            / "localized"
            / f"{resolved.source.stem}-{digest}.tm"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    copyfile(resolved.localized, output)
    click.echo(output.resolve())


@cli.command()
@_kernel_options(offline_flag=False)
@click.option("--json", "as_json", is_flag=True, help="Print as JSON.")
def info(sm: QuickSpiceManager, *, as_json: bool) -> None:
    """Show what is cached for a metakernel, without using the network."""
    try:
        resolved = _resolve(sm)
    except FileNotFoundError as exc:
        message = f"{exc}\nRun 'qsm download' first."
        raise click.ClickException(message) from exc
    skd_version, mk_identifier = _read_mk_keywords(resolved.source)
    fields = {
        "metakernel": str(resolved.source),
        "skd_version": skd_version,
        "mk_identifier": mk_identifier,
        "kernels_dir": str(resolved.kernels_dir),
        "kernels": len(resolved.kernels),
        "missing": len(resolved.missing),
        "size": resolved.size if as_json else sizeof_fmt(resolved.size),
    }
    if as_json:
        click.echo(json.dumps(fields, indent=2))
        return
    for key, value in fields.items():
        click.echo(f"{key + ':':<15}{'-' if value is None else value}")


@cli.group()
def cache() -> None:
    """Inspect or clear the kernels cache."""


def _directory_size(directory: Path) -> int:
    return sum(f.stat().st_size for f in directory.glob("**/*") if f.is_file())


@cache.command("list")
def cache_list() -> None:
    """Show the cached missions, their size and location."""
    root = get_user_kernels_cache_directory()
    dirs = sorted(
        d
        for d in (root.iterdir() if root.is_dir() else ())
        if d.is_dir() and not d.name.startswith(".")  # lock directories
    )
    if not dirs:
        click.echo(f"Nothing cached in {root}", err=True)
    for directory in dirs:
        size = sizeof_fmt(_directory_size(directory))
        click.echo(f"{directory.name}\t{size}\t{directory}")


@cache.command("clear")
@click.argument("mission")
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation.")
def cache_clear(mission: str, *, yes: bool) -> None:
    """Delete the cached kernels of MISSION.

    Don't run this while another process is downloading into the same cache.
    """
    _ftp_base(mission)
    sm = QuickSpiceManager(spacecraft=mission)
    if not yes:
        click.confirm(
            f"Delete {sm.user_kernels_cache_directory} ({sm.cache_size})?",
            abort=True,
        )
    sm.clear_cache()
    click.echo(f"Cleared {sm.user_kernels_cache_directory}", err=True)


main = cli
