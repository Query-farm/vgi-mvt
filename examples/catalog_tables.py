"""Recipe: expose a dataset as a named table, with a primary key.

A catalog table is queried by name, with no parentheses, like any DuckDB table:

    SELECT * FROM catalog_tables.main.countries WHERE continent = 'Europe';

Use a table for a fixed or slowly changing dataset (reference data, a directory, a
lookup list). Use a table function when the caller supplies arguments. Declaring
NOT NULL and a primary key tells the optimizer, and agents, what identifies a row.

Constraints are the worker's own attestation: declare a record as ENFORCED with
ALL_ROWS validation only if every row the table can return satisfies it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import pyarrow as pa
from vgi import Worker
from vgi.catalog import Catalog, Schema, Table
from vgi.catalog.constraints import constraint_record
from vgi.table_function import (
    OutputCollector,
    ProcessParams,
    TableFunctionGenerator,
    bind_fixed_schema,
    init_single_worker,
)

COUNTRIES = {
    "code": ["DE", "FR", "JP", "KE", "US"],
    "name": ["Germany", "France", "Japan", "Kenya", "United States"],
    "continent": ["Europe", "Europe", "Asia", "Africa", "North America"],
}


@dataclass(slots=True, frozen=True, kw_only=True)
class NoArgs:
    """The backing function takes no arguments."""


@bind_fixed_schema
@init_single_worker
class CountriesSource(TableFunctionGenerator[NoArgs, None]):
    """Rows for the ``countries`` table."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            pa.field("code", pa.string(), nullable=False),
            pa.field("name", pa.string()),
            pa.field("continent", pa.string()),
        ]
    )

    class Meta:
        """Function metadata."""

        name = "countries_source"
        description = "Backing rows for the countries table"

    @classmethod
    def process(cls, params: ProcessParams[NoArgs], state: None, out: OutputCollector) -> None:
        """Emit every country once."""
        out.emit(pa.RecordBatch.from_pydict(COUNTRIES, schema=params.output_schema))
        out.finish()


COUNTRIES_TABLE = Table(
    name="countries",
    function=CountriesSource,
    comment="A small reference list of countries.",
    constraints=(
        constraint_record(
            ["main"],
            "countries",
            "code_not_null",
            "NOT_NULL",
            [["code"]],
            enforcement="ENFORCED",
            validation="ALL_ROWS",
        ),
        constraint_record(
            ["main"], "countries", "pk", "PRIMARY_KEY", [["code"]], enforcement="ENFORCED", validation="ALL_ROWS"
        ),
    ),
    column_comments={
        "code": "ISO 3166-1 alpha-2 country code.",
        "name": "English short name.",
        "continent": "Continent the country is in.",
    },
)


class RecipeWorker(Worker):
    """Serves the ``catalog_tables`` catalog."""

    catalog = Catalog(
        name="catalog_tables",
        schemas=[Schema(path=["main"], tables=[COUNTRIES_TABLE], functions=[CountriesSource])],
    )
