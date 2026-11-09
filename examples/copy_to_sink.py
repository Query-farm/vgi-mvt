"""Recipe: write query results out with ``COPY … TO`` and a custom format.

    ATTACH 'copy_to_sink' (TYPE vgi, LOCATION '…/run.py copy_to_sink');
    COPY (SELECT * FROM t) TO 'out.jsonl' (FORMAT 'copy_to_sink.jsonl_out');

The format name is qualified by the attach alias. ``write`` runs once per batch
(possibly in parallel, possibly in different processes), so it stashes rows in
``params.storage``; ``close`` runs once and writes the file. Swap the file write for
an upload to object storage, a warehouse load, or an API call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated

import pyarrow as pa
from vgi import Arg, Worker
from vgi.catalog import Catalog, Schema
from vgi.copy_to_function import CopyToFunction
from vgi.table_buffering_function import TableBufferingParams


@dataclass(slots=True, frozen=True, kw_only=True)
class WriteOptions:
    """Options for ``COPY … TO … (FORMAT 'copy_to_sink.jsonl_out', …)``."""

    sort_keys: Annotated[bool, Arg("sort_keys", doc="Sort the keys of each JSON object", default=False)] = False


class WriteJsonLines(CopyToFunction[WriteOptions]):
    """Write each row as one JSON object per line."""

    COPY_TO_FORMAT = "jsonl_out"
    COPY_TO_COMMENT = "Newline-delimited JSON, one object per row"

    @classmethod
    def write(
        cls, *, batch: pa.RecordBatch, options: WriteOptions, file_path: str, params: TableBufferingParams[WriteOptions]
    ) -> None:
        """Serialize this batch and stash it (scoped to this COPY by execution_id)."""
        lines = [json.dumps(row, sort_keys=options.sort_keys, default=str) for row in batch.to_pylist()]
        if lines:
            params.storage.state_append(b"shards", b"", "\n".join(lines).encode())

    @classmethod
    def close(cls, *, options: WriteOptions, file_path: str, params: TableBufferingParams[WriteOptions]) -> int:
        """Write every stashed batch to the file once; return the row count."""
        written = 0
        with open(file_path, "w", encoding="utf-8") as handle:
            for _key, chunk in params.storage.state_log_scan(b"shards", b""):
                text = chunk.decode()
                handle.write(text + "\n")
                written += text.count("\n") + 1
        return written


class RecipeWorker(Worker):
    """Serves the ``copy_to_sink`` catalog."""

    catalog = Catalog(name="copy_to_sink", schemas=[Schema(path=["main"], functions=[WriteJsonLines])])
