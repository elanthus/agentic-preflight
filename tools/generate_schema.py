"""Regenerate the JSON Schemas under skill/reference/schema/ from the CLI contract models.

Usage: ``uv run python tools/generate_schema.py``. Each file is
``model_json_schema()`` output, serialized with sorted keys so a regeneration that
changes nothing produces no diff; ``tests/test_cli_contract.py`` enforces that.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "skill" / "reference" / "schema"


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> int:
    from agentic_preflight.cli_contract import schemas

    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    wanted = schemas()
    for stale in SCHEMA_DIR.glob("*.json"):
        if stale.name not in wanted:
            stale.unlink()
    for name, schema in wanted.items():
        (SCHEMA_DIR / name).write_text(render(schema), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
