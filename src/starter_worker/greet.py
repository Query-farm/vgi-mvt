"""Scalar function example: ``greet``.

A scalar maps each input row to one output row. VGI hands ``compute`` a whole
Arrow column at a time (not one value), so use ``pyarrow.compute`` for speed.

SQL has no named arguments for scalar functions, so an optional argument is
an *overload*: two classes that share the SQL name ``greet`` and differ in
arity. DuckDB picks one by the number of arguments.

``style`` is a ``ConstParam``. It must be a constant in the query (not a
column), and its ``choices`` are enforced when the query is bound, so
``greet('Ada', 'loud')`` fails with a helpful error before any row runs.
"""

from __future__ import annotations

from typing import Annotated, cast

import pyarrow as pa
import pyarrow.compute as pc
from vgi.arguments import ConstParam, Param, Returns
from vgi.metadata import FunctionExample
from vgi.scalar_function import ScalarFunction

from .meta import object_tags

STYLES = ("casual", "formal")

_PREFIX = {"casual": "Hi, ", "formal": "Good day, "}
_SUFFIX = {"casual": "!", "formal": "."}

_NAME_DOC = "Person or thing to greet; NULL in, NULL out."
_STYLE_DOC = "Greeting tone: 'casual' gives 'Hi, <name>!', 'formal' gives 'Good day, <name>.'"


def _greet(names: pa.StringArray, style: str) -> pa.StringArray:
    """Build greetings for a whole column; NULL names stay NULL."""
    # pyarrow-stubs has no overload mixing str literals with arrays; the call is valid.
    result = pc.binary_join_element_wise(_PREFIX[style], names, _SUFFIX[style], "")  # type: ignore[call-overload]
    return cast("pa.StringArray", result)


class Greet(ScalarFunction):
    """``greet(name)``: a casual greeting for every row."""

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "greet"
        description = "Greet a name casually: greet('Ada') returns 'Hi, Ada!'"
        tags = object_tags(
            title="Greet (casual)",
            category="text",
            keywords="greet, greeting, hello, salutation, welcome message",
            doc_llm=(
                "Return a **casual greeting** for each input name: `greet('Ada')` returns "
                "`'Hi, Ada!'`. NULL names produce NULL. This is the one-argument overload; "
                "call `greet(name, style)` for the formal tone. It is a vectorized scalar, "
                "so it works over a whole column: `SELECT starter.main.greet(name) FROM people`."
            ),
            doc_md=(
                "## greet(name)\n\n"
                "A casual greeting for each row: `greet('Ada')` returns `'Hi, Ada!'`.\n\n"
                "NULL in, NULL out. See `greet(name, style)` for the formal tone."
            ),
        )
        examples = [
            FunctionExample(
                sql="SELECT starter.main.greet('Ada') AS greeting",
                description="Greet one name casually",
            ),
        ]

    @classmethod
    def compute(
        cls,
        name: Annotated[pa.StringArray, Param(doc=_NAME_DOC)],
    ) -> Annotated[pa.StringArray, Returns()]:
        """Return ``Hi, <name>!`` for every row."""
        return _greet(name, "casual")


class GreetWithStyle(ScalarFunction):
    """``greet(name, style)``: a greeting in the chosen tone."""

    class Meta:
        """Function metadata surfaced to DuckDB, agents and vgi-lint."""

        name = "greet"
        description = "Greet a name in a chosen tone: greet('Ada', 'formal') returns 'Good day, Ada.'"
        tags = object_tags(
            title="Greet (with tone)",
            category="text",
            keywords="greet, greeting, formal greeting, casual greeting, salutation, tone",
            doc_llm=(
                "Return a greeting for each input name in the requested tone. `style` must be a "
                "constant, either `'casual'` (`'Hi, Ada!'`) or `'formal'` (`'Good day, Ada.'`); "
                "any other value is rejected when the query is planned. NULL names produce NULL."
            ),
            doc_md=(
                "## greet(name, style)\n\n"
                "A greeting in the chosen tone: `'casual'` or `'formal'`.\n\n"
                "`greet('Ada', 'formal')` returns `'Good day, Ada.'`."
            ),
        )
        examples = [
            FunctionExample(
                sql="SELECT starter.main.greet('Ada', 'formal') AS greeting",
                description="Greet one name formally",
            ),
        ]

    @classmethod
    def compute(
        cls,
        name: Annotated[pa.StringArray, Param(doc=_NAME_DOC)],
        style: Annotated[str, ConstParam(_STYLE_DOC, choices=list(STYLES))],
    ) -> Annotated[pa.StringArray, Returns()]:
        """Return a greeting in ``style`` for every row."""
        return _greet(name, style)
