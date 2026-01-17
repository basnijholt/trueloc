"""Pytest configuration and fixtures."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
import respx

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: Path) -> Generator[None, None, None]:
    """Prevent tests from using the real cache directory.

    This fixture patches CACHE_DIR in all modules that import it to use a
    temporary directory, ensuring tests never touch ~/.cache/trueloc/.
    """
    test_cache_dir = tmp_path / "test_cache"
    test_cache_dir.mkdir(parents=True, exist_ok=True)

    with (
        patch("trueloc.utils.CACHE_DIR", test_cache_dir),
        patch("trueloc.cli.CACHE_DIR", test_cache_dir),
    ):
        yield


@pytest.fixture(autouse=True)
def _respx_mock() -> Generator[respx.Router, None, None]:
    """Set up respx mock for all tests.

    Uses assert_all_mocked=True to fail on any unmocked request.
    Tests must add routes for any HTTP calls they make.
    """
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


@pytest.fixture
def respx_mock(_respx_mock: respx.Router) -> respx.Router:
    """Provide access to the respx router for tests that need to add routes."""
    return _respx_mock
