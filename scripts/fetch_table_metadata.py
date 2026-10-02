"""Fetch table metadata from API and reformat for generate_metadata.py.

Usage:
    python fetch_table_metadata.py <table_name> [--out result.json]

Example:
    python fetch_table_metadata.py asrs_fact_savingmaster --out table_metadata.json
"""

import argparse
import json
import sys
import requests


API_BASE_URL = "https://api-bribrain.ddb.dev.bri.co.id/mage//api/v1/metadata"
BEARER_TOKEN = "meB6syT6-RmkanBwSthkMNbMw-02Obb-k-sp4OYIods"


def fetch_table_metadata(table_name: str) -> dict:
    """Fetch table metadata from the API."""
    url = f"{API_BASE_URL}/{table_name}"
    headers = {
        "Authorization": f"Bearer {BEARER_TOKEN}",
        "Content-Type": "application/json",
    }

    print(f"Fetching metadata from: {url}")
    response = requests.get(url, headers=headers)
    response.raise_for_status()

    return response.json()


def reformat_for_generate_metadata(api_response: dict) -> dict:
    """Reformat API response to match generate_metadata.py input format.

    Handles BRI API response structure with wrapper fields and nested data.
    """

    # Extract data from wrapper (if present)
    data = api_response.get("data", api_response)

    # Extract table name
    table_name = data.get("TableName", "")

    # Extract table description
    table_description = data.get("TableDescription", "")

    # Extract source schema from Knowledge field if available
    source_schema = ""
    columns_data = data.get("Columns", [])
    if columns_data and columns_data[0].get("Knowledge"):
        source_schema = columns_data[0]["Knowledge"][0].get("SourceSchema", "")

    # Reformat columns
    formatted_columns = []
    for col in columns_data:
        formatted_col = {
            "ColumnName": col.get("ColumnName", ""),
            "ColumnDescription": col.get("ColumnDescription", ""),
            "ColumnDataType": col.get("ColumnDataType", "VARCHAR"),
        }
        if formatted_col["ColumnName"]:  # Only add if column name exists
            formatted_columns.append(formatted_col)

    # Build the formatted output
    formatted = {
        "TableName": table_name,
        "SourceSchema": source_schema,
        "TableDescription": table_description,
        "Columns": formatted_columns,
    }

    return formatted


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch table metadata from API and reformat for generate_metadata.py")
    parser.add_argument("table_name", help="Name of the table to fetch metadata for")
    parser.add_argument("--out", help="Write the reformatted JSON to this path")
    args = parser.parse_args()

    try:
        # Fetch from API
        api_response = fetch_table_metadata(args.table_name)
        print(f"✓ Successfully fetched metadata for table: {args.table_name}")

        # Reformat for generate_metadata.py
        formatted_data = reformat_for_generate_metadata(api_response)

        # Display the result
        print("\n" + "=" * 60)
        print("Reformatted metadata for generate_metadata.py:")
        print("=" * 60)
        print(json.dumps(formatted_data, indent=2, ensure_ascii=False))

        # Save to file if requested
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(formatted_data, f, ensure_ascii=False, indent=2)
            print(f"\n✓ Saved to: {args.out}")
            print(f"\nNow you can run:")
            print(f"  python generate_metadata.py {args.out}")

    except requests.exceptions.RequestException as e:
        print(f"✗ API request failed: {e}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"✗ Failed to parse API response as JSON: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
