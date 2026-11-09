"""Recipe: an aggregate function, driven by GROUP BY.

    SELECT category, aggregate.main.int_sum(amount) FROM sales GROUP BY category;

Aggregates keep per-group state across three phases:

- ``update``   fold a batch of values into each group's state
- ``combine``  merge partial states for the same group (parallel execution)
- ``finalize`` turn each group's state into one output value

State must be serializable (``ArrowSerializableDataclass``) because partial states
can be combined in a different process.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import pyarrow as pa
from vgi import Worker
from vgi.aggregate_function import AggregateFunction
from vgi.arguments import Param, Returns
from vgi.catalog import Catalog, Schema
from vgi.metadata import DistinctDependence, NullHandling, OrderDependence
from vgi.table_function import ProcessParams
from vgi_rpc import ArrowSerializableDataclass, ArrowType


@dataclass(kw_only=True)
class SumState(ArrowSerializableDataclass):
    """Running total for one group."""

    total: Annotated[int, ArrowType(pa.int64())] = 0


class IntSum(AggregateFunction[SumState]):
    """Sum a BIGINT column per group."""

    class Meta:
        """Function metadata, including aggregate semantics the optimizer relies on."""

        name = "int_sum"
        description = "Sum integer values per group"
        null_handling = NullHandling.DEFAULT
        order_dependent = OrderDependence.NOT_ORDER_DEPENDENT
        distinct_dependent = DistinctDependence.NOT_DISTINCT_DEPENDENT

    @classmethod
    def initial_state(cls, params: ProcessParams[None]) -> SumState:
        """Create a fresh accumulator for each group."""
        return SumState()

    @classmethod
    def update(
        cls,
        states: dict[int, SumState],
        group_ids: pa.Int64Array,
        value: Annotated[pa.Int64Array, Param(doc="Values to add up")],
    ) -> None:
        """Add a batch of values to each group's total."""
        grouped = pa.table({"gid": group_ids, "value": value}).group_by("gid").aggregate([("value", "sum")])
        for gid, total in zip(grouped.column("gid").to_pylist(), grouped.column("value_sum").to_pylist(), strict=True):
            if gid is not None and total is not None:
                states[gid] = SumState(total=states[gid].total + total)

    @classmethod
    def combine(cls, source: SumState, target: SumState, params: ProcessParams[None]) -> SumState:
        """Merge two partial totals."""
        return SumState(total=source.total + target.total)

    @classmethod
    def finalize(
        cls, group_ids: pa.Int64Array, states: dict[int, SumState], params: ProcessParams[None]
    ) -> Annotated[pa.RecordBatch, Returns(pa.int64())]:
        """Emit each group's total."""
        totals = [states[g].total if g is not None else None for g in group_ids.to_pylist()]
        return pa.record_batch({"result": pa.array(totals, pa.int64())})


class RecipeWorker(Worker):
    """Serves the ``aggregate`` catalog."""

    catalog = Catalog(name="aggregate", schemas=[Schema(path=["main"], functions=[IntSum])])
