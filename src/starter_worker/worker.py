"""Assemble the functions into a catalog and run the worker.

To add a function: write it (see ``greet.py`` / ``sequence.py`` /
``fetch_items.py``), then add the class to ``FUNCTIONS`` below.

Run it:

    vgi-starter-worker                      # stdio: what DuckDB launches by default
    vgi-starter-worker --http --port 8000   # HTTP: for Docker / hosted deploys
    vgi-starter-worker --help

Attach it from DuckDB:

    ATTACH 'starter' (TYPE vgi, LOCATION '/abs/path/to/.venv/bin/vgi-starter-worker');
"""

from __future__ import annotations

import json
from typing import Any

from vgi import Worker
from vgi.catalog import Catalog, Schema

from .fetch_items import API_SECRET_TYPE, FetchItems
from .greet import Greet, GreetWithStyle
from .meta import (
    CATALOG_DOC_MD,
    CATALOG_NAME,
    CATALOG_TAGS,
    SCHEMA_CATEGORIES,
    SCHEMA_DOC_LLM,
    SCHEMA_DOC_MD,
    SOURCE_URL,
    keywords_array,
)
from .sequence import SequenceTable

FUNCTIONS: list[type[Any]] = [Greet, GreetWithStyle, SequenceTable, FetchItems]


def _stamp_example_queries(functions: list[type[Any]]) -> list[dict[str, str]]:
    """Publish each function's examples as ``vgi.example_queries`` (with descriptions).

    Overloads share one SQL name, so their examples are merged per name.
    Returns every example, for the schema-level list.
    """
    by_name: dict[str, list[dict[str, str]]] = {}
    for fn in functions:
        bucket = by_name.setdefault(fn.Meta.name, [])
        for ex in getattr(fn.Meta, "examples", []):
            entry = {"description": ex.description, "sql": ex.sql}
            if entry not in bucket:
                bucket.append(entry)
    for fn in functions:
        fn.Meta.tags = {**fn.Meta.tags, "vgi.example_queries": json.dumps(by_name[fn.Meta.name])}
    return [entry for entries in by_name.values() for entry in entries]


_ALL_EXAMPLES = _stamp_example_queries(FUNCTIONS)


class StarterWorker(Worker):
    """The worker process: one catalog, one ``main`` schema."""

    catalog = Catalog(
        name=CATALOG_NAME,
        comment=CATALOG_DOC_MD,
        source_url=SOURCE_URL,
        tags=CATALOG_TAGS,
        schemas=[
            Schema(
                path=["main"],
                comment=SCHEMA_DOC_MD,
                functions=FUNCTIONS,
                tags={
                    "vgi.title": "Starter Functions",
                    "domain": "examples",
                    "vgi.keywords": keywords_array("greeting, sequence, generator, rest api, pagination"),
                    "vgi.doc_llm": SCHEMA_DOC_LLM,
                    "vgi.doc_md": SCHEMA_DOC_MD,
                    "vgi.categories": json.dumps(SCHEMA_CATEGORIES),
                    "vgi.example_queries": json.dumps(_ALL_EXAMPLES),
                },
            )
        ],
    )

    secret_types = [API_SECRET_TYPE]


def main() -> None:
    """Console-script entry point: parse ``--http`` / ``--unix`` / logging flags and serve."""
    StarterWorker.main()


if __name__ == "__main__":
    main()
