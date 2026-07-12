"""
Pytest configuration and shared fixtures.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_ftp_connection_lock_dir(tmp_path_factory, monkeypatch):
    """Redirect fixed, non-kernels_dir-scoped cache/lock locations into
    throwaway temp directories for every test.

    Without this, tests that exercise the real (unmocked) download/list code
    paths in ``quick_spice_manager.ftp`` would create lock files and cache
    entries under the real user cache directory
    (``platformdirs.user_cache_dir(...)``), would contend with any real
    concurrent process's connection slots, and could pollute/be polluted by
    real metakernel-listing cache data. (Paths derived from an explicit
    ``kernels_dir`` argument, like the resolution cache, don't need this --
    every test already passes a ``tmp_path``-scoped ``kernels_dir``.)
    """
    lock_dir = tmp_path_factory.mktemp("ftp-connection-locks")
    monkeypatch.setattr(
        "quick_spice_manager.ftp.get_ftp_connection_lock_dir", lambda: lock_dir
    )
    listing_cache_dir = tmp_path_factory.mktemp("metakernel-listing-cache")
    monkeypatch.setattr(
        "quick_spice_manager.ftp.get_metakernel_listing_cache_dir",
        lambda: listing_cache_dir,
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Run integration tests that make real network requests.",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip @pytest.mark.integration tests unless --integration is passed."""
    if config.getoption("--integration"):
        return
    skip = pytest.mark.skip(reason="Pass --integration to run network tests")
    for item in items:
        if item.get_closest_marker("integration"):
            item.add_marker(skip)
