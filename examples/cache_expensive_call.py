"""Recipe: let DuckDB cache an expensive result.

The worker *advertises* how long a result stays fresh. DuckDB caches it, and repeat
queries inside the TTL never reach the worker. After the TTL, DuckDB revalidates
with an ETag; if nothing changed the worker answers "not modified" with zero rows.

    SELECT * FROM cache_expensive_call.main.exchange_rates();
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, cast

import pyarrow as pa
from vgi import Worker
from vgi.cache_control import CacheControl
from vgi.catalog import Catalog, Schema
from vgi.protocol import VgiOutputCollector
from vgi.table_function import (
    OutputCollector,
    ProcessParams,
    TableFunctionGenerator,
    bind_fixed_schema,
    init_single_worker,
)

# In a real worker, derive this from the upstream: a Last-Modified header, a version
# column, a content hash. It is the only thing revalidation compares.
DATA_VERSION = '"rates-v1"'
# Module level on purpose: the SDK evaluates class-body annotations at runtime.
_RATE_FIELDS: list[pa.Field[Any]] = [pa.field("currency", pa.string()), pa.field("rate", pa.float64())]
ROWS = {"currency": ["EUR", "GBP", "JPY"], "rate": [1.09, 1.27, 0.0067]}


@dataclass(slots=True, frozen=True, kw_only=True)
class NoArgs:
    """``exchange_rates()`` takes no arguments."""


@bind_fixed_schema
@init_single_worker
class ExchangeRates(TableFunctionGenerator[NoArgs, None]):
    """Rates from a slow upstream, cacheable for five minutes."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(_RATE_FIELDS)

    class Meta:
        """Function metadata."""

        name = "exchange_rates"
        description = "Exchange rates from a slow upstream, cacheable for 5 minutes"

    @classmethod
    def process(cls, params: ProcessParams[NoArgs], state: None, out: OutputCollector) -> None:
        """Emit the rates with cache metadata, or confirm the client's copy is current."""
        vgi_out = cast(VgiOutputCollector, out)
        if params.if_none_match == DATA_VERSION:
            vgi_out.emit(
                pa.RecordBatch.from_pylist([], schema=cls.FIXED_SCHEMA),
                cache_control=CacheControl(ttl=300, etag=DATA_VERSION, revalidatable=True, not_modified=True),
            )
            out.finish()
            return
        # Cache metadata must ride on the FIRST batch.
        vgi_out.emit(
            pa.RecordBatch.from_pydict(ROWS, schema=cls.FIXED_SCHEMA),
            cache_control=CacheControl(
                ttl=300, etag=DATA_VERSION, revalidatable=True, stale_while_revalidate=60, stale_if_error=600
            ),
        )
        out.finish()


class RecipeWorker(Worker):
    """Serves the ``cache_expensive_call`` catalog."""

    catalog = Catalog(name="cache_expensive_call", schemas=[Schema(path=["main"], functions=[ExchangeRates])])
