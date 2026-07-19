from __future__ import annotations

import argparse
import json
from pathlib import Path

from a2a_proof.config import config_schema

SCHEMA_PATH = Path(__file__).parents[1] / "schema" / "a2a-proof.schema.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    generated = f"{json.dumps(config_schema(), indent=2, sort_keys=True)}\n"

    if args.check:
        if not SCHEMA_PATH.exists() or SCHEMA_PATH.read_text(encoding="utf-8") != generated:
            parser.error("schema/a2a-proof.schema.json is out of date")
        return

    SCHEMA_PATH.parent.mkdir(exist_ok=True)
    SCHEMA_PATH.write_text(generated, encoding="utf-8")


if __name__ == "__main__":
    main()
