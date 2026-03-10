"""
Pytest configuration and shared fixtures.
"""

import pytest


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
