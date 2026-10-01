"""
Quick verification: for each unique table in kata_test_dataset.csv, does
KataOpenSearchClient().search_datasets(table_name) return a dataset whose ID
matches the one the curator linked in the "Link KATA Dataset" column?

This is a narrow, fast check — it does NOT do any column-level matching or
LLM calls. It only answers: "can we find the right dataset for a table at
all?" — which is the prerequisite for the dataset-scoped column-matching
approach to work.

Usage:
    python test_kata_dataset_search.py
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import pandas as pd

from mage_flow.kata import KataOpenSearchClient

BASE_DIR = Path(__file__).parent
INPUT_FILE = BASE_DIR / "kata_test_dataset.csv"

TABLE_COL = "Tabel"
LINK_COL = "Link KATA Dataset"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def _extract_id_from_link(url: str) -> str:
    """https://kata.bri.co.id/metadata-directory/data-detail/{id} -> id"""
    url = str(url or "").strip()
    if not url:
        return ""
    match = re.search(r"/data-detail/([^/?#]+)", url)
    return match.group(1) if match else ""


def main() -> None:
    if not INPUT_FILE.exists():
        logger.error("Input file not found: %s", INPUT_FILE)
        sys.exit(1)

    df = pd.read_csv(INPUT_FILE)
    missing = [c for c in (TABLE_COL, LINK_COL) if c not in df.columns]
    if missing:
        raise ValueError(f"kata_test_dataset.csv missing columns: {missing}. Found: {list(df.columns)}")

    # One row per unique table (all columns of the same table share the same GT link).
    unique_tables = (
        df[[TABLE_COL, LINK_COL]]
        .drop_duplicates(subset=[TABLE_COL])
        .assign(gt_id=lambda d: d[LINK_COL].map(_extract_id_from_link))
    )
    unique_tables = unique_tables[unique_tables["gt_id"] != ""].reset_index(drop=True)
    logger.info("Checking %d unique table(s) …", len(unique_tables))

    kata_client = KataOpenSearchClient()

    results = []
    for _, row in unique_tables.iterrows():
        table_name = str(row[TABLE_COL])
        gt_id = row["gt_id"]

        try:
            # Pass table_name both as free-text query and as the exact
            # technical-alias term filter, to maximize recall across
            # whichever field actually matches in the dataset index.
            candidates = kata_client.search_datasets(table_name, table_name=table_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("search_datasets failed for %s: %s", table_name, exc)
            candidates = []

        candidate_ids = [c["id"] for c in candidates]
        hit = gt_id in candidate_ids
        rank = candidate_ids.index(gt_id) + 1 if hit else None

        results.append(
            {
                "Tabel": table_name,
                "GT Dataset ID": gt_id,
                "Found": hit,
                "Rank": rank,
                "N Candidates": len(candidates),
                "Candidate Names": "; ".join(c["data_name"] for c in candidates[:5]),
            }
        )

    results_df = pd.DataFrame(results)
    hit_count = int(results_df["Found"].sum())
    total = len(results_df)

    logger.info("\n%s", results_df.to_string(index=False))
    logger.info(
        "\nDataset search hit rate: %d/%d (%.1f%%)",
        hit_count, total, (hit_count / total * 100) if total else 0.0,
    )

    misses = results_df[~results_df["Found"]]
    if not misses.empty:
        logger.info("\nMissed tables (search_datasets did not surface the GT dataset):")
        for _, row in misses.iterrows():
            logger.info("  - %s (candidates found: %s)", row["Tabel"], row["Candidate Names"] or "(none)")


if __name__ == "__main__":
    main()
