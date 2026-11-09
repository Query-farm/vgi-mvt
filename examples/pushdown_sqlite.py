"""Recipe: push WHERE filters and column selection down into a database.

DuckDB tells a table function which columns the query needs (projection pushdown)
and which simple predicates it can apply at the source (filter pushdown). Turning
those into the source's own query means less data crosses the wire.

    SELECT id, amount FROM pushdown_sqlite.main.orders() WHERE region = 'west' AND amount > 900;

DuckDB removes every predicate it pushes down from its own plan and does not
re-check the rows, so the worker must apply each one exactly. Here only simple
top-level comparisons, IN lists and IS [NOT] NULL are translated into SQL, which
narrows what SQLite returns; ``auto_apply_filters`` then has the SDK re-apply every
pushed predicate to each batch, so the ones not translated (NOT IN, for example)
still hold. Whatever you translate must never drop a matching row. The demo
database is created on first use in the system temp directory.

**Dependency:** vgi-python binds pushed-down predicates with a local engine, so a
worker that sets ``filter_pushdown = True`` needs ``vgi-python[http,haybarn]`` in its
dependencies (the template installs it in the dev group for this recipe).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import pyarrow as pa
from vgi import Worker
from vgi.catalog import Catalog, Schema
from vgi.table_filter_pushdown import ComparisonOp, ConstantFilter, InFilter, IsNotNullFilter, IsNullFilter
from vgi.table_function import (
    OutputCollector,
    ProcessParams,
    TableFunctionGenerator,
    bind_fixed_schema,
    init_single_worker,
)
from vgi_rpc import ArrowSerializableDataclass

log = logging.getLogger(__name__)
DB_PATH = Path(tempfile.gettempdir()) / "vgi_recipe_orders.sqlite"
PAGE = 500
_SQL_OP = {
    ComparisonOp.EQ: "=",
    ComparisonOp.NE: "<>",
    ComparisonOp.GT: ">",
    ComparisonOp.GE: ">=",
    ComparisonOp.LT: "<",
    ComparisonOp.LE: "<=",
}


def _ensure_db() -> None:
    """Create a deterministic 2,000-row orders table if it does not exist."""
    if DB_PATH.exists():
        return
    tmp = DB_PATH.with_suffix(f".{os.getpid()}.tmp")
    # `with sqlite3.connect()` only commits; closing() also closes the file, which
    # Windows requires before the rename below.
    with closing(sqlite3.connect(tmp)) as con, con:
        con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT NOT NULL, amount INTEGER NOT NULL)")
        regions = ["north", "south", "east", "west"]
        con.executemany(
            "INSERT INTO orders VALUES (?, ?, ?)", [(i, regions[i % 4], (i * 37) % 1000) for i in range(1, 2001)]
        )
    try:
        tmp.replace(DB_PATH)
    except PermissionError:
        # Another worker process created (and has open) the database first; use theirs.
        tmp.unlink(missing_ok=True)
        if not DB_PATH.exists():
            raise


# Module level on purpose: the SDK evaluates class-body annotations at runtime.
_ORDER_FIELDS: list[pa.Field[Any]] = [
    pa.field("id", pa.int64(), nullable=False),
    pa.field("region", pa.string()),
    pa.field("amount", pa.int64()),
]


@dataclass(slots=True, frozen=True, kw_only=True)
class NoArgs:
    """``orders()`` takes no arguments."""


@dataclass(kw_only=True)
class Cursor(ArrowSerializableDataclass):
    """Keyset pagination cursor: the last id emitted."""

    last_id: int = 0


def _where(params: ProcessParams[NoArgs]) -> tuple[list[str], list[Any]]:
    """Translate the filters SQLite can evaluate into parameterized SQL."""
    clauses: list[str] = []
    values: list[Any] = []
    filters = params.current_pushdown_filters
    for f in filters.filters if filters else ():
        col = f'"{f.column_name}"'
        if isinstance(f, ConstantFilter):
            clauses.append(f"{col} {_SQL_OP[f.op]} ?")
            values.append(f.value.as_py())
        elif isinstance(f, InFilter):
            items = f.values.to_pylist()
            if items:
                clauses.append(f"{col} IN ({', '.join('?' * len(items))})")
                values.extend(items)
        elif isinstance(f, IsNullFilter):
            clauses.append(f"{col} IS NULL")
        elif isinstance(f, IsNotNullFilter):
            clauses.append(f"{col} IS NOT NULL")
        # Anything else: not translated; auto_apply_filters applies it.
    return clauses, values


@bind_fixed_schema
@init_single_worker
class Orders(TableFunctionGenerator[NoArgs, Cursor]):
    """Scan the SQLite ``orders`` table, pushing filters and projections into SQL."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(_ORDER_FIELDS)

    class Meta:
        """Opt in to both kinds of pushdown."""

        name = "orders"
        description = "Orders from a SQLite database, with filter and projection pushdown"
        filter_pushdown = True
        # DuckDB drops pushed predicates from its plan; re-apply them all exactly.
        auto_apply_filters = True
        projection_pushdown = True

    @classmethod
    def initial_state(cls, params: ProcessParams[NoArgs]) -> Cursor:
        """Start before the first id."""
        _ensure_db()
        return Cursor()

    @classmethod
    def process(cls, params: ProcessParams[NoArgs], state: Cursor, out: OutputCollector) -> None:
        """Fetch one page of matching rows, selecting only the projected columns."""
        wanted = params.output_schema.names
        clauses, values = _where(params)
        sql = (
            f"SELECT id, {', '.join(f'"{c}"' for c in wanted) or 'id'} FROM orders "
            f"WHERE {' AND '.join(['id > ?', *clauses])} ORDER BY id LIMIT {PAGE}"
        )
        log.debug("sqlite: %s %s", sql, values)
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as con:
            rows = con.execute(sql, [state.last_id, *values]).fetchall()
        if not rows:
            out.finish()
            return
        state.last_id = rows[-1][0]
        columns = {name: [r[i + 1] for r in rows] for i, name in enumerate(wanted)}
        out.emit(pa.RecordBatch.from_pydict(columns, schema=params.output_schema))


class RecipeWorker(Worker):
    """Serves the ``pushdown_sqlite`` catalog."""

    catalog = Catalog(name="pushdown_sqlite", schemas=[Schema(path=["main"], functions=[Orders])])
