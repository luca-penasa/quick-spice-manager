"""Tests for the ``qsm`` command line. No network access: FTP is mocked."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from loguru import logger

from quick_spice_manager.cli import cli

_TEXT_KERNEL = "KPL/PCK\n\n\\begindata\nQSM_TEST_VALUE = ( 42 )\n\\begintext\n"
_METAKERNEL = textwrap.dedent("""\
    KPL/MK

    \\begindata
    PATH_VALUES = ( '..' )
    PATH_SYMBOLS = ( 'KERNELS' )
    KERNELS_TO_LOAD = (
        '$KERNELS/pck/test.tpc'
    )
    SKD_VERSION = 'v001_20260101_001'
    MK_IDENTIFIER = 'juice_plan_v001_20260101_001'
    \\begintext
""")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Throwaway cache, no SPICE_* overrides, and logging restored afterwards."""
    root = tmp_path / "cache"
    for module in ("spice_manager", "cli"):
        monkeypatch.setattr(
            f"quick_spice_manager.{module}.get_user_kernels_cache_directory",
            lambda: root,
        )
    monkeypatch.setattr("quick_spice_manager.spice_manager.load_dotenv", lambda: False)
    monkeypatch.delenv("SPICE_METAKERNEL", raising=False)
    monkeypatch.delenv("SPICE_DIRECTORY", raising=False)
    yield
    logger.remove()
    logger.disable("quick_spice_manager")


def _populate(kernels_dir: Path, *, with_kernel: bool = True) -> Path:
    mk = kernels_dir / "mk" / "juice_plan.tm"
    mk.parent.mkdir(parents=True, exist_ok=True)
    mk.write_text(_METAKERNEL, encoding="utf-8")
    if with_kernel:
        kernel = kernels_dir / "pck" / "test.tpc"
        kernel.parent.mkdir(parents=True, exist_ok=True)
        kernel.write_text(_TEXT_KERNEL, encoding="utf-8")
    return mk


@pytest.fixture
def fake_ftp():
    """Stand in for the FTP download: "fetches" the metakernel and its kernel."""

    def fake(*, kernels_dir, download_kernels, **_kwargs):
        if not download_kernels:
            raise FileNotFoundError("no complete local cache")
        return _populate(kernels_dir)

    target = "quick_spice_manager.spice_manager.download_kernels_via_ftp"
    with patch(target, side_effect=fake) as mock:
        yield mock


def _run(*args: str, **kwargs):
    return CliRunner().invoke(cli, list(args), **kwargs)


def test_missions_lists_names_and_aliases():
    result = _run("missions")
    assert result.exit_code == 0
    assert "SOLAR-ORBITER SOLO" in result.stdout.splitlines()


def test_list_filters_and_sorts():
    names = ["juice_plan_v2", "juice_crema", "juice_plan"]
    with patch(
        "quick_spice_manager.cli.list_metakernels_via_ftp", return_value=names,
    ) as mock:
        result = _run("list", "-s", "juice", "--filter", "PLAN")
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["juice_plan", "juice_plan_v2"]
    mock.assert_called_once_with("juice")


def test_download_reports_what_was_fetched(fake_ftp, tmp_path):
    result = _run("download", "plan", "--version", "v001_20260101_001")
    assert result.exit_code == 0, result.output
    assert "juice_plan.tm" in result.stdout
    assert "kernels:     1 (" in result.stdout
    kwargs = fake_ftp.call_args.kwargs
    assert kwargs["spacecraft"] == "JUICE"
    assert kwargs["mk"] == "plan"
    assert kwargs["version"] == "v001_20260101_001"
    assert kwargs["kernels_dir"] == tmp_path / "cache" / "juice"


def test_download_fails_when_a_kernel_is_still_missing(tmp_path):
    def partial(*, kernels_dir, **_kwargs):
        return _populate(kernels_dir, with_kernel=False)

    target = "quick_spice_manager.spice_manager.download_kernels_via_ftp"
    with patch(target, side_effect=partial):
        result = _run("download")
    assert result.exit_code == 1
    assert "1 of 1 kernels" in result.stderr
    assert "pck/test.tpc" in result.stderr


def test_resolve_prints_only_a_path_that_outlives_the_process(fake_ftp, tmp_path):
    """The manager's localized metakernel is deleted at exit: resolve must not
    print that one. Furnished in another process, after this command's temp
    file would be gone, from an unrelated working directory."""
    result = _run("resolve")
    assert result.exit_code == 0, result.output
    path = Path(result.stdout.strip())
    assert path.is_relative_to(tmp_path / "cache" / "juice")

    code = (
        "import sys, spiceypy\n"
        "spiceypy.furnsh(sys.argv[1])\n"
        "print(spiceypy.gdpool('QSM_TEST_VALUE', 0, 1)[0])\n"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code, str(path)],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
    ).stdout
    assert float(out) == 42


def test_resolve_output_option(fake_ftp, tmp_path):
    out = tmp_path / "new" / "dir" / "my.tm"
    result = _run("resolve", "-o", str(out))
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == str(out)
    assert "PATH_VALUES = ( '..' )" not in out.read_text(encoding="utf-8")


def test_resolve_relative_kernels_dir_works_from_elsewhere(
    fake_ftp, tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    result = _run("resolve", "--kernels-dir", "kernels")
    assert result.exit_code == 0, result.output
    content = Path(result.stdout.strip()).read_text(encoding="utf-8")
    assert f"'{tmp_path / 'kernels'}'" in content


def test_resolve_refuses_an_incomplete_local_metakernel(tmp_path):
    mk = _populate(tmp_path / "dataset", with_kernel=False)
    result = _run("resolve", str(mk))
    assert result.exit_code == 1
    assert result.stdout == ""


def test_resolve_local_metakernel_writes_nothing_next_to_it(tmp_path):
    mk = _populate(tmp_path / "dataset")
    result = _run("resolve", str(mk))
    assert result.exit_code == 0, result.output
    assert Path(result.stdout.strip()).is_relative_to(tmp_path / "cache")
    assert [p.name for p in mk.parent.iterdir()] == [mk.name]


def test_resolve_same_name_from_two_datasets_does_not_collide(tmp_path):
    outputs = {
        _run("resolve", str(_populate(tmp_path / name))).stdout.strip()
        for name in ("a", "b")
    }
    assert len(outputs) == 2


def test_offline_never_downloads(fake_ftp):
    result = _run("resolve", "--offline")
    assert result.exit_code == 1
    assert "no complete local cache" in result.stderr
    assert fake_ftp.call_args.kwargs["download_kernels"] is False


def test_info_does_not_use_the_network(fake_ftp):
    result = _run("info")
    assert fake_ftp.call_args.kwargs["download_kernels"] is False
    assert result.exit_code == 1
    assert "Run 'qsm download' first" in result.stderr


def test_info_json(tmp_path):
    mk = _populate(tmp_path / "dataset")
    result = _run("info", str(mk), "--json")
    assert result.exit_code == 0, result.output
    fields = json.loads(result.stdout)
    assert fields["metakernel"] == str(mk)
    assert fields["skd_version"] == "v001_20260101_001"
    assert fields["mk_identifier"] == "juice_plan_v001_20260101_001"
    assert fields["kernels"] == 1
    assert fields["missing"] == 0
    assert fields["size"] == len(_TEXT_KERNEL)


def test_info_reports_missing_kernels_without_failing(tmp_path):
    mk = _populate(tmp_path / "dataset", with_kernel=False)
    result = _run("info", str(mk))
    assert result.exit_code == 0, result.output
    assert "missing:       1" in result.stdout
    assert "size:          0.0 B" in result.stdout


def test_unknown_mission_is_a_clean_error_and_creates_no_cache(tmp_path):
    result = _run("download", "-s", "NOPE")
    assert result.exit_code == 1
    assert "No FTP path known for mission 'NOPE'" in result.stderr
    assert not (tmp_path / "cache" / "nope").exists()


def test_environment_override_is_reported(tmp_path, monkeypatch):
    mk = _populate(tmp_path / "dataset")
    monkeypatch.setenv("SPICE_METAKERNEL", str(mk))
    result = _run("info", "other")
    assert result.exit_code == 0, result.output
    assert f"note: using SPICE_METAKERNEL={mk}" in result.stderr
    assert str(mk) in result.stdout


def test_offline_fallback_warning_is_shown_by_default(tmp_path):
    """Logging is off in the library, but a stale 'latest' must not go unseen."""
    kernels_dir = tmp_path / "cache" / "juice"
    _populate(kernels_dir)
    (kernels_dir / ".resolution_cache.json").write_text(
        json.dumps({"JUICE|plan|latest": {"local_tm": "mk/juice_plan.tm"}}),
        encoding="utf-8",
    )
    with patch("quick_spice_manager.ftp.ftplib.FTP", side_effect=OSError("down")):
        result = _run("download")
    assert result.exit_code == 0, result.output
    assert "offline fallback" in result.stderr


def test_cache_list_skips_lock_directories(fake_ftp, tmp_path):
    root = tmp_path / "cache"
    assert "Nothing cached" in _run("cache", "list").stderr

    _run("download")
    (root / ".juice.load-locks").mkdir(exist_ok=True)
    lines = _run("cache", "list").stdout.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("juice\t")
    assert lines[0].endswith(str(root / "juice"))


def test_cache_clear_asks_first(fake_ftp, tmp_path):
    _run("download")
    kernel = tmp_path / "cache" / "juice" / "pck" / "test.tpc"

    assert _run("cache", "clear", "juice", input="n\n").exit_code == 1
    assert kernel.exists()

    assert _run("cache", "clear", "juice", input="y\n").exit_code == 0
    assert not kernel.exists()


def test_cache_clear_ignores_spice_directory(tmp_path, monkeypatch):
    """SPICE_DIRECTORY may be the user's own dataset: never delete that."""
    dataset = tmp_path / "dataset"
    _populate(dataset)
    monkeypatch.setenv("SPICE_DIRECTORY", str(dataset))
    assert _run("cache", "clear", "juice", "--yes").exit_code == 0
    assert (dataset / "pck" / "test.tpc").exists()


def test_cache_clear_rejects_unknown_mission():
    assert _run("cache", "clear", "nope", "--yes").exit_code == 1


def test_broken_pipe_is_left_to_click():
    with patch(
        "quick_spice_manager.cli.list_metakernels_via_ftp",
        side_effect=BrokenPipeError,
    ):
        result = _run("list")
    assert isinstance(result.exception, BrokenPipeError)
