import time
import requests
import pandas as pd

BASE_URL = "https://api-bribrain.ddb.dev.bri.co.id/mage/api/v1/metadata"
HEADERS = {
    "Authorization": "Bearer meB6syT6-RmkanBwSthkMNbMw-02Obb-k-sp4OYIods"
}
INPUT_FILE = "list_new_tables.txt"
OUTPUT_FILE = "all_tables.xlsx"

with open(INPUT_FILE) as f:
    table_names = [line.strip() for line in f if line.strip()]

all_results = []
for table_name in table_names:
    print(f"Fetching {table_name}...")
    try:
        response = requests.get(f"{BASE_URL}/{table_name}", headers=HEADERS)
        response.raise_for_status()
        data = response.json().get("data", {})
        for col in data.get("Columns", []):
            all_results.append({
                "Tabel": table_name,
                "Kolom": col.get("ColumnName"),
                "Tipe Data": col.get("ColumnDataType"),
                "Predicted Business Title": col.get("ColumnBusinessTitle"),
                "Predicted Column Description": col.get("ColumnDescription"),
            })
    except Exception as e:
        print(f"  ERROR: {e}")
    time.sleep(1)

df = pd.DataFrame(all_results)
df.to_excel(OUTPUT_FILE, index=False)
print(f"\nSaved {len(all_results)} rows to {OUTPUT_FILE}")
