"""Batch fetch table metadata from API for all tables in a list file.

Reads a list of table names, fetches metadata from API, and saves reformatted
JSON files to an output folder.

Usage:
    python batch_fetch_metadata.py <tables_list.txt> [--output output_dir]

Example:
    python batch_fetch_metadata.py list_of_etutor_used_tables.txt --output metadata_output
"""

import argparse
import json
import sys
from pathlib import Path
import requests
from datetime import datetime


API_BASE_URL = "https://api-bribrain.ddb.dev.bri.co.id/mage//api/v1/metadata"
BEARER_TOKEN = "meB6syT6-RmkanBwSthkMNbMw-02Obb-k-sp4OYIods"


def fetch_table_metadata(table_name: str) -> dict:
    """Fetch table metadata from the API."""
    url = f"{API_BASE_URL}/{table_name}"
    headers = {
        "Authorization": f"Bearer {BEARER_TOKEN}",
        "Content-Type": "application/json",
    }

    response = requests.get(url, headers=headers)
    response.raise_for_status()

    return response.json()


def reformat_for_generate_metadata(api_response: dict) -> dict:
    """Reformat API response to match generate_metadata.py input format."""
    data = api_response.get("data", api_response)

    table_name = data.get("TableName", "")
    table_description = data.get("TableDescription", "")

    source_schema = ""
    columns_data = data.get("Columns", [])
    if columns_data and columns_data[0].get("Knowledge"):
        source_schema = columns_data[0]["Knowledge"][0].get("SourceSchema", "")

    formatted_columns = []
    for col in columns_data:
        formatted_col = {
            "ColumnName": col.get("ColumnName", ""),
            "ColumnDescription": col.get("ColumnDescription", ""),
            "ColumnDataType": col.get("ColumnDataType", "VARCHAR"),
        }
        if formatted_col["ColumnName"]:
            formatted_columns.append(formatted_col)

    formatted = {
        "TableName": table_name,
        "SourceSchema": source_schema,
        "TableDescription": table_description,
        "Columns": formatted_columns,
    }

    return formatted


def load_table_list(list_file: str) -> list[str]:
    """Load table names from a text file, skipping empty lines."""
    tables = []
    with open(list_file, "r", encoding="utf-8") as f:
        for line in f:
            table_name = line.strip()
            if table_name and not table_name.startswith("#"):  # Skip empty lines and comments
                tables.append(table_name)
    return tables


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch fetch table metadata from API for all tables in a list"
    )
    parser.add_argument("tables_list", help="Path to file containing table names (one per line)")
    parser.add_argument(
        "--output",
        default="metadata_output",
        help="Output directory for JSON files (default: metadata_output)"
    )
    args = parser.parse_args()

    # Create output directory
    output_dir = Path(args.output)
    output_dir.mkdir(exist_ok=True)

    # Load table list
    try:
        tables = load_table_list(args.tables_list)
        print(f"Found {len(tables)} tables to fetch")
    except FileNotFoundError:
        print(f"✗ File not found: {args.tables_list}", file=sys.stderr)
        sys.exit(1)

    if not tables:
        print("✗ No tables found in the list file", file=sys.stderr)
        sys.exit(1)

    # Statistics
    successful = 0
    failed = 0
    failed_tables = []

    print("\n" + "=" * 70)
    print("Starting batch fetch...")
    print("=" * 70 + "\n")

    # Fetch metadata for each table
    for idx, table_name in enumerate(tables, 1):
        try:
            print(f"[{idx}/{len(tables)}] Fetching: {table_name}...", end=" ", flush=True)

            # Fetch from API
            api_response = fetch_table_metadata(table_name)

            # Reformat
            formatted_data = reformat_for_generate_metadata(api_response)

            # Save to file
            output_file = output_dir / f"{table_name}.json"
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(formatted_data, f, ensure_ascii=False, indent=2)

            col_count = len(formatted_data.get("Columns", []))
            print(f"✓ ({col_count} columns)")
            successful += 1

        except requests.exceptions.RequestException as e:
            print(f"✗ API Error: {e}")
            failed += 1
            failed_tables.append((table_name, str(e)))

        except json.JSONDecodeError as e:
            print(f"✗ JSON Error: {e}")
            failed += 1
            failed_tables.append((table_name, str(e)))

        except Exception as e:
            print(f"✗ Error: {e}")
            failed += 1
            failed_tables.append((table_name, str(e)))

    # Summary report
    print("\n" + "=" * 70)
    print("BATCH FETCH SUMMARY")
    print("=" * 70)
    print(f"Total tables     : {len(tables)}")
    print(f"Successful       : {successful}")
    print(f"Failed           : {failed}")
    print(f"Output directory : {output_dir.absolute()}")

    if failed_tables:
        print("\n" + "=" * 70)
        print("FAILED TABLES:")
        print("=" * 70)
        for table_name, error in failed_tables:
            print(f"  - {table_name}: {error}")

    # Create summary file
    summary = {
        "timestamp": datetime.now().isoformat(),
        "total_tables": len(tables),
        "successful": successful,
        "failed": failed,
        "output_directory": str(output_dir.absolute()),
        "failed_tables": [{"table": t, "error": e} for t, e in failed_tables],
    }

    summary_file = output_dir / "SUMMARY.json"
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Summary saved to: {summary_file}")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
