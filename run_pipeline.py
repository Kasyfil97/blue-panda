"""
Batch inference + evaluation pipeline for data_test.xlsx.

Steps
-----
1. Inference  – fill missing 'Predicted Column Description' cells by calling
                generate_column_description() from generate_column_description.py.
                Progress is checkpointed after every row so the script is safe
                to interrupt and resume.
2. Evaluation – score predictions against ground-truth 'Column Description'
                by importing evaluate_all() from evaluate_column_description.py.

Usage
-----
  python research/run_pipeline.py                   # run both steps
  python research/run_pipeline.py --skip-inference  # evaluation only
  python research/run_pipeline.py --skip-eval       # inference only
  python research/run_pipeline.py --force           # re-infer all rows, not just missing ones
  python research/run_pipeline.py --workers 4       # use 4 parallel inference workers
"""

import argparse
import asyncio
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd
from tqdm import tqdm

BASE_DIR = Path(__file__).parent

# Module-level load_dotenv calls inside each module handle credential loading.
from mage_flow import default_settings, generate_column             # noqa: E402
from mage_flow.clients import BedrockLLMClient                      # noqa: E402
from mage_flow.llm_generation import LLMGeneration                  # noqa: E402
from evaluate_column_description import evaluate_all                 # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

INPUT_FILE      = BASE_DIR / "data_test.xlsx"
CHECKPOINT_FILE = BASE_DIR / "data_test_checkpoint.xlsx"

TABLE_COL     = "Tabel"
COLUMN_COL    = "Kolom"
PREDICTED_COL = "Predicted Column Description"   # inference target (unchanged)
REFERENCE_COL = "Column Description"             # inference-side ground truth (unchanged)
RESOLVER_COL  = "Resolver"
KNOWLEDGE_COL = "BM25 Retrieved Knowledge"
EVAL_COL      = "LLM Evaluation"

# Evaluation targets — what the LLM judge scores. Set to the business-title pair.
EVAL_REFERENCE_COL = "Business Title"            # ground truth
EVAL_PREDICTED_COL = "Predicted Business Title"  # prediction

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_missing(value) -> bool:
    return str(value).strip().lower() in ("", "none", "nan", "-")


def _format_knowledges(knowledges: list) -> str:
    if not knowledges:
        return ""
    parts = []
    for hit in knowledges:
        # generate_column returns col-knowledge shaped with PascalCase keys
        # (see mage_flow.flow._format_col_knowledge): TableName / ColumnName /
        # ColumnDescription / Score. Score may be None (e.g. exact lookup).
        tbl   = hit.get("TableName") or ""
        col   = hit.get("ColumnName") or ""
        desc  = hit.get("ColumnDescription") or ""
        score = hit.get("Score")
        score_str = f"{score:.2f}" if isinstance(score, (int, float)) else "n/a"
        parts.append(f"{tbl}.{col} (score={score_str}): {desc}")
    return "\n".join(parts)


def _load_checkpoint(df: pd.DataFrame, checkpoint_file: Path = CHECKPOINT_FILE) -> pd.DataFrame:
    """Merge checkpointed predictions back into df if a checkpoint file exists."""
    if checkpoint_file.exists():
        logger.info("Found checkpoint — resuming from %s", checkpoint_file)
        ckpt = pd.read_excel(checkpoint_file)
        for col in (PREDICTED_COL, RESOLVER_COL, KNOWLEDGE_COL):
            if col in ckpt.columns:
                df[col] = ckpt[col].values
    return df


def _build_summary(labels: list[str], total: int) -> pd.DataFrame:
    label_order = ["similar", "partial", "unsimilar", "fallback", "error"]
    series = pd.Series(labels)
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
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Step 1: Inference
# ---------------------------------------------------------------------------

_df_lock = threading.Lock()


def _infer_row(i: int, table_name: str, col_name: str, settings: dict, llm: LLMGeneration) -> tuple[int, str, str, str, str]:
    """Run inference for one row; returns (index, description, business_title, resolver, knowledge)."""
    try:
        result = generate_column(table_name, col_name, settings=settings, llm=llm)
        knowledge = _format_knowledges(result.get("knowledges", []))
        return i, result.get("description", ""), result.get("business_title", ""), result.get("resolver", ""), knowledge
    except Exception as exc:
        logger.error("Row %d (%s.%s) failed: %s", i, table_name, col_name, exc)
        return i, "", "", "error", ""


def run_inference(df: pd.DataFrame, force: bool = False, workers: int = 1, checkpoint_file: Path = CHECKPOINT_FILE) -> pd.DataFrame:
    """Call generate_column_description for each row with a missing prediction."""
    for col in (PREDICTED_COL, EVAL_PREDICTED_COL, RESOLVER_COL, KNOWLEDGE_COL):
        if col not in df.columns:
            df[col] = ""
        # Ensure object dtype so string assignment doesn't raise TypeError
        # when the column was loaded as float64 (all-NaN from a prior failed run).
        df[col] = df[col].astype(object).where(df[col].notna(), "")

    pending = [
        i for i, row in df.iterrows()
        if force or _is_missing(row.get(PREDICTED_COL))
    ]

    if not pending:
        logger.info("Inference: all %d rows already have predictions — skipping.", len(df))
        return df

    logger.info("Inference: %d row(s) to process with %d worker(s).", len(pending), workers)

    settings = default_settings()
    settings["business_title"]["enabled"] = True   # force-generate business title per row
    # Single shared LLM client — avoids re-authenticating OIDC per row.
    llm = LLMGeneration(BedrockLLMClient())

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_infer_row, i, str(df.at[i, TABLE_COL]), str(df.at[i, COLUMN_COL]), settings, llm): i
            for i in pending
        }

        with tqdm(total=len(pending), desc="Inference", unit="row") as pbar:
            for future in as_completed(futures):
                i, desc, bt, resolver, knowledge = future.result()
                with _df_lock:
                    df.at[i, PREDICTED_COL]    = desc
                    df.at[i, EVAL_PREDICTED_COL] = bt
                    df.at[i, RESOLVER_COL]     = resolver
                    df.at[i, KNOWLEDGE_COL]    = knowledge
                    df.to_excel(checkpoint_file, index=False)
                pbar.update(1)

    logger.info("Inference complete. Checkpoint: %s", checkpoint_file)
    return df


# ---------------------------------------------------------------------------
# Step 2: Evaluation
# ---------------------------------------------------------------------------

def run_evaluation(df: pd.DataFrame) -> pd.DataFrame:
    """Score business-title predictions via LLM judge (evaluate_all from evaluate_column_description)."""
    # missing = [c for c in (EVAL_REFERENCE_COL, EVAL_PREDICTED_COL) if c not in df.columns]
    # missing = [c for c in (EVAL_RERENCE_COL, EVAL_PREDICTED_COL) if c not in df.columns]
    # if missing:
    #     raise ValueError(f"Evaluation requires columns: {missing}")

    # evaluate_all reads hardcoded 'Column Description'/'Predicted Column Description'
    # columns, so map the business-title pair onto those names. It also expects a
    # zero-based sequential index for results assignment.
    eval_df = (
        df[[EVAL_REFERENCE_COL, EVAL_PREDICTED_COL]]
        .rename(columns={
            EVAL_REFERENCE_COL: "Column Description",
            EVAL_PREDICTED_COL: "Predicted Column Description",
        })
        .reset_index(drop=True)
    )
    labels = asyncio.run(evaluate_all(eval_df))
    df[EVAL_COL] = labels
    return df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Inference + evaluation pipeline")
    parser.add_argument("--input",  default=None, help="Input Excel file (default: data_test.xlsx)")
    parser.add_argument("--skip-inference", action="store_true", help="Skip inference step")
    parser.add_argument("--skip-eval",      action="store_true", help="Skip evaluation step")
    parser.add_argument(
        "--force", action="store_true",
        help="Re-run inference for all rows, not just rows with missing predictions",
    )
    parser.add_argument(
        "--workers", type=int, default=1,
        help="Number of parallel inference workers (default: 1)",
    )
    args = parser.parse_args()

    input_file = Path(args.input) if args.input else INPUT_FILE
    stem = input_file.stem
    checkpoint_file = BASE_DIR / f"{stem}_checkpoint.xlsx"

    date_tag = datetime.now().strftime("%Y%m%d")
    output_file = BASE_DIR / "evaluation" / f"{stem}_pipeline_output_{date_tag}.xlsx"

    logger.info("Reading %s …", input_file)
    df = pd.read_excel(input_file)

    # Step 1
    if not args.skip_inference:
        df = _load_checkpoint(df, checkpoint_file)
        df = run_inference(df, force=args.force, workers=args.workers, checkpoint_file=checkpoint_file)

    # Step 2
    if not args.skip_eval:
        df = run_evaluation(df)
        labels = list(df[EVAL_COL])
        summary = _build_summary(labels, len(labels))
        logger.info("Evaluation summary:\n%s", summary.to_string(index=False))

    # Write output
    logger.info("Writing results to %s …", output_file)
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Results", index=False)
        if EVAL_COL in df.columns:
            summary = _build_summary(list(df[EVAL_COL]), len(df))
            summary.to_excel(writer, sheet_name="Summary", index=False)

    logger.info("Saved %d rows → %s", len(df), output_file)


if __name__ == "__main__":
    main()
