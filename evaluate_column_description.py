"""
Evaluate semantic similarity between 'Column Description' and 'Predicted Column Description'
using Azure OpenAI GPT-4.1 mini as LLM judge.

Also fetches BM25 retrieved knowledge for each row when BM25_SERVICE_URL is configured
and the input file contains 'Tabel' / 'Kolom' columns.

Metrics:
  - similar   : meanings are equivalent or near-equivalent
  - partial   : meanings partially overlap but differ in scope/detail
  - unsimilar : meanings are clearly different or unrelated

Usage:
  1. Fill Azure OpenAI credentials in research/.env (AZURE_OPENAI_ENDPOINT, etc.)
     or export them as environment variables.
  2. Optionally set BM25_SERVICE_URL in research/.env to enable BM25 retrieval.
  3. pip install openai pandas openpyxl python-dotenv tqdm requests
  4. python research/evaluate_column_description.py [input.xlsx] [--output output.xlsx]
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from openai import AsyncAzureOpenAI
from tqdm.asyncio import tqdm as atqdm

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

AZURE_ENDPOINT   = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_API_KEY    = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_API_VER    = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
AZURE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini")

BM25_URL        = os.getenv("BM25_SERVICE_URL", "").rstrip("/")
BM25_TIMEOUT    = float(os.getenv("BM25_SERVICE_TIMEOUT", "60"))
BM25_INDEX_NAME = os.getenv("BM25_INDEX_NAME", "default")

_DEFAULT_INPUT  = BASE_DIR / "data_test.xlsx"
_DEFAULT_OUTPUT = BASE_DIR / "data_test_evaluated.xlsx"

CONCURRENCY  = 10   # parallel API calls; tune down if you hit rate limits
MAX_RETRIES  = 3
RETRY_DELAY  = 5.0  # seconds between retries on rate-limit / server errors

TABLE_COL             = "Tabel"
COLUMN_COL            = "Kolom"
RESULT_COLUMN         = "LLM Evaluation"
BM25_KNOWLEDGE_COLUMN = "BM25 Retrieved Knowledge"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a semantic similarity evaluator for database column descriptions.
Your task is to compare a reference "Column Description" with a "Predicted Column Description"
and judge whether their **meanings** are equivalent, partially overlapping, or clearly different.

Classification rules
--------------------
similar   – The predicted description conveys the same core meaning as the reference.
            Minor differences in phrasing, language (Indonesian vs English), or level of detail
            are acceptable as long as the essential concept is the same.

partial   – The predicted description overlaps with the reference but is either
            noticeably broader, narrower, or adds/misses a meaningful aspect.
            They share a common topic but are not interchangeable.

unsimilar – The predicted description describes a clearly different concept
            or is so vague/wrong that it does not represent the reference meaning.

IMPORTANT: Respond with ONLY one word — exactly one of: similar, partial, unsimilar.
Do not include any explanation, punctuation, or extra text.
"""

USER_TEMPLATE = """\
Column Description          : {reference}
Predicted Column Description: {predicted}
"""


def build_messages(reference: str, predicted: str) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": USER_TEMPLATE.format(
            reference=str(reference).strip(),
            predicted=str(predicted).strip(),
        )},
    ]


# ---------------------------------------------------------------------------
# Async evaluator
# ---------------------------------------------------------------------------

async def evaluate_one(
    client: AsyncAzureOpenAI,
    semaphore: asyncio.Semaphore,
    idx: int,
    reference: str,
    predicted: str,
) -> tuple[int, str]:
    """Return (row_index, label) where label ∈ {similar, partial, unsimilar, fallback, error}."""
    if str(predicted).strip().lower() in ("", "none", "-", "nan"):
        return idx, "fallback"

    messages = build_messages(reference, predicted)

    async with semaphore:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.chat.completions.create(
                    model=AZURE_DEPLOYMENT,
                    messages=messages,
                    temperature=0,
                    max_tokens=10,
                )
                label = response.choices[0].message.content.strip().lower()
                if label not in {"similar", "partial", "unsimilar", "fallback"}:
                    logger.warning(
                        "Row %d: unexpected label %r — defaulting to 'error'", idx, label
                    )
                    label = "error"
                return idx, label

            except Exception as exc:
                err_msg = str(exc)
                if attempt < MAX_RETRIES and any(
                    code in err_msg for code in ("429", "500", "503", "RateLimitError")
                ):
                    wait = RETRY_DELAY * attempt
                    logger.warning(
                        "Row %d: attempt %d/%d failed (%s). Retrying in %.1fs …",
                        idx, attempt, MAX_RETRIES, type(exc).__name__, wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error("Row %d: failed after %d attempts — %s", idx, attempt, exc)
                    return idx, "error"

    return idx, "error"  # unreachable but satisfies type checker


async def evaluate_all(df: pd.DataFrame) -> list[str]:
    if not AZURE_ENDPOINT or not AZURE_API_KEY:
        raise EnvironmentError(
            "Azure OpenAI credentials are missing.\n"
            "Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY in research/.env "
            "or as environment variables."
        )

    client = AsyncAzureOpenAI(
        azure_endpoint=AZURE_ENDPOINT,
        api_key=AZURE_API_KEY,
        api_version=AZURE_API_VER,
    )

    semaphore = asyncio.Semaphore(CONCURRENCY)
    results = [""] * len(df)

    tasks = [
        evaluate_one(
            client, semaphore,
            idx=i,
            reference=row["Business Title"],
            predicted=row["Predicted Column Description"],
        )
        for i, row in df.iterrows()
    ]

    logger.info(
        "Starting evaluation of %d rows with concurrency=%d, deployment=%s",
        len(df), CONCURRENCY, AZURE_DEPLOYMENT,
    )
    t0 = time.perf_counter()

    for coro in atqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Evaluating"):
        idx, label = await coro
        results[idx] = label

    elapsed = time.perf_counter() - t0
    logger.info("Done in %.1f s (%.2f rows/s)", elapsed, len(df) / elapsed)
    return results


# ---------------------------------------------------------------------------
# BM25 retrieval
# ---------------------------------------------------------------------------

_bm25_session = requests.Session()


def _bm25_post_sync(path: str, payload: dict) -> dict:
    url = f"{BM25_URL}{path}"
    try:
        resp = _bm25_session.post(url, json=payload, timeout=BM25_TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
        logger.debug("BM25 %s status=%d", path, resp.status_code)
    except requests.RequestException as exc:
        logger.debug("BM25 %s error: %s", path, exc)
    return {}


def _fetch_bm25_knowledge_sync(table_name: str, col_name: str) -> str:
    """Exact lookup → table-filtered column search → global column search fallback."""
    # 1. Exact lookup
    match = _bm25_post_sync(
        "/api/v1/exact/lookup",
        {"table_name": table_name, "column_name": col_name},
    ).get("match", {})
    if isinstance(match, dict):
        desc = str(match.get("description", "")).strip()
        if desc and desc.lower() not in ("", "none", "-", "nan"):
            col = match.get("column_name") or match.get("field_name", col_name)
            return f"[exact] {table_name}.{col}: {desc}"

    # 2. Table name search
    table_hits = _bm25_post_sync("/api/v1/search", {
        "query": table_name,
        "top_k": 20,
        "threshold": 8.0,
        "table_name_boost": 2.0,
        "mode": "table_name",
        "index_name": BM25_INDEX_NAME,
    }).get("results", [])

    # 3. Table-filtered column search
    col_hits: list = []
    if table_hits:
        table_names = [h.get("table_name") for h in table_hits if h.get("table_name")]
        table_scores = {
            str(h.get("table_name")): float(h.get("final_score", h.get("bm25_score", 0.0)) or 0.0)
            for h in table_hits
        }
        if table_names:
            col_hits = _bm25_post_sync("/api/v1/search", {
                "query": col_name,
                "top_k": 5,
                "threshold": 1.0,
                "table_name_boost": 2.0,
                "mode": "column",
                "index_name": BM25_INDEX_NAME,
                "table_names": table_names,
                "table_scores": table_scores,
                "table_score_alpha": 0.9,
            }).get("results", [])

    # 4. Global column search fallback
    if not col_hits:
        col_hits = _bm25_post_sync("/api/v1/search", {
            "query": col_name,
            "top_k": 5,
            "threshold": 1.0,
            "table_name_boost": 2.0,
            "mode": "column",
            "index_name": BM25_INDEX_NAME,
        }).get("results", [])

    if not col_hits:
        return ""

    parts = []
    for hit in col_hits:
        tbl   = hit.get("table_name", "")
        col   = hit.get("column_name", "")
        desc  = hit.get("column_description", "")
        score = hit.get("final_score", hit.get("bm25_score", 0.0))
        parts.append(f"{tbl}.{col} (score={score:.2f}): {desc}")
    return "\n".join(parts)


async def _fetch_bm25_one(
    semaphore: asyncio.Semaphore, idx: int, table_name: str, col_name: str
) -> tuple[int, str]:
    async with semaphore:
        knowledge = await asyncio.to_thread(_fetch_bm25_knowledge_sync, table_name, col_name)
    return idx, knowledge


async def fetch_all_bm25_knowledge(df: pd.DataFrame) -> list[str]:
    semaphore = asyncio.Semaphore(CONCURRENCY)
    results = [""] * len(df)

    tasks = [
        _fetch_bm25_one(semaphore, i, str(row.get(TABLE_COL, "")), str(row.get(COLUMN_COL, "")))
        for i, (_, row) in enumerate(df.iterrows())
    ]

    logger.info("Fetching BM25 knowledge for %d rows …", len(tasks))
    t0 = time.perf_counter()

    for coro in atqdm(asyncio.as_completed(tasks), total=len(tasks), desc="BM25 Retrieval"):
        idx, knowledge = await coro
        results[idx] = knowledge

    logger.info("BM25 retrieval done in %.1f s", time.perf_counter() - t0)
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _parse_args() -> tuple[Path, Path]:
    parser = argparse.ArgumentParser(
        description="Evaluate semantic similarity between Column Description and Predicted Column Description."
    )
    parser.add_argument(
        "input",
        nargs="?",
        type=Path,
        default=_DEFAULT_INPUT,
        help=f"Path to input Excel file (default: {_DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="Path to output Excel file (default: <input_stem>_evaluated.xlsx)",
    )
    args = parser.parse_args()
    input_path: Path = args.input
    if args.output is not None:
        output_path: Path = args.output
    else:
        output_path = input_path.parent / f"{input_path.stem}_evaluated{input_path.suffix}"
    return input_path, output_path


def main() -> None:
    input_file, output_file = _parse_args()

    logger.info("Reading %s …", input_file)
    df = pd.read_excel(input_file)

    if "Column Description" not in df.columns or "Predicted Column Description" not in df.columns:
        raise ValueError(
            "Expected columns 'Column Description' and 'Predicted Column Description' "
            f"in the Excel file. Found: {list(df.columns)}"
        )

    has_bm25_cols = TABLE_COL in df.columns and COLUMN_COL in df.columns
    run_bm25 = bool(BM25_URL) and has_bm25_cols

    if not BM25_URL:
        logger.info("BM25_SERVICE_URL not set — skipping BM25 retrieval.")
    elif not has_bm25_cols:
        logger.info(
            "Columns '%s'/'%s' not found in input — skipping BM25 retrieval.",
            TABLE_COL, COLUMN_COL,
        )

    async def _run_pipeline() -> tuple[list[str], list[str]]:
        if run_bm25:
            eval_results, bm25_results = await asyncio.gather(
                evaluate_all(df),
                fetch_all_bm25_knowledge(df),
            )
        else:
            eval_results = await evaluate_all(df)
            bm25_results = []
        return eval_results, bm25_results

    labels, bm25_knowledge = asyncio.run(_run_pipeline())

    df[RESULT_COLUMN] = labels
    if run_bm25:
        df[BM25_KNOWLEDGE_COLUMN] = bm25_knowledge

    # Build summary
    label_order = ["similar", "partial", "unsimilar", "fallback", "error"]
    series = pd.Series(labels)
    total = len(series)
    fallback_count = int((series == "fallback").sum())
    evaluated_total = total - fallback_count

    counts = series.value_counts().reindex(label_order, fill_value=0)
    rows = []
    for label in label_order:
        count = int(counts[label])
        denom = evaluated_total if label in ("similar", "partial", "unsimilar") else total
        denom = denom if denom > 0 else 1
        rows.append({"Label": label, "Count": count, "Percentage": f"{count / denom * 100:.2f}%"})
    rows.append({"Label": "Evaluated Total", "Count": evaluated_total, "Percentage": "100.00%"})
    rows.append({"Label": "Grand Total",     "Count": total,           "Percentage": "100.00%"})
    summary = pd.DataFrame(rows)

    logger.info("Evaluation summary:\n%s", summary.to_string(index=False))

    logger.info("Writing results to %s …", output_file)
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Results", index=False)
        summary.to_excel(writer, sheet_name="Summary", index=False)
    logger.info("Saved %d rows → %s (sheets: Results, Summary)", len(df), output_file)


if __name__ == "__main__":
    main()
