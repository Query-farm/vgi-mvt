"""Table function example: ``sequence_table``.

A table function returns a set of rows. This one is a deterministic generator,
the simplest shape to learn from:

* ``count`` is positional and required; ``step`` is *named* with a default, so
  callers write ``sequence_table(5)`` or ``sequence_table(5, step := 2)``.
  (Named arguments work on table functions, not on scalars.)
* ``ge``/``le`` constraints are enforced before the function runs and are
  published so agents know the valid range.
* Output streams in bounded batches. ``SequenceState`` remembers where the last
  batch stopped. The state is serializable, so the same code works when DuckDB
  talks to the worker over HTTP, where each batch may hit a different process.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, ClassVar

import pyarrow as pa
from vgi.arguments import Arg
from vgi.metadata import FunctionExample
from vgi.table_function import (
    BindParams,
    OutputCollector,
    ProcessParams,
    TableCardinality,
    TableFunctionGenerator,
    bind_fixed_schema,
    init_single_worker,
)
from vgi_rpc import ArrowSerializableDataclass

from .meta import column, object_tags

BATCH_ROWS = 10_000
MAX_COUNT = 1_000_000


@dataclass(slots=True, frozen=True, kw_only=True)
class SequenceArgs:
    """Arguments for ``sequence_table(count, step := 1)``."""

    count: Annotated[int, Arg(0, doc="How many rows to generate, from 0 to 1,000,000.", ge=0, le=MAX_COUNT)]
    step: Annotated[int, Arg("step", default=1, doc="Gap between consecutive n values; at least 1.", ge=1)]


@dataclass(kw_only=True)
class SequenceState(ArrowSerializableDataclass):
    """Cursor: how many rows have been emitted so far."""

    emitted: int = 0


@init_single_worker
@bind_fixed_schema
class SequenceTable(TableFunctionGenerator[SequenceArgs, SequenceState]):
    """Generate ``count`` rows of ``(n, squared)`` with ``n = 0, step, 2*step, ...``."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column("n", pa.int64(), "The generated value: row index times step.", nullable=False),
            column("squared", pa.int64(), "n multiplied by itself.", nullable=False),
        ]
    )

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "sequence_table"
        description = "Generate count rows of (n, squared), stepping n by step"
        tags = {
            **object_tags(
                title="Sequence Table",
                category="generators",
                keywords="sequence, series, range, generate rows, numbers, squares",
                doc_llm=(
                    "Generate a deterministic table of `count` rows with columns `n` and `squared`, "
                    "where `n` runs `0, step, 2*step, ...`. `count` is positional (0 to 1,000,000); "
                    "`step` is a named argument that defaults to 1, for example "
                    "`sequence_table(5, step := 2)`. Rows stream back in batches, so large counts do "
                    "not buffer in memory."
                ),
                doc_md=(
                    "## sequence_table(count, step := 1)\n\n"
                    "`count` rows of `(n, squared)` with `n = 0, step, 2*step, ...`.\n\n"
                    "Deterministic, so it is handy for tests and demos."
                ),
            ),
            "vgi.result_columns_schema": json.dumps(
                [
                    {"name": "n", "type": "BIGINT", "description": "The generated value: row index times step."},
                    {"name": "squared", "type": "BIGINT", "description": "n multiplied by itself."},
                ]
            ),
            "vgi.executable_examples": json.dumps(
                [
                    {
                        "description": "Three rows stepping by two",
                        "sql": "SELECT n, squared FROM starter.main.sequence_table(3, step := 2) ORDER BY n",
                        "expected_result": [[0, 0], [2, 4], [4, 16]],
                    },
                    {
                        "description": "Count the rows generated",
                        "sql": "SELECT count(*) AS rows FROM starter.main.sequence_table(1000)",
                        "expected_result": [[1000]],
                    },
                ]
            ),
        }
        examples = [
            FunctionExample(
                sql="SELECT n, squared FROM starter.main.sequence_table(5) ORDER BY n",
                description="The first five values and their squares",
            ),
            FunctionExample(
                sql="SELECT n FROM starter.main.sequence_table(4, step := 10) ORDER BY n",
                description="Four values stepping by ten",
            ),
        ]

    @classmethod
    def cardinality(cls, params: BindParams[SequenceArgs]) -> TableCardinality:
        """Tell the planner exactly how many rows to expect."""
        return TableCardinality(estimate=params.args.count, max=params.args.count)

    @classmethod
    def initial_state(cls, params: ProcessParams[SequenceArgs]) -> SequenceState:
        """Start the cursor at zero."""
        return SequenceState()

    @classmethod
    def process(cls, params: ProcessParams[SequenceArgs], state: SequenceState, out: OutputCollector) -> None:
        """Emit one bounded batch per call; finish when ``count`` rows are out."""
        count, step = params.args.count, params.args.step
        if state.emitted >= count:
            out.finish()
            return
        rows = min(BATCH_ROWS, count - state.emitted)
        n = [(state.emitted + i) * step for i in range(rows)]
        out.emit(pa.RecordBatch.from_pydict({"n": n, "squared": [v * v for v in n]}, schema=params.output_schema))
        state.emitted += rows
