"""Discovery metadata: the one file to edit when you rename or extend the worker.

DuckDB, coding agents and the ``vgi-lint`` metadata checker all read these
tags. Keep every object documented and ``make lint`` stays at 100.

* Catalog-level tags (title, license, author, links, long description) live in
  ``CATALOG_TAGS`` below.
* Every function and table carries ``object_tags(...)``: a title, a category
  from ``SCHEMA_CATEGORIES``, search keywords, and LLM- and human-facing docs.
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa

CATALOG_NAME = "starter"

SOURCE_URL = "https://github.com/Query-farm/vgi-starter-python"

# Categories group functions in docs and agent tools. Every object's
# ``vgi.category`` must name one of these.
SCHEMA_CATEGORIES = [
    {"name": "text", "title": "Text", "description": "Functions that build or transform strings."},
    {"name": "generators", "title": "Generators", "description": "Table functions that generate rows."},
    {"name": "api", "title": "REST APIs", "description": "Table functions that read from HTTP APIs."},
]


def keywords_array(keywords: str) -> str:
    """Turn ``"a, b, c"`` into the JSON array string ``vgi.keywords`` requires."""
    return json.dumps([k.strip() for k in keywords.split(",") if k.strip()])


def object_tags(*, title: str, category: str, keywords: str, doc_llm: str, doc_md: str) -> dict[str, str]:
    """Build the standard per-object discovery tags."""
    if category not in {c["name"] for c in SCHEMA_CATEGORIES}:
        raise ValueError(f"unknown category {category!r}; add it to SCHEMA_CATEGORIES")
    return {
        "vgi.title": title,
        "vgi.category": category,
        "vgi.keywords": keywords_array(keywords),
        "vgi.doc_llm": doc_llm,
        "vgi.doc_md": doc_md,
    }


def column(name: str, type_: pa.DataType, comment: str, *, nullable: bool = True) -> pa.Field[Any]:
    """Build an Arrow field whose ``comment`` shows up in DuckDB's ``DESCRIBE`` and agent tools."""
    return pa.field(name, type_, nullable=nullable, metadata={b"comment": comment.encode()})


CATALOG_DOC_LLM = (
    "An example VGI worker generated from the Query.Farm starter template. It shows the three "
    "shapes most workers need: a vectorized scalar function (`greet`, with a one- and two-argument "
    "overload), a deterministic table function with a named default argument (`sequence_table`), "
    "and a table function that pages through a REST API with retries and an optional bearer token "
    "taken from a DuckDB secret or an environment variable (`fetch_items`). Replace these with "
    "your own functions; keep the metadata complete so agents can discover and call them."
)

CATALOG_DOC_MD = (
    "# VGI starter worker\n\n"
    "**An example [VGI](https://query.farm/vgi) worker that brings custom functions into DuckDB "
    "SQL.** It was generated from the Query.Farm starter template and is meant to be renamed and "
    "rewritten: every function here demonstrates one pattern you will reuse.\n\n"
    "- `greet(name)` and `greet(name, style)`: a vectorized scalar with an overload and a "
    "constrained constant argument.\n"
    "- `sequence_table(count, step := 1)`: a deterministic table function that streams its "
    "output in batches.\n"
    "- `fetch_items(base_url, page_size := 100)`: wraps a paginated REST API, retrying politely "
    "and reading a bearer token from a DuckDB secret or the environment.\n\n"
    "The worker speaks Apache Arrow to DuckDB over stdin/stdout, a Unix socket, or HTTP, so the "
    "same code runs as a local subprocess or a hosted service. See the "
    "[VGI documentation](https://query.farm/vgi/docs/) to learn more."
)

SCHEMA_DOC_LLM = (
    "The `main` schema holds the starter template's example functions, one per common worker "
    "pattern. `greet(name)` and `greet(name, style)` are vectorized scalars: use them inside a "
    "projection or predicate, one result per row; `style` is a constant that must be 'casual' or "
    "'formal'. `sequence_table(count, step := 1)` is a deterministic table function that returns "
    "`count` rows of `(n, squared)`; use it for demos, tests, and as a template for generators. "
    "`fetch_items(base_url, page_size := 100)` reads every item from a paginated JSON REST API as "
    "`(id, name, category, price)` rows, streaming one page per batch, retrying rate-limited "
    "requests, and authenticating with a `starter_api` DuckDB secret or the `STARTER_API_TOKEN` "
    "environment variable. Scalars cannot take named arguments; table functions can."
)

SCHEMA_DOC_MD = (
    "## main\n\n"
    "The starter template's example functions, one per common worker pattern.\n\n"
    "- **Scalars** answer one question per row and slot into a projection or predicate.\n"
    "- **Table functions** return sets of rows, streamed to DuckDB in Arrow batches.\n"
    "- **REST readers** page through an HTTP API and authenticate with a DuckDB secret."
)

AGENT_TEST_TASKS = [
    {"name": "formal_greeting", "prompt": "Using the starter worker, greet the name 'Grace' formally."},
    {
        "name": "sum_of_squares",
        "prompt": "Using the starter worker, what is the sum of the squared column for the first 10 rows "
        "of the sequence with the default step?",
    },
    {
        "name": "stepped_sequence",
        "prompt": "List the n values of a 4-row sequence that steps by 5, in ascending order.",
    },
    {
        "name": "items_per_category",
        "prompt": (
            "The items API is running at https://vgi-starter-api.query-farm.services/v1. "
            "How many items does it have in each category?"
        ),
    },
]

CATALOG_TAGS = {
    "vgi.title": "VGI Starter Worker",
    "vgi.subtitle": "Example DuckDB functions to rename and replace: a scalar, a table function and a REST API reader.",
    "vgi.doc_llm": CATALOG_DOC_LLM,
    "vgi.doc_md": CATALOG_DOC_MD,
    "vgi.source_url": SOURCE_URL,
    "vgi.icon_url": "https://query.farm/media-kit/products/vgi/vgi-logo.png",
    "vgi.license": "0BSD",
    "vgi.author": "Query.Farm",
    "vgi.copyright": "Copyright 2026 Query.Farm LLC",
    "vgi.keywords": keywords_array("vgi, duckdb, starter template, example worker, rest api, table function"),
    "vgi.support_contact": f"{SOURCE_URL}/issues",
    "vgi.support_policy_url": f"{SOURCE_URL}#support",
    # Public analyst tasks for `vgi-lint simulate`; private graders live in vgi-agent-tests.yaml.
    "vgi.agent_test_tasks": json.dumps(AGENT_TEST_TASKS),
}
