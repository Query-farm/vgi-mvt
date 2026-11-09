"""Unit tests. The SQL-level tests live in test/sql and run through DuckDB."""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest
from vgi.arguments import Arguments
from vgi.client import Client

from starter_worker.greet import _greet

SCHEMA = ["main"]


def test_greet_casual_and_formal() -> None:
    names = pa.array(["Ada", None, "Grace"])
    assert _greet(names, "casual").to_pylist() == ["Hi, Ada!", None, "Hi, Grace!"]
    assert _greet(names, "formal").to_pylist() == ["Good day, Ada.", None, "Good day, Grace."]


def _table(client: Client, name: str, *positional: Any, **named: Any) -> pa.Table:
    arguments = Arguments(
        positional=tuple(pa.scalar(v) for v in positional),
        named={k: pa.scalar(v) for k, v in named.items()},
    )
    batches = list(client.table_function(function_name=name, schema_path=SCHEMA, arguments=arguments))
    return pa.Table.from_batches(batches)


def test_sequence_table_default_step(client: Client) -> None:
    table = _table(client, "sequence_table", 4)
    assert table.column("n").to_pylist() == [0, 1, 2, 3]
    assert table.column("squared").to_pylist() == [0, 1, 4, 9]


def test_sequence_table_named_step(client: Client) -> None:
    table = _table(client, "sequence_table", 3, step=5)
    assert table.column("n").to_pylist() == [0, 5, 10]


def test_sequence_table_streams_in_batches(client: Client) -> None:
    table = _table(client, "sequence_table", 25_000)
    assert table.num_rows == 25_000
    assert table.column("n").to_pylist()[-1] == 24_999


def test_fetch_items_pages_through_api(client: Client, mock_api: str) -> None:
    table = _table(client, "fetch_items", mock_api, page_size=4)
    assert table.num_rows == 25
    assert table.column("id").to_pylist() == list(range(1, 26))
    assert set(table.column("category").to_pylist()) == {"hardware", "software", "service"}


def test_fetch_items_rejects_bad_url(client: Client) -> None:
    with pytest.raises(Exception, match="required pattern"):
        _table(client, "fetch_items", "ftp://example.com")
