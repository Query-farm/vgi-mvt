"""Shared fixtures: a worker client and the local mock API."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from vgi.client import Client

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def client() -> Iterator[Client]:
    """A client that launches the worker as a subprocess, the way DuckDB does."""
    with Client([sys.executable, "-m", "starter_worker.worker"]) as c:
        yield c


@pytest.fixture(scope="session")
def mock_api() -> Iterator[str]:
    """Start test/mock_api/server.py on a free port and return its base URL."""
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "test" / "mock_api" / "server.py")],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline().strip()
    assert line.startswith("PORT:"), line
    yield f"http://127.0.0.1:{line.removeprefix('PORT:')}"
    proc.terminate()
    proc.wait(timeout=10)
