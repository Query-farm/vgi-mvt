"""Run one recipe as a worker: ``python examples/run.py <recipe> [worker flags]``.

ATTACH 'aggregate' (TYPE vgi, LOCATION '/abs/.venv/bin/python /abs/examples/run.py aggregate');
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

RECIPES = ["transform_rows", "aggregate", "pushdown_sqlite", "cache_expensive_call", "catalog_tables", "copy_to_sink"]


def main() -> None:
    """Import ``examples/<recipe>.py`` and serve its ``RecipeWorker``."""
    if len(sys.argv) < 2 or sys.argv[1] not in RECIPES:
        sys.exit(f"usage: run.py <{'|'.join(RECIPES)}> [worker flags]")
    name = sys.argv.pop(1)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    importlib.import_module(name).RecipeWorker.main()


if __name__ == "__main__":
    main()
