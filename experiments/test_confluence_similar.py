"""
Standalone, ISOLATED test of ConfluenceSimilarResolver — measures its own
coverage/accuracy without running the rest of the resolver hierarchy (exact
match, KATA, BM25, PureLLM fallback), the same way test_kata_scoped_full.py
isolated the KATA-similar experiment before it was folded into layer_eval.py
as one layer among several.

Why this exists: layer_eval.py runs EVERY layer on EVERY row (by design, so
each layer's coverage/accuracy is measured independently) — that means ~10-14
LLM calls per row even if you only care about confluence_similar's own
numbers right now. This script makes only the calls confluence_similar
actually needs per row:
  - per TABLE (cached, not repeated per column): system context, table term
    context, Confluence discover() (all shared across that table's columns)
  - per ROW: abbreviation-context fetch + hypothesis LLM call (inside
    ctx.get_abbr_context(), lazy-loaded by the resolver itself) + the
    col_desc_generate synthesis call
  ~3 calls/row instead of ~10-14 — cheaper and faster to iterate on just this
  one idea.

Usage:
    python test_confluence_similar.py --sample 20   # smoke test
    python test_confluence_similar.py               # full run
"""

from __future__ import annotations

import sys
from pathlib import Path


def _find_project_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / "mage_flow").is_dir():
            return candidate
    raise RuntimeError(
        f"Could not find a 'mage_flow' package above {start} — "
        "this script must live somewhere inside (or under) the blue-panda project root."
    )


_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _find_project_root(_SCRIPT_DIR)
for _p in (str(_PROJECT_ROOT), str(_SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import argparse
import asyncio
import datetime as dt
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List

import pandas as pd
from tqdm import tqdm

import evaluate_column_description as ecd
from mage_flow import config
from mage_flow.clients import BM25Client, get_llm_client
from mage_flow.common import is_missing_desc, norm
from mage_flow.confluence import ConfluenceDiscoveryRequest, ConfluenceDiscoveryService
from mage_flow.confluence_similar import ConfluenceSimilarResolver
from mage_flow.llm_generation import LLMGeneration
from mage_flow.resolvers import ResolverContext

BASE_DIR = _PROJECT_ROOT
TABLE_COL = "Tabel"
COLUMN_COL = "Kolom"
DATA_TYPE_COL = "Tipe Data"
GT_TITLE_COL = "gt business title"
GT_DESC_COL = "gt column description"
PRED_DESC_COL = "Predicted Column Description"
PRED_TITLE_COL = "Predicted Business Title"

DEFAULT_INPUT_CANDIDATES = [
    "test set eval v2 mage.xlsx", "test set eval v2.xlsx", "test set eval 6.xlsx", "test_set_eval_v2.xlsx",
]
JUDGE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT_FULL", "gpt-4.1")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("test_confluence_similar")


def _resolve_input(arg: str | None) -> Path:
    if arg:
        return Path(arg) if Path(arg).is_absolute() else BASE_DIR / arg
    # Checked in order: project root first (back-compat with older layouts),
    # then data/ (the current convention -- test-set xlsx/csv files live
    # there now, not loose in the project root).
    search_dirs = [BASE_DIR, BASE_DIR / "data"]
    for directory in search_dirs:
        for name in DEFAULT_INPUT_CANDIDATES:
            if (directory / name).exists():
                return directory / name
    raise FileNotFoundError(
        f"No input file given and none of the default names exist in {search_dirs} "
        f"({DEFAULT_INPUT_CANDIDATES}). Pass --input \"<your file>.xlsx\" "
        f"(or \"data\\<your file>.xlsx\")."
    )


def prepare_table(table: str, columns: List[str], bm25: BM25Client, settings: Dict[str, Any]) -> Dict[str, Any]:
    """Only what confluence_similar actually needs — no BM25 table search, no
    KATA dataset search, no PureLLM prep."""
    request_id = str(uuid.uuid4())
    payload = bm25.get_system_context(table_name=table, request_id=request_id)
    payload = payload if isinstance(payload, dict) else {}
    table_term = bm25.get_table_term_context(table, request_id=request_id) or []

    confluence_candidates: List[Any] = []
    cfg = settings.get("confluence_fallback", {})
    if norm(config.CONFLUENCE_PAT):
        try:
            response = ConfluenceDiscoveryService().discover(
                ConfluenceDiscoveryRequest(
                    table_name=table, columns=columns, source_schema=None,
                    source_system=norm(payload.get("system_name")) or None,
                    limit=int(cfg.get("limit", 10)), request_id=request_id,
                )
            )
            confluence_candidates = list(response.candidates)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Confluence discovery failed for %s: %s", table, exc)
    else:
        logger.warning("CONFLUENCE_PAT not configured -- every row will be uncovered.")

    logger.info(
        "prepared %s: confluence_candidates=%d table_terms=%d",
        table, len(confluence_candidates), len(table_term),
    )
    return {
        "request_id": request_id, "system_context": payload.get("context", "") or "",
        "table_term": table_term, "confluence_candidates": confluence_candidates,
    }


def process_row(
    ri: int, row: pd.Series, prep: Dict[str, Any], resolver: ConfluenceSimilarResolver,
    bm25: BM25Client, llm: LLMGeneration, sampling: Dict[str, Any], settings: Dict[str, Any],
) -> Dict[str, Any]:
    table, col = norm(row[TABLE_COL]), norm(row[COLUMN_COL])
    tp = prep[table]
    ctx = ResolverContext(
        table_name=table, col_name=col, system_context=tp["system_context"], table_hits=[],
        bm25=bm25, llm=llm, sampling_params=sampling, bm25_params={}, settings=settings,
        request_id=tp["request_id"], table_term_context=tp["table_term"],
    )
    ctx.confluence_candidates = tp["confluence_candidates"]

    try:
        result = resolver.resolve(ctx)
    except Exception as exc:  # noqa: BLE001
        logger.warning("confluence_similar failed on %s.%s: %s", table, col, exc)
        return {"row_id": ri, "covered": False, "desc": "", "title": "", "n_candidates": len(tp["confluence_candidates"]), "error": str(exc)}

    if not result.resolved or is_missing_desc(result.description or ""):
        return {"row_id": ri, "covered": False, "desc": "", "title": "", "n_candidates": len(tp["confluence_candidates"]), "error": ""}

    knowledge = (result.knowledge or [{}])[0]
    title = ""
    try:
        from mage_flow.flow import _generate_business_title
        title = _generate_business_title(
            table_name=table, col_name=col, col_description=norm(result.description),
            system_context=ctx.system_context, knowledge=result.knowledge, llm=llm,
            sampling_params=settings.get("business_title", {}).get("sampling"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("business title generation failed for %s.%s: %s", table, col, exc)

    return {
        "row_id": ri, "covered": True, "desc": norm(result.description), "title": title,
        "n_candidates": len(tp["confluence_candidates"]),
        "page_titles": "; ".join(norm(k.get("PageTitle")) for k in (result.knowledge or []) if norm(k.get("PageTitle"))),
        "page_links": "; ".join(norm(k.get("PageUrl")) for k in (result.knowledge or []) if norm(k.get("PageUrl"))),
        "error": "",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Isolated evaluation of ConfluenceSimilarResolver")
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--sample", type=int, default=None)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    out_dir = BASE_DIR / "evaluation"
    out_dir.mkdir(parents=True, exist_ok=True)
    output = Path(args.output) if args.output else out_dir / f"confluence_similar_{dt.datetime.now():%Y%m%d}.xlsx"

    provider = os.getenv("LLM_PROVIDER", "bedrock")
    logger.info("LLM_PROVIDER=%s | judge deployment=%s", provider, JUDGE_DEPLOYMENT)

    input_path = _resolve_input(args.input)
    logger.info("Reading %s ...", input_path)
    df = pd.read_excel(input_path)
    missing = [c for c in (TABLE_COL, COLUMN_COL, GT_TITLE_COL, GT_DESC_COL) if c not in df.columns]
    if missing:
        raise ValueError(f"{input_path.name} is missing columns {missing}. Found: {list(df.columns)}")
    df = df.dropna(subset=[TABLE_COL, COLUMN_COL]).reset_index(drop=True)
    if args.sample and args.sample < len(df):
        df = df.sample(n=args.sample, random_state=42).reset_index(drop=True)
        logger.info("Sampled %d rows.", len(df))
    else:
        logger.info("Running all %d rows.", len(df))

    settings = config.default_settings()
    bm25 = BM25Client()
    llm = LLMGeneration(get_llm_client())
    logger.info("Generator client: %s", type(llm.client).__name__)
    resolver = ConfluenceSimilarResolver()
    sampling = settings.get("llm", {}).get("sampling", {"temperature": 0.1, "top_p": 0.9, "max_tokens": 1200})

    columns_by_table: Dict[str, List[str]] = {}
    for _, row in df.iterrows():
        columns_by_table.setdefault(norm(row[TABLE_COL]), []).append(norm(row[COLUMN_COL]))

    prep: Dict[str, Any] = {}
    for table, cols in tqdm(columns_by_table.items(), desc="Preparing tables (Confluence search)"):
        prep[table] = prepare_table(table, cols, bm25, settings)

    results: List[Any] = [None] * len(df)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(process_row, ri, row, prep, resolver, bm25, llm, sampling, settings): ri
            for ri, row in df.iterrows()
        }
        for future in tqdm(as_completed(futures), total=len(futures), desc="Running confluence_similar"):
            ri = futures[future]
            try:
                results[ri] = future.result()
            except Exception as exc:  # noqa: BLE001
                logger.error("row %d failed entirely: %s", ri, exc)
                results[ri] = {"row_id": ri, "covered": False, "desc": "", "title": "", "n_candidates": 0, "error": str(exc)}

    df["N Candidate Pages"] = [r["n_candidates"] for r in results]
    df["Covered"] = [r["covered"] for r in results]
    df[PRED_DESC_COL] = [r["desc"] for r in results]
    df[PRED_TITLE_COL] = [r["title"] for r in results]
    df["Page Titles Used"] = [r.get("page_titles", "") for r in results]
    df["Page Links Used"] = [r.get("page_links", "") for r in results]
    df["Error"] = [r["error"] for r in results]

    logger.info("Evaluating description/title similarity against ground truth (judge=%s) ...", JUDGE_DEPLOYMENT)

    async def _run_eval() -> tuple[list[str], list[str]]:
        return await asyncio.gather(
            ecd.evaluate_all(df, GT_DESC_COL, PRED_DESC_COL, "column descriptions", "column description", deployment=JUDGE_DEPLOYMENT),
            ecd.evaluate_all(df, GT_TITLE_COL, PRED_TITLE_COL, "business titles", "business title", deployment=JUDGE_DEPLOYMENT),
        )

    desc_labels, title_labels = asyncio.run(_run_eval())
    df["Eval Column Description"] = desc_labels
    df["Eval Business Title"] = title_labels

    desc_summary = ecd.build_summary(desc_labels, "Column Description (confluence_similar)")
    title_summary = ecd.build_summary(title_labels, "Business Title (confluence_similar)")
    summary = pd.concat([desc_summary, title_summary], ignore_index=True)
    logger.info("Evaluation summary:\n%s", summary.to_string(index=False))

    def _write(path: Path) -> None:
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Results", index=False)
            summary.to_excel(writer, sheet_name="Summary", index=False)

    try:
        _write(output)
        logger.info("Saved %d rows -> %s", len(df), output)
    except PermissionError:
        fallback = output.with_name(f"{output.stem}_{dt.datetime.now():%Y%m%d_%H%M%S}{output.suffix}")
        logger.warning("Could not write %s (open in Excel?) -- saving to %s instead.", output, fallback)
        _write(fallback)
        logger.info("Saved %d rows -> %s", len(df), fallback)


if __name__ == "__main__":
    main()