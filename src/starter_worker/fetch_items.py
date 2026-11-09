"""Table function example: ``fetch_items``, wrapping a paginated REST API.

This is the pattern most workers start from: page through an HTTP API and
stream each page to DuckDB as one Arrow batch.

* **Streaming.** ``process`` fetches one page per call and keeps the next page
  number in ``FetchState``, so a 10,000-page API never sits in memory.
* **Politeness.** Requests time out, and 429/5xx responses are retried with
  backoff that honours ``Retry-After``.
* **Credentials.** A bearer token comes from a DuckDB secret if one exists
  (``CREATE SECRET (TYPE starter_api, TOKEN '...')``), otherwise from the
  ``STARTER_API_TOKEN`` environment variable. The token is read from the secret
  on every call and never stored in state, because state can travel back to
  DuckDB over HTTP.

The tests run against ``test/mock_api/server.py``; point ``base_url`` at your
real API once you adapt the parsing in ``_parse_page``.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg, Secret
from vgi.catalog import SecretTypeSpec
from vgi.invocation import BindResponse
from vgi.metadata import FunctionExample
from vgi.table_function import (
    BindParams,
    OutputCollector,
    ProcessParams,
    TableFunctionGenerator,
    init_single_worker,
)
from vgi_rpc import ArrowSerializableDataclass

from .meta import column, object_tags

SECRET_TYPE = "starter_api"
TOKEN_ENV_VAR = "STARTER_API_TOKEN"
TIMEOUT_SECONDS = 30.0
MAX_ATTEMPTS = 4
USER_AGENT = "vgi-starter-worker/0.1"

# Registered with DuckDB so users can run CREATE SECRET (TYPE starter_api, TOKEN '...').
API_SECRET_TYPE = SecretTypeSpec(
    name=SECRET_TYPE,
    description="Bearer token for the API that fetch_items reads.",
    schema=pa.schema([pa.field("token", pa.string(), metadata={"redact": "true"})]),
)

OUTPUT_SCHEMA = pa.schema(
    [
        column("id", pa.int64(), "Item identifier assigned by the API.", nullable=False),
        column("name", pa.string(), "Item display name."),
        column("category", pa.string(), "Item category, e.g. 'hardware'."),
        column("price", pa.float64(), "Item price in US dollars."),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class FetchArgs:
    """Arguments for ``fetch_items(base_url, page_size := 100)``."""

    base_url: Annotated[
        str,
        Arg(
            0, doc="Root URL of the API, e.g. 'https://vgi-starter-api.query-farm.services/v1'.", pattern=r"^https?://"
        ),
    ]
    page_size: Annotated[int, Arg("page_size", default=100, doc="Items requested per page; 1 to 1000.", ge=1, le=1000)]


@dataclass(kw_only=True)
class FetchState(ArrowSerializableDataclass):
    """Cursor: the next page to request, or 0 once the API says there are no more."""

    next_page: int = 1


class ApiError(RuntimeError):
    """The API returned an error we will not retry."""


def _token(params: ProcessParams[FetchArgs]) -> str | None:
    """Return the bearer token: DuckDB secret first, then the environment."""
    for secret in params.secrets.of_type(SECRET_TYPE):
        value = secret.get("token")
        if value is not None and value.as_py():
            return str(value.as_py())
    return os.environ.get(TOKEN_ENV_VAR) or None


def _get_json(url: str, token: str | None) -> Any:
    """GET ``url`` and decode JSON, retrying 429 and 5xx with backoff."""
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return json.load(response)
        except urllib.error.HTTPError as err:
            retryable = err.code == 429 or err.code >= 500
            if not retryable or attempt == MAX_ATTEMPTS:
                raise ApiError(
                    f"GET {url} failed with HTTP {err.code}: {err.read(500).decode(errors='replace')}"
                ) from err
            retry_after = err.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 0.5 * 2**attempt
            time.sleep(min(delay, 30.0))
        except urllib.error.URLError as err:
            if attempt == MAX_ATTEMPTS:
                raise ApiError(f"GET {url} failed: {err.reason}") from err
            time.sleep(0.5 * 2**attempt)
    raise AssertionError("unreachable")


def _parse_page(body: Any) -> tuple[list[dict[str, Any]], int | None]:
    """Split one API response into rows and the next page number. Adapt this to your API."""
    return list(body["items"]), body.get("next_page")


@init_single_worker
class FetchItems(TableFunctionGenerator[FetchArgs, FetchState]):
    """Page through ``<base_url>/items`` and stream every item as a row."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = OUTPUT_SCHEMA

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "fetch_items"
        description = "Read every item from a paginated REST API, one page per batch"
        tags = {
            **object_tags(
                title="Fetch Items From API",
                category="api",
                keywords="rest api, http, pagination, fetch, items, json api, bearer token",
                doc_llm=(
                    "Read all items from a paginated JSON REST API at `base_url` and return them as "
                    "rows `(id, name, category, price)`. Pages are requested `page_size` at a time "
                    "(named argument, default 100) and streamed to DuckDB as they arrive. If a "
                    "DuckDB secret of type `starter_api` exists its `token` is sent as a bearer "
                    "token; otherwise the `STARTER_API_TOKEN` environment variable is used. "
                    "Rate-limit (429) and server errors are retried with backoff."
                ),
                doc_md=(
                    "## fetch_items(base_url, page_size := 100)\n\n"
                    "Every item from a paginated REST API, streamed one page per batch.\n\n"
                    "Authenticate with a `starter_api` secret or the `STARTER_API_TOKEN` "
                    "environment variable:\n\n"
                    "```sql\nCREATE SECRET (TYPE starter_api, TOKEN 'your-token');\n```"
                ),
            ),
            "vgi.result_columns_schema": json.dumps(
                [
                    {"name": "id", "type": "BIGINT", "description": "Item identifier assigned by the API."},
                    {"name": "name", "type": "VARCHAR", "description": "Item display name."},
                    {"name": "category", "type": "VARCHAR", "description": "Item category, e.g. 'hardware'."},
                    {"name": "price", "type": "DOUBLE", "description": "Item price in US dollars."},
                ]
            ),
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT id, name, price "
                    "FROM starter.main.fetch_items('https://vgi-starter-api.query-farm.services/v1') "
                    "WHERE category = 'hardware' ORDER BY price DESC LIMIT 5"
                ),
                description="The five priciest hardware items from the public demo API",
            ),
            FunctionExample(
                sql=(
                    "SELECT category, count(*) AS items, avg(price) AS avg_price "
                    "FROM starter.main.fetch_items("
                    "'https://vgi-starter-api.query-farm.services/v1', page_size := 10) "
                    "GROUP BY category ORDER BY category"
                ),
                description="Summarize the API's items by category, ten per request",
            ),
        ]

    @classmethod
    def on_bind(
        cls,
        params: BindParams[FetchArgs],
        *,
        starter_api: Annotated[dict[str, pa.Scalar[Any]] | None, Secret(SECRET_TYPE)] = None,
    ) -> BindResponse:
        """Declare the output schema and ask DuckDB for the optional ``starter_api`` secret."""
        return BindResponse(output_schema=OUTPUT_SCHEMA)

    @classmethod
    def initial_state(cls, params: ProcessParams[FetchArgs]) -> FetchState:
        """Start at page 1."""
        return FetchState()

    @classmethod
    def process(cls, params: ProcessParams[FetchArgs], state: FetchState, out: OutputCollector) -> None:
        """Fetch one page and emit it; finish when the API has no next page."""
        if state.next_page == 0:
            out.finish()
            return
        query = urllib.parse.urlencode({"page": state.next_page, "page_size": params.args.page_size})
        url = f"{params.args.base_url.rstrip('/')}/items?{query}"
        rows, next_page = _parse_page(_get_json(url, _token(params)))
        columns = {f.name: [row.get(f.name) for row in rows] for f in OUTPUT_SCHEMA}
        out.emit(pa.RecordBatch.from_pydict(columns, schema=params.output_schema))
        state.next_page = next_page or 0
