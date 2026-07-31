"""Standalone single-column description generator — experimental CLI.

Runs the FULL 6-stage resolver chain ported from ms-bribrain-mage
(exact AS400/Confluence -> bm25 AS400/Confluence -> KATA -> bm25 Informatica
-> Confluence fallback -> pure LLM) for one column. All the logic lives in the
`mage_flow/` package next to this file; edit prompts in mage_flow/prompts.py and
knobs/flags in mage_flow/config.py.

Usage:
    python generate_column_description.py <table_name> <column_name>

Notes:
    - Hypothesis generation + understanding-check are ACTIVE (unlike the old
      script where hypothesis was hard-disabled).
    - KATA (OpenSearch/Postgres) and Confluence stages are OFF by default; enable
      them in mage_flow/config.py SETTINGS once creds are configured.
"""

import json
import sys

from mage_flow import default_settings, generate_column
from mage_flow.trace import configure_logging


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python generate_column_description.py <table_name> <column_name>")
        sys.exit(1)

    # Emit the full flow trace (BM25 / KATA / Confluence / hypothesis / LLM) so
    # the resolution path is auditable. Set to "WARNING" to silence.
    configure_logging("INFO")

    table_name, col_name = sys.argv[1], sys.argv[2]

    # Tweak per-run settings here (or edit mage_flow/config.py for defaults).
    settings = default_settings()
    # settings["business_title"]["enabled"] = True
    # settings["kata"]["enabled"] = True
    # settings["confluence_fallback"]["enabled"] = True

    result = generate_column(table_name, col_name, settings=settings)

    print("\n" + "=" * 60)
    print(f"Table    : {result['table_name']}")
    print(f"Column   : {result['col_name']}")
    print(f"Resolver : {result['resolver']}")
    print(f"Result   : {result['description']}")
    if result.get("business_title"):
        print(f"BizTitle : {result['business_title']}")
    print("Knowledge:")
    print(json.dumps(result["knowledges"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
