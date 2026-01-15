"""Pytest configuration and fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import respx

if TYPE_CHECKING:
    from collections.abc import Generator


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
