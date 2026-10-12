"""
hierarchy_v4_patch_rows.py -- re-run kata_similar for a few rows of an
existing v4 run and rebuild the workbook WITHOUT re-judging anything else.

Every other (row, step) keeps the generation output from the raw JSON and the
judge / truthfulness / reranker results already in the v4 workbook, so the
before/after difference is only the patched rows (no judge noise elsewhere).
Patched rows get: new kata_similar output -> judged -> truthfulness -> reranked.

Usage:
    python experiments/hierarchy_v4_patch_rows.py \
        --raw evaluation/hierarchy_v4_raw_XXXX.json \
        --rows "antasena_webportal_d_validasi_rinciangagal.periodelaporan" "..."
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import hierarchy_v4_eval as v4  # noqa: E402

import argparse  # noqa: E402
import datetime as dt  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import shutil  # noqa: E402
from typing import Any, Dict, List  # noqa: E402

import pandas as pd  # noqa: E402

from mage_flow import config  # noqa: E402
from mage_flow.approved_examples import ApprovedExamples, format_examples  # noqa: E402
from mage_flow.clients import BM25Client, get_llm_client  # noqa: E402
from mage_flow.common import norm  # noqa: E402
from mage_flow.kata_similar import KataDatasetPool, retrieve_bm25_knowledge  # noqa: E402
from mage_flow.llm_generation import LLMGeneration  # noqa: E402
from mage_flow.resolvers import ResolverContext  # noqa: E402

v3 = v4.v3
logger = logging.getLogger("hierarchy_v4_patch_rows")
STEP = "kata_similar"


def _s(value: Any) -> str:
    return "" if pd.isna(value) else str(value)


def rerun_kata_similar(df: pd.DataFrame, results: List[Dict[str, Any]], row_ids: List[int], deps: v3.Deps,
                       type_filter: bool = False, examples_provider: Any = None) -> None:
    """Replace results[ri]['outcomes']['kata_similar'] in place for row_ids."""
    resolvers = v4.build_resolvers(deps.pool, type_filter=type_filter, examples_provider=examples_provider)
    cols_by_table = v4.table_columns_from_df(df)
    prep: Dict[str, Any] = {}
    by_id = {r["row_id"]: r for r in results}
    for ri in row_ids:
        table, col = norm(df.at[ri, v4.TABLE_COL]), norm(df.at[ri, v4.COLUMN_COL])
        if table not in prep:
            prep[table] = v3.prepare_table(table, [c["ColumnName"] for c in cols_by_table[table]], deps)
            prep[table]["table_columns"] = cols_by_table[table]
        tp = prep[table]
        ctx = ResolverContext(
            table_name=table, col_name=col, system_context=tp["system_context"], table_hits=tp["table_hits"],
            bm25=deps.bm25, llm=deps.llm, sampling_params=deps.sampling, bm25_params=deps.bm25_params,
            settings=deps.settings, request_id=tp["request_id"], table_term_context=tp["table_term"],
            data_type=norm(df.at[ri, v4.DATA_TYPE_COL]), table_columns=tp["table_columns"],
        )
        try:
            ctx.extra_knowledge = retrieve_bm25_knowledge(resolvers["bm25_as400"], ctx)
        except Exception:  # noqa: BLE001
            ctx.extra_knowledge = []
        result, error = v3.run_one(resolvers[STEP], ctx)
        outcome = v3.to_outcome(result, error, ctx, deps.llm, deps.bt_sampling)
        first = (result.knowledge or [{}])[0] if result is not None else {}
        outcome["confidence"] = (first.get("kata_similar") or {}).get("confidence")
        old = by_id[ri]["outcomes"][STEP]
        logger.info("%s.%s: %r -> %r", table, col, old.get("title") or "(not covered)", outcome["title"] or "(not covered)")
        by_id[ri]["outcomes"][STEP] = outcome


def restore_labels(long_df: pd.DataFrame, workbook: Path, skip: set) -> pd.DataFrame:
    """Copy judge labels + truthfulness from the previous v4 workbook for every
    (row, step) not in ``skip``."""
    prev = pd.read_excel(workbook, sheet_name="Results")
    for i, r in long_df.iterrows():
        key = (int(r["row_id"]), r["layer"])
        if key in skip:
            continue
        p = f"[{r['layer']}]"
        long_df.at[i, "eval_desc"] = _s(prev.at[key[0], f"{p} eval desc"])
        long_df.at[i, "eval_title"] = _s(prev.at[key[0], f"{p} eval title"])
        if f"{p} truthfulness" in prev.columns:
            long_df.at[i, "truthfulness"] = _s(prev.at[key[0], f"{p} truthfulness"])
            long_df.at[i, "truthfulness_reason"] = _s(prev.at[key[0], f"{p} truthfulness reasoning"])
    return long_df


def restore_rerank(workbook: Path, n_rows: int) -> pd.DataFrame:
    prev = pd.read_excel(workbook, sheet_name="Results")
    return pd.DataFrame({
        "selected": [_s(x) for x in prev["[reranker] selected step"]],
        "confidence": [None if pd.isna(x) else x for x in prev["[reranker] confidence"]],
        "reasoning": [_s(x) for x in prev["[reranker] reasoning"]],
        "n_candidates": [int(x) if not pd.isna(x) else 0 for x in prev["[reranker] n candidates"]],
    }, index=pd.RangeIndex(n_rows, name="row_id"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-run kata_similar for selected rows of a v4 run")
    parser.add_argument("--raw", required=True)
    parser.add_argument("--workbook", default=str(v4.BASE_DIR / "layer_eval_results_v4.xlsx"))
    parser.add_argument("--baseline", default=str(v4.DEFAULT_BASELINE))
    parser.add_argument("--rows", nargs="+", default=[], help="table.column identifiers")
    parser.add_argument("--type-filter", action="store_true", help="re-run kata_similar with the data-type pre-filter (#3)")
    parser.add_argument("--output", default=None, help="workbook to write (default: overwrite --workbook, with backup)")
    parser.add_argument("--baseline-label", default="v3")
    parser.add_argument("--new-label", default="v4")
    parser.add_argument("--note", default="", help="extra text for the Notes sheet")
    parser.add_argument("--approved-examples", default=None,
                        help="workbook with steward-approved metadata (sheet 'Validate') used as few-shot examples")
    parser.add_argument("--all-rows", action="store_true", help="re-run kata_similar for every row (ignores --rows)")
    args = parser.parse_args()

    raw_path, workbook = Path(args.raw), Path(args.workbook)
    output = Path(args.output) if args.output else workbook
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    df = pd.DataFrame(raw["rows"])
    results = raw["results"]

    keys = {f"{norm(t)}.{norm(c)}": i for i, (t, c) in enumerate(zip(df[v4.TABLE_COL], df[v4.COLUMN_COL]))}
    if args.all_rows:
        args.rows = list(keys)
    if not args.rows:
        raise ValueError("pass --rows or --all-rows")
    missing = [r for r in args.rows if r not in keys]
    if missing:
        raise ValueError(f"rows not found in raw run: {missing}")
    row_ids = [keys[r] for r in args.rows]

    llm = LLMGeneration(get_llm_client())
    deps = v3.Deps(BM25Client(), llm, config.default_settings(), KataDatasetPool())
    examples_provider = None
    if args.approved_examples:
        examples = ApprovedExamples.from_workbook(Path(args.approved_examples))
        test_tables = {norm(t) for t in df[v4.TABLE_COL]}
        examples_provider = lambda ctx: format_examples(  # noqa: E731
            examples.similar(ctx.col_name, ctx.table_name, k=5, exclude_tables=test_tables))
    rerun_kata_similar(df, results, row_ids, deps, type_filter=args.type_filter, examples_provider=examples_provider)

    stamp = f"{dt.datetime.now():%Y%m%d_%H%M%S}"
    new_raw = raw_path.with_name(f"hierarchy_v4_raw_patched_{stamp}.json")
    new_raw.write_text(json.dumps({"rows": raw["rows"], "results": results}, ensure_ascii=False, default=str), encoding="utf-8")
    logger.info("Patched raw saved -> %s", new_raw)

    skip = {(ri, STEP) for ri in row_ids}
    long_df = restore_labels(v3.build_long_df(df, results), workbook, skip)
    patched_idx = long_df.index[long_df.apply(lambda r: (int(r["row_id"]), r["layer"]) in skip, axis=1)]
    sub = v3.judge_truthfulness(v3.judge_similarity(long_df.loc[patched_idx].copy()), df)
    long_df.loc[patched_idx, ["eval_desc", "eval_title", "truthfulness", "truthfulness_reason"]] = \
        sub[["eval_desc", "eval_title", "truthfulness", "truthfulness_reason"]]

    rerank = restore_rerank(workbook, len(df))
    by_id = {r["row_id"]: r for r in results}
    truth: Dict[int, Dict[str, str]] = {}
    for _, r in long_df[long_df["truthfulness"].astype(str) != ""].iterrows():
        truth.setdefault(int(r["row_id"]), {})[r["layer"]] = str(r["truthfulness"])
    for ri in row_ids:
        out = v4.rerank_one(llm.client, df, by_id[ri], truth.get(ri, {}))
        rerank.loc[ri, ["selected", "confidence", "reasoning", "n_candidates"]] = \
            [out["selected"], out["confidence"], out["reasoning"], out["n_candidates"]]

    if output == workbook:
        backup = workbook.with_name(f"{workbook.stem}_before_patch_{stamp}{workbook.suffix}")
        shutil.copy2(workbook, backup)
        logger.info("Previous workbook backed up -> %s", backup)

    report = v4.build_report(df, long_df, rerank, Path(args.baseline), args.baseline_label, args.new_label)
    report["Notes"] = pd.concat([report["Notes"], pd.DataFrame([{
        "Topic": "Patched rows",
        "Description": f"kata_similar re-run ({stamp}, type_filter={args.type_filter}, "
                       f"approved_examples={bool(args.approved_examples)}) for {len(args.rows)} row(s): "
                       + ", ".join(args.rows) + ". All other rows keep the labels of the input workbook. " + args.note,
    }])], ignore_index=True)
    logger.info("\n=== WATERFALL ===\n%s", report["Waterfall"].to_string(index=False))
    if "Compare Summary" in report:
        logger.info("\n=== COMPARE vs v3 ===\n%s", report["Compare Summary"].to_string(index=False))
    logger.info("Saved -> %s", v3.write_workbook(output, report))


if __name__ == "__main__":
    main()
