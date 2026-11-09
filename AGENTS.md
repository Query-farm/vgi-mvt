<!-- managed-by: vgi-starters (edit upstream in shared/AGENTS.md.tmpl, then run tools/render.py) -->
# AGENTS.md: working on this VGI worker

This repository is a **VGI worker** written in Python. DuckDB starts it (or connects
to it over HTTP) and calls its functions from SQL; data moves as Apache Arrow
batches. Background reading, in order of usefulness:

1. https://query.farm/vgi/llms.txt — index of all VGI docs, written for agents
2. https://query.farm/vgi/docs/python/ — the Python SDK (vgi-python)
3. https://query.farm/vgi/building-a-worker — concepts: catalogs, schemas, function kinds

## Definition of done

**`make check` passes.** It runs formatting and static checks, unit tests, the SQL
tests over the stdio, launch and http transports, and the metadata quality check
(vgi-lint, at the `fail_on` level in `vgi-lint.toml`). Don't hand work back until it passes. If a
check is wrong, fix the check and say why. Never delete a test to get green.

Faster loops while iterating: `make test-unit`, `make test-sql`, `make lint-explain`.

## Layout

```
src/starter_worker/
  worker.py        # assembles the catalog; add new functions to FUNCTIONS
  meta.py          # catalog/schema metadata + helpers (object_tags, column)
  greet.py         # scalar example (overloads, ConstParam choices)
  sequence.py      # table function example (named args, streaming state)
  fetch_items.py   # REST API table function (pagination, retries, secrets)
tests/             # pytest unit tests (in-process client)
test/sql/          # sqllogictest files run through DuckDB
test/mock_api/     # offline mock of the REST API used by tests
examples/          # recipes: pushdown, caching, catalog tables, aggregates, COPY …
ci/                # integration harness used by `make test-sql` and CI
scripts/           # vgi_template.py (init / template-diff)
vgi-agent-tests.yaml  # private graders for the agent test tasks
```

## Recipe: add a function

1. Create `src/starter_worker/<name>.py`. Copy the closest example: `greet.py` for a
   scalar, `sequence.py` for a generator, `fetch_items.py` for an API.
2. Declare arguments with `Annotated[..., Param(...)]`/`ConstParam(...)` (scalars) or a
   frozen dataclass of `Annotated[..., Arg(...)]` (table functions). Use `Arg(0, ...)`
   for positional arguments and `Arg("name", default=...)` for named ones.
3. Give the class a `class Meta` with `name`, `description`, `tags=object_tags(...)`
   and `examples`.
4. Add the class to `FUNCTIONS` in `src/starter_worker/worker.py`.

Then, for every new function:

1. **Metadata** (`src/starter_worker/meta.py` and each function's `class Meta`): title, category from the schema's category list,
   keywords, `doc_llm` (for agents: what it does, arguments, when to use it) and `doc_md`
   (for humans). Describe each argument, and for table functions describe the result
   columns. Add 1–2 `examples` with fully qualified, analytical SQL
   (`starter.main.fn(...)`): project, filter or aggregate rather than `SELECT *`.
2. **Executable examples:** if the function is deterministic and needs no network,
   add an `executable_examples` entry with `ORDER BY` and an `expected_result`.
3. **SQL test:** add cases to `test/sql/starter.test`, or a new `test/sql/<name>.test`
   with the same header (`require vgi`, `require-env STARTER_WORKER`, then `ATTACH`).
   Avoid expected-error text that depends on SDK wording; use `<REGEX>:.*` for errors you
   don't control.
4. **Unit test** for any non-trivial logic.
5. **Agent task:** add a `{name, prompt}` to the catalog's agent test tasks, and its
   grader (`reference_sql` or `check_sql`) to `vgi-agent-tests.yaml`.
6. Run `make check`.

## Passing the metadata check

`make lint-explain` runs [vgi-lint](https://github.com/Query-farm/vgi-lint-check) against
the built worker with the settings in `vgi-lint.toml`. The linter isn't pinned, so new
rules arrive on their own. When it reports findings:

1. Run `make lint-explain LINT_ARGS="--format agent"`. Findings are grouped by rule:
   each rule's fix is stated once, followed by every object it applies to.
2. For a rule you don't understand, run
   `uvx --prerelease=allow --from 'vgi-lint-check>=2,<3' vgi-lint explain VGI123`.
   For tag names and value shapes, read `vgi-lint spec` rather than guessing.
3. Fix errors first, then warnings, then info. Fix the metadata in the worker, never
   the linter's settings.
4. If a finding is genuinely wrong for this worker, waive it in `vgi-lint.toml` with a
   `kind` and a `reason` (see the comments there). Never add an unexplained `ignore`.

The check executes examples (assurance level L2). `make lint-ai` adds L3: an agent must
solve the tasks in `vgi-agent-tests.yaml` using only the metadata, and the docs are
reviewed. It's optional, so run it before publishing.

## Choosing the function kind

| Need | Kind |
|---|---|
| one value per input row | scalar |
| rows from arguments (API, file, generator) | table function |
| transform the rows of a subquery | table in-out function |
| combine many rows into one value | aggregate |
| a fixed dataset referenced by name | catalog table |
| write data out to a file or service | `COPY … TO` copy function |
| accept INSERT / UPDATE / DELETE | writable catalog table: `Table(insert_function=…, update_function=…, delete_function=…)` |

Recipes for most kinds are in `examples/`. Writable tables have no recipe:

A writable table is a catalog `Table` that names a table in-out function per operation in
`insert_function`, `update_function` and `delete_function`, with the richest result each one
produces in `write_result_modes`. The helpers in `vgi.write_results` (`write_options`,
`write_result_schema`, `write_result_batch`, `row_id_field`) read DuckDB's `write_options`
argument and build the result. UPDATE and DELETE need a scan function that emits
`row_id_field()` and declares projection pushdown. Transactions: override `attach_supports_transactions`
and `catalog_transaction_begin` / `_commit` / `_rollback` on the catalog. See
[Writable tables](https://github.com/Query-farm/vgi-python/blob/main/docs/catalog-interface.md#writable-tables)
in the vgi-python docs.

## Sharp edges (read before writing code)

- **Never write to stdout.** stdout is the Arrow protocol channel, and one stray print
  corrupts the stream. Log to stderr. Use the standard `logging` module (the SDK routes it to stderr); `--debug` or `--log-level DEBUG` turns it up.
- **Scalars have no named arguments.** For an optional scalar argument, add an overload
  (a second function with the same SQL name and one more argument). Named arguments
  (`step := 2`) work on table functions only.
- **Declared constraints are enforced at bind.** `choices`, `ge`, `le` and `pattern` on a
  constant argument are published so DuckDB and agents can discover them, and the SDK
  (vgi-python) rejects a violating value when the query is bound, before your code runs, for
  positional and named arguments alike. `pattern` is an unanchored search (JSON Schema
  semantics) in every SDK: write `^https?://`, not `https?://`. Constraints cover constant
  arguments only: validate the values of a columnar scalar argument in the compute body.
- **Reserved argument names.** DuckDB keywords such as `interval`, `end`, `from`, `order`
  and `table` break `name := value` syntax. Pick another name (`start_date`, `end_date`).
- **State must be serializable.** Table-function state (`initial_state`/`process`) must subclass `ArrowSerializableDataclass`. Over HTTP each batch can be served by a different process, so the cursor travels with the request. Keep it small, and never put secrets in it.
- **Examples must be deterministic.** Add `ORDER BY`, and no `now()`, random values or
  network. Examples that hit a live API go in `examples`, not `executable_examples`.
- **Credentials.** Read them from a DuckDB secret first (`CREATE SECRET (TYPE starter_api, …)`),
  then from an environment variable. Never put them in logs, examples or the repo, and
  keep them out of table-function state unless the state rule above says
  the SDK encrypts it. Workers that need real credentials set `execute: false` on the metadata check.
- **Errors.** Raise errors with a clear message. For HTTP failures, include the status
  code. DuckDB shows the message to the user.
- **Rate limits.** Retry 429 and 5xx with backoff, honour `Retry-After`, and set timeouts on
  every request.
- **Version pins.** Don't bump the vgi-python minor version by hand. A minor release can
  change the wire protocol, which must match the DuckDB extension exactly. Template
  updates (`make template-diff`) carry tested bumps.

## Language notes (Python)

- Scalars receive whole Arrow arrays. Use `pyarrow.compute` (`pc.*`) instead of
  per-row Python loops wherever you can.
- Table functions: `@init_single_worker` for sources that can't be split, plus
  `@bind_fixed_schema` with a `FIXED_SCHEMA` when the output columns don't depend on
  arguments. Otherwise implement `on_bind` and return `BindResponse(output_schema=...)`.
- Emit `pa.RecordBatch.from_pydict(..., schema=params.output_schema)` so column types
  match what was bound. Call `out.finish()` when done.
- Use `column(name, type, comment)` from `meta.py` for output fields. The comment
  becomes the column description in DuckDB and agent tools.
- Dependencies go in `pyproject.toml` `[project.dependencies]`; run `uv lock`. Keep
  them few: every dependency ships to every user of the worker.
- Gates: `ruff format`, `ruff check`, `mypy --strict`, `pydoclint`.

## Useful prompts

- "Add a scalar `slugify(text VARCHAR) -> VARCHAR` with full metadata and tests."
- "Replace `fetch_items` with a table function for <API>. Map its JSON to typed columns,
  read the token from a `starter_api` secret, and add mock-server tests."
- "Add a catalog table `countries` (code, name) with a primary key, following
  `examples/catalog_tables`."
