"""Utility functions for trueloc."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import dateparser  # type: ignore[import-untyped]
import diskcache  # type: ignore[import-untyped]
import typer

if TYPE_CHECKING:
    from datetime import datetime

CACHE_DIR = Path.home() / ".cache" / "trueloc"
TTL_MUTABLE = 604800  # 7 days for mutable data
TTL_IMMUTABLE = None  # Never expires for immutable data
RATE_LIMIT_BUFFER = 500  # Proactively pause when remaining requests drop below this


def get_file_extension(filename: str) -> str:
    """Extract file extension from filename."""
    path = Path(filename)
    return path.suffix.lower() if path.suffix else path.name.lower()


def get_github_token() -> str:
    """Get GitHub token from gh CLI."""
    result = subprocess.run(
        ["gh", "auth", "token"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def get_cache(no_cache: bool) -> diskcache.Cache:  # noqa: FBT001
    """Get disk cache or in-memory cache."""
    if no_cache:
        return diskcache.Cache(":memory:")
    return diskcache.Cache(str(CACHE_DIR))


def parse_date(date_str: str) -> datetime:
    """Parse a date string using dateparser.

    Supports:
    - Relative: "5d", "2w", "3m", "1y", "5 days ago", "last week", "last month"
    - Absolute: "2024-01-01", "Jan 1 2024"
    """
    # Handle shorthand like "5d", "2w", "3m", "1y"
    shorthand = re.match(r"^(\d+)([dwmy])$", date_str.strip().lower())
    if shorthand:
        num, unit = shorthand.groups()
        unit_map = {"d": "days", "w": "weeks", "m": "months", "y": "years"}
        date_str = f"{num} {unit_map[unit]} ago"

    parsed: datetime | None = dateparser.parse(
        date_str,
        settings={"PREFER_DATES_FROM": "past", "RETURN_AS_TIMEZONE_AWARE": False},
    )
    if parsed is None:
        msg = f"Could not parse date: {date_str!r}"
        raise typer.BadParameter(msg)
    return parsed
