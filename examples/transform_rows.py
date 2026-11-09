"""Recipe: transform the rows of a query with a table in-out function.

A table in-out function receives another query's rows, batch by batch, and emits
rows back. Use it to clean, enrich, filter or reshape data in SQL:

    SELECT * FROM transform_rows.main.positive_only((SELECT * FROM readings));

Here: keep only rows whose ``value`` column is greater than zero. The output schema
equals the input schema, so it works on any table that has a ``value`` column.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import pyarrow as pa
import pyarrow.compute as pc
from vgi import Arg, Worker
from vgi.arguments import TableInput
from vgi.catalog import Catalog, Schema
from vgi.invocation import BindResponse
from vgi.table_function import BindParams, ProcessParams
from vgi.table_in_out_function import OutputCollector, TableInOutGenerator


@dataclass(slots=True, frozen=True, kw_only=True)
class PositiveOnlyArgs:
    """One positional argument: the input query."""

    data: Annotated[TableInput, Arg(0, doc="Rows to filter; must have a numeric `value` column.")]


class PositiveOnly(TableInOutGenerator[PositiveOnlyArgs]):
    """Emit the input rows whose ``value`` is greater than zero."""

    class Meta:
        """Function metadata."""

        name = "positive_only"
        description = "Keep the rows of a query whose value column is positive"

    @classmethod
    def on_bind(cls, params: BindParams[PositiveOnlyArgs]) -> BindResponse:
        """Output columns are the input columns; fail early without a `value` column."""
        schema = params.bind_call.input_schema
        if schema is None or "value" not in schema.names:
            raise ValueError("positive_only needs an input column named 'value'")
        return BindResponse(output_schema=schema)

    @classmethod
    def process(
        cls, params: ProcessParams[PositiveOnlyArgs], state: None, batch: pa.RecordBatch, out: OutputCollector
    ) -> None:
        """Filter each incoming batch and emit the survivors."""
        value = batch.column("value")
        keep = pc.fill_null(pc.greater(value, pa.scalar(0, type=value.type)), pa.scalar(False))  # NULL -> drop
        out.emit(batch.filter(keep))


class RecipeWorker(Worker):
    """Serves the ``transform_rows`` catalog."""

    catalog = Catalog(name="transform_rows", schemas=[Schema(path=["main"], functions=[PositiveOnly])])
