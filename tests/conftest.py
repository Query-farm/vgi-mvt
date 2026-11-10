"""Shared fixtures: a worker client and the fixture tileset paths."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from vgi.client import Client

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "test" / "fixtures"
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/mvt_encode.py


@pytest.fixture(scope="session")
def client() -> Iterator[Client]:
    """A client that launches the worker as a subprocess, the way DuckDB does."""
    with Client([sys.executable, "-m", "mvt_worker.worker"]) as c:
        yield c
