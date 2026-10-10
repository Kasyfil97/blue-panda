"""Standalone full-table metadata generator — experimental CLI.

Mirrors production generate_metadata_async: validates the table, fetches system
context + BM25 table hits, optionally runs Confluence discovery, resolves every
column through the 6-stage chain, generates a table description and business
titles, and reports a GenerationSummary (including auto-approve eligibility).

Usage:
    python generate_metadata.py <table.json> [--force] [--out result.json]

The input JSON must look like:
    {
      "TableName": "MY_TABLE",
      "SourceSchema": "optional",
      "TableDescription": "",              # optional; generated if missing/--force
      "Columns": [
        {"ColumnName": "cust_bal_amt", "ColumnDescription": "", "ColumnDataType": "DECIMAL"},
        ...
      ]
    }

Edit prompts in mage_flow/prompts.py and knobs/flags in mage_flow/config.py.
"""

import argparse
import json
import sys

from mage_flow import default_settings, generate_metadata
from mage_flow.flow import NoGenerationNeeded
from mage_flow.trace import configure_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate metadata for a whole table.")
    parser.add_argument("table_json", help="Path to the input table JSON file")
    parser.add_argument("--force", action="store_true", help="Regenerate even when descriptions already exist")
    parser.add_argument("--out", help="Write the full result JSON to this path")
    parser.add_argument("--business-title", action="store_true", help="Also generate column business titles")
    parser.add_argument("--kata", action="store_true", help="Enable the KATA evidence stage (needs creds)")
    parser.add_argument("--confluence", action="store_true", help="Enable the Confluence fallback stage (needs creds)")
    parser.add_argument("--quiet", action="store_true", help="Silence the per-step flow trace (only warnings)")
    args = parser.parse_args()

    configure_logging("WARNING" if args.quiet else "INFO")

    with open(args.table_json, "r", encoding="utf-8") as f:
        table = json.load(f)

    settings = default_settings()
    if args.business_title:
        settings["business_title"]["enabled"] = True
    if args.kata:
        settings["kata"]["enabled"] = True
    if args.confluence:
        settings["confluence_fallback"]["enabled"] = True

    try:
        result = generate_metadata(table, force_generate=args.force, settings=settings)
    except NoGenerationNeeded as exc:
        print(f"No generation needed: {exc}")
        sys.exit(0)

    summary = result.get("GenerationSummary", {})
    print("\n" + "=" * 60)
    print(f"Table                 : {result.get('TableName')}")
    print(f"Table description      : {result.get('TableDescription')}")
    print(f"Total columns          : {summary.get('total_columns')}")
    print(f"Failed columns         : {summary.get('failed_columns')}")
    print(f"Table desc generated   : {summary.get('table_description_generated')}")
    print(f"Auto-approve eligible  : {summary.get('auto_approve_eligible')}")
    print(f"Confluence fallback    : {summary.get('confluence_fallback', {}).get('status')}")
    print("\nPer-column resolutions:")
    for outcome in summary.get("outcomes", []):
        print(f"  - {outcome['column_name']}: {outcome.get('resolution', outcome.get('status'))}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\nFull result written to {args.out}")


if __name__ == "__main__":
    main()
