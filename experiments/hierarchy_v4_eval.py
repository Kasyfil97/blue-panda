"""
hierarchy_v4_eval.py -- v3's flat 7-step cascade plus the two highest-priority
fixes from the v3 Error Analysis sheet:

  #2  kata_similar with ENRICHED context (KataSimilarResolver(enriched_context=True)):
      system context, table term knowledge, the column's abbreviation
      expansion and its sibling columns are shown to the selector, together
      with explicit token-coverage / specificity, generic-name and data-type
      compatibility rules. Generic column names get a table-anchored rescue
      query. Everything else in the cascade is unchanged from v3.

  #1  Cross-layer RERANKER instead of first-hit-wins: every step still runs
      ISOLATED on every row (as in v3); the reranker then sees all distinct
      candidate descriptions for the row (+ their evidence, kata_similar's
      selection confidence and the truthfulness verdict as SIGNALS, never as
      a gate) and picks one with the generator LLM.

Because steps run isolated and every step's output is judged, the reranker's
pick inherits the judge label of the step it picked -- no extra judging, and
the comparison against the first-hit-wins cascade is exact.

Output workbook has the same sheets/columns as layer_eval_results_v3.xlsx
(Ranking, Waterfall, Resolver Breakdown, Results, Step Detail, Notes), plus:
  - Results: "final eval desc/title" (first-hit cascade, as values) and
    "[reranker] ..." columns,
  - Waterfall: extra "reranker total" and "oracle ceiling" rows,
  - "Compare vs v3" + "Compare Summary" sheets (when the v3 workbook exists).

Usage:
    python experiments/hierarchy_v4_eval.py --sample 10            # smoke test
    python experiments/hierarchy_v4_eval.py                        # full run
    python experiments/hierarchy_v4_eval.py --resume-raw evaluation/hierarchy_v4_raw_XXXX.json
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import hierarchy_v3_eval as v3  # noqa: E402 - also puts the project root on sys.path

import argparse  # noqa: E402
import datetime as dt  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: E402
from typing import Any, Dict, List, Optional  # noqa: E402

import pandas as pd  # noqa: E402
from tqdm import tqdm  # noqa: E402

import layer_metrics as lm  # noqa: E402
from mage_flow import config  # noqa: E402
from mage_flow.clients import BM25Client, get_llm_client  # noqa: E402
from mage_flow.common import norm, parse_llm_output  # noqa: E402
from mage_flow.kata_similar import (  # noqa: E402
    MAX_SYSTEM_CONTEXT_CHARS,
    KataDatasetPool,
    KataSimilarResolver,
    format_sibling_columns,
    format_term_entries,
    retrieve_bm25_knowledge,
)
from mage_flow.llm_generation import LLMGeneration  # noqa: E402
from mage_flow.resolvers import ResolverContext, _is_as400_table  # noqa: E402

logger = logging.getLogger("hierarchy_v4_eval")

BASE_DIR = v3.BASE_DIR
STEP_ORDER = v3.STEP_ORDER
TABLE_COL, COLUMN_COL, DATA_TYPE_COL = v3.TABLE_COL, v3.COLUMN_COL, v3.DATA_TYPE_COL
DEFAULT_BASELINE = BASE_DIR / "layer_eval_results_v3.xlsx"

v3.STEP_LABELS["kata_similar"] = "2. KATA Similar (ENRICHED context; BM25 context on AS400 tables)"

RERANK_MAX_TOKENS = 600
MAX_EVIDENCE_CHARS = 400
LABEL_SCORE = {"similar": 2, "partial": 1, "unsimilar": 0}


# ---------------------------------------------------------------------------
# Generation (same as v3 except kata_similar + extra per-row context)
# ---------------------------------------------------------------------------

def build_resolvers(pool: KataDatasetPool, type_filter: bool = False, examples_provider: Any = None) -> Dict[str, Any]:
    resolvers = v3.build_resolvers(pool)
    resolvers["kata_similar"] = KataSimilarResolver(
        pool=pool, use_bm25_context=True, enriched_context=True, type_filter=type_filter,
        examples_provider=examples_provider,
    )
    return resolvers


def table_columns_from_df(df: pd.DataFrame) -> Dict[str, List[Dict[str, str]]]:
    """Sibling-column context. NOTE: only the test set's columns of each table
    are known here (~10 per table); production would pass the full table."""
    out: Dict[str, List[Dict[str, str]]] = {}
    for _, row in df.iterrows():
        dtype = norm(row.get(DATA_TYPE_COL)) if DATA_TYPE_COL in df.columns else ""
        out.setdefault(norm(row[TABLE_COL]), []).append({"ColumnName": norm(row[COLUMN_COL]), "ColumnDataType": dtype})
    return out


def process_row(ri: int, row: pd.Series, prep: Dict[str, Any], resolvers: Dict[str, Any], deps: v3.Deps) -> Dict[str, Any]:
    table, col = norm(row[TABLE_COL]), norm(row[COLUMN_COL])
    tp = prep[table]
    ctx = ResolverContext(
        table_name=table, col_name=col, system_context=tp["system_context"], table_hits=tp["table_hits"],
        bm25=deps.bm25, llm=deps.llm, sampling_params=deps.sampling, bm25_params=deps.bm25_params,
        settings=deps.settings, request_id=tp["request_id"], table_term_context=tp["table_term"],
        data_type=norm(row.get(DATA_TYPE_COL)), table_columns=tp["table_columns"],
    )
    try:
        evidence = retrieve_bm25_knowledge(resolvers["bm25_as400"], ctx)
    except Exception as exc:  # noqa: BLE001
        logger.warning("BM25 AS400 evidence retrieval failed for %s.%s: %s", table, col, exc)
        evidence = []
    ctx.extra_knowledge = evidence

    outcomes: Dict[str, Dict[str, Any]] = {}
    for step in STEP_ORDER:
        result, error = v3.run_one(resolvers[step], ctx)
        outcome = v3.to_outcome(result, error, ctx, deps.llm, deps.bt_sampling)
        first = (result.knowledge or [{}])[0] if result is not None else {}
        outcome["confidence"] = (first.get("kata_similar") or {}).get("confidence")
        outcomes[step] = outcome

    try:
        abbr = deps.bm25.get_abbreviation_context(col, request_id=tp["request_id"])
    except Exception:  # noqa: BLE001
        abbr = []
    context = {
        "data_type": ctx.data_type or "(unknown)",
        "system_context": norm(tp["system_context"])[:MAX_SYSTEM_CONTEXT_CHARS] or "(none)",
        "table_terms": format_term_entries(tp["table_term"]) or "(none)",
        "abbr_terms": format_term_entries(abbr) or "(none)",
        "sibling_columns": format_sibling_columns(tp["table_columns"], col) or "(none)",
    }
    return {"row_id": ri, "is_as400": _is_as400_table(table), "n_evidence": len(evidence),
            "outcomes": outcomes, "context": context}


def run_all_rows(df: pd.DataFrame, deps: v3.Deps, workers: int) -> List[Dict[str, Any]]:
    resolvers = build_resolvers(deps.pool)
    cols_by_table = table_columns_from_df(df)
    prep: Dict[str, Any] = {}
    for table, cols in tqdm(cols_by_table.items(), desc="Preparing tables"):
        prep[table] = v3.prepare_table(table, [c["ColumnName"] for c in cols], deps)
        prep[table]["table_columns"] = cols

    results: List[Optional[Dict[str, Any]]] = [None] * len(df)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(process_row, ri, row, prep, resolvers, deps): ri for ri, row in df.iterrows()}
        for future in tqdm(as_completed(futures), total=len(futures), desc="Running hierarchy v4"):
            ri = futures[future]
            try:
                results[ri] = future.result()
            except Exception as exc:  # noqa: BLE001
                logger.error("row %d failed entirely: %s", ri, exc)
                empty = {"covered": False, "tag": "", "desc": "", "title": "", "reason": "", "error": str(exc),
                         "knowledge": "", "confidence": None}
                results[ri] = {"row_id": ri, "is_as400": _is_as400_table(norm(df.at[ri, TABLE_COL])), "n_evidence": 0,
                               "outcomes": {s: dict(empty) for s in STEP_ORDER}, "context": {}}
    return [r for r in results if r is not None]


# ---------------------------------------------------------------------------
# Reranker (#1)
# ---------------------------------------------------------------------------

RERANK_SYSTEM_PROMPT = """\
You are a banking data steward choosing the single best description for a
database column from several candidate descriptions produced by different
metadata sources. Only ONE becomes official metadata.

INTERNAL REASONING REQUIREMENT:
- Reason internally; output only the final JSON.

COLUMN:
- Table name: {table_name}
- Source system context: {system_context}
- Table term knowledge (codes in the table name): {table_terms}
- Column name: {col_name}
- Column data type: {data_type}
- Abbreviation expansion of column-name tokens (may contain irrelevant
  partial matches): {abbr_terms}
- Other columns in the same table:
{sibling_columns}

CANDIDATES (id = source):
{candidates_block}

SOURCE NOTES (reliability priors, not rules):
- kata_similar: official KATA data element chosen by an LLM from the table's
  curated KATA dataset. Usually most reliable, but it can pick an element
  that is too generic or from the wrong domain. "selection_confidence" is
  that LLM's own confidence; "audit" is an independent check of whether the
  selection is supported by the evidence (false = suspicious, NOT proof of
  being wrong).
- kata_alias: KATA element whose alias equals the column name; curated, but
  an alias can be shared across unrelated domains.
- bm25_informatica / bm25_confluence / bm25_as400: description synthesized
  from documentation of similarly-named columns in other/same tables.
- llm: pure LLM guess without retrieved evidence (least reliable).

DECISION STEPS:
1. Infer the table's domain (name, system context, table terms, siblings).
2. Interpret the column: explain every token of its name within that domain;
   for generic names (id, name, code, status, amount, branch, ...) the
   meaning comes from the table's own entity.
3. Reject candidates whose meaning conflicts with the domain, the tokens or
   the data type (e.g. free text for an int column, a money value where the
   table counts transactions, a different entity than the table's).
4. Among the remaining, prefer the one that is SPECIFIC to this column in
   this table (covers its qualifiers) over a generic one. Do not prefer a
   candidate just because it is longer.
5. If two candidates say essentially the same thing, prefer the curated
   (kata_*) source.

OUTPUT FORMAT (STRICT JSON ONLY):
{{"selected": "<candidate id>", "confidence": <0..1>, "reasoning": "<one short sentence, in Indonesian>"}}
"""


def _rerank_candidates(res: Dict[str, Any], truth: Dict[str, str]) -> List[Dict[str, Any]]:
    """Distinct covered outputs in cascade order (identical descriptions are
    collapsed onto the earliest step, which is what a cascade would keep)."""
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for step in STEP_ORDER:
        o = res["outcomes"][step]
        if not o.get("covered"):
            continue
        key = norm(o["desc"]).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": step, "desc": o["desc"], "title": o["title"], "evidence": o.get("knowledge", ""),
                    "confidence": o.get("confidence"), "audit": truth.get(step, "")})
    return out


def _format_candidates(cands: List[Dict[str, Any]]) -> str:
    blocks = []
    for c in cands:
        lines = [f"[{c['id']}]", f"  business_title: {c['title'] or '-'}", f"  description: {c['desc']}"]
        if c["evidence"]:
            lines.append(f"  evidence: {norm(c['evidence'])[:MAX_EVIDENCE_CHARS]}")
        if c["confidence"] is not None:
            lines.append(f"  selection_confidence: {c['confidence']}")
        if c["audit"] in ("true", "false"):
            lines.append(f"  audit_truthful: {c['audit']}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def rerank_one(llm_client: Any, df: pd.DataFrame, res: Dict[str, Any], truth: Dict[str, str]) -> Dict[str, Any]:
    ri = res["row_id"]
    cands = _rerank_candidates(res, truth)
    if not cands:
        return {"row_id": ri, "selected": "", "confidence": None, "reasoning": "no candidates", "n_candidates": 0}
    if len(cands) == 1:
        return {"row_id": ri, "selected": cands[0]["id"], "confidence": None,
                "reasoning": "single distinct candidate", "n_candidates": 1}
    ctx = res.get("context") or {}
    prompt = RERANK_SYSTEM_PROMPT.format(
        table_name=norm(df.at[ri, TABLE_COL]), col_name=norm(df.at[ri, COLUMN_COL]),
        data_type=ctx.get("data_type", "(unknown)"), system_context=ctx.get("system_context", "(none)"),
        table_terms=ctx.get("table_terms", "(none)"), abbr_terms=ctx.get("abbr_terms", "(none)"),
        sibling_columns=ctx.get("sibling_columns", "(none)"), candidates_block=_format_candidates(cands),
    )
    messages = [{"role": "system", "content": prompt}, {"role": "user", "content": "Choose now."}]
    valid = {c["id"] for c in cands}
    last_error = ""
    for _ in range(2):
        try:
            raw = llm_client.create_response(messages=messages, sampling_params={"temperature": 0.0, "max_tokens": RERANK_MAX_TOKENS})
            parsed = parse_llm_output(raw)
            selected = norm(parsed.get("selected")).strip("[]")
            if selected in valid:
                try:
                    confidence = float(parsed.get("confidence"))
                except (TypeError, ValueError):
                    confidence = None
                return {"row_id": ri, "selected": selected, "confidence": confidence,
                        "reasoning": norm(parsed.get("reasoning")), "n_candidates": len(cands)}
            last_error = f"invalid selection {selected!r}"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
    # Fallback = what the first-hit cascade would have returned.
    return {"row_id": ri, "selected": cands[0]["id"], "confidence": None,
            "reasoning": f"rerank failed ({last_error}); fell back to cascade winner", "n_candidates": len(cands)}


def run_reranker(df: pd.DataFrame, results: List[Dict[str, Any]], long_df: pd.DataFrame,
                 llm_client: Any, workers: int) -> pd.DataFrame:
    truth_by_row: Dict[int, Dict[str, str]] = {}
    for _, r in long_df[long_df["truthfulness"].astype(str) != ""].iterrows():
        truth_by_row.setdefault(int(r["row_id"]), {})[r["layer"]] = str(r["truthfulness"])
    out: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(rerank_one, llm_client, df, res, truth_by_row.get(res["row_id"], {})) for res in results]
        for future in tqdm(as_completed(futures), total=len(futures), desc="Reranking"):
            out.append(future.result())
    return pd.DataFrame(out).set_index("row_id").sort_index()


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _label(long_df: pd.DataFrame, ri: int, step: str, field: str) -> str:
    if not step:
        return ""
    hit = long_df[(long_df["row_id"] == ri) & (long_df["layer"] == step)]
    return str(hit[field].iloc[0]) if len(hit) else ""


def _step_value(long_df: pd.DataFrame, ri: int, step: str, field: str) -> str:
    return _label(long_df, ri, step, field)


def results_sheet(df: pd.DataFrame, long_df: pd.DataFrame, rerank: pd.DataFrame) -> pd.DataFrame:
    base = v3.wide_results(df, long_df)
    base["final eval desc"] = [_label(long_df, i, w, "eval_desc") for i, w in zip(base.index, base["Cascade winner"])]
    base["final eval title"] = [_label(long_df, i, w, "eval_title") for i, w in zip(base.index, base["Cascade winner"])]
    sel = [rerank["selected"].get(i, "") for i in base.index]
    base["[reranker] selected step"] = sel
    base["[reranker] n candidates"] = [rerank["n_candidates"].get(i, 0) for i in base.index]
    base["[reranker] confidence"] = [rerank["confidence"].get(i) for i in base.index]
    base["[reranker] reasoning"] = [rerank["reasoning"].get(i, "") for i in base.index]
    base["[reranker] description"] = [_step_value(long_df, i, s, "pred_desc") for i, s in zip(base.index, sel)]
    base["[reranker] business title"] = [_step_value(long_df, i, s, "pred_title") for i, s in zip(base.index, sel)]
    base["[reranker] eval desc"] = [_label(long_df, i, s, "eval_desc") for i, s in zip(base.index, sel)]
    base["[reranker] eval title"] = [_label(long_df, i, s, "eval_title") for i, s in zip(base.index, sel)]
    base["[reranker] changed vs cascade"] = [s != w for s, w in zip(sel, base["Cascade winner"])]
    return base


def _stats_row(name: str, labels: List[str], n_rows: int) -> Dict[str, Any]:
    s = pd.Series(labels)
    counts = {lab: int((s == lab).sum()) for lab in lm.EVAL_LABELS}
    evaluated = sum(counts.values())
    return {"Step": name, "Layer": name, "Newly covered": int((s != "").sum()),
            "Cumulative covered": int((s != "").sum()), "Cumulative coverage %": lm._frac(int((s != "").sum()), n_rows),
            "Desc Evaluated": evaluated,
            "Desc Similar %": lm._frac(counts["similar"], evaluated),
            "Desc Partial %": lm._frac(counts["partial"], evaluated),
            "Desc Unsimilar %": lm._frac(counts["unsimilar"], evaluated),
            "Cumulative similar (desc)": counts["similar"], "Cumulative yield %": lm._frac(counts["similar"], n_rows)}


def oracle_labels(long_df: pd.DataFrame, field: str) -> List[str]:
    """Best label any covered step achieved per row (upper bound for a reranker)."""
    out = []
    for _, grp in long_df[long_df["covered"]].groupby("row_id"):
        labels = [str(x) for x in grp[field] if str(x) in LABEL_SCORE]
        out.append(max(labels, key=LABEL_SCORE.get) if labels else "")
    return out


def waterfall_sheet(long_df: pd.DataFrame, results_df: pd.DataFrame, n_rows: int) -> pd.DataFrame:
    wf = lm.waterfall(long_df, n_rows, order=STEP_ORDER)
    rr = _stats_row("reranker total", list(results_df["[reranker] eval desc"]), n_rows)
    rr["Layer"] = "Reranker across all steps (#1) -- replaces first-hit-wins"
    rr["Title Similar %"] = lm._frac(int((results_df["[reranker] eval title"] == "similar").sum()),
                                     int(results_df["[reranker] eval title"].isin(lm.EVAL_LABELS).sum()))
    oracle = _stats_row("oracle ceiling", oracle_labels(long_df, "eval_desc"), n_rows)
    oracle["Layer"] = "Ideal step choice per row (upper bound, not achievable in production)"
    oracle["Title Similar %"] = None
    return pd.concat([wf, pd.DataFrame([rr, oracle])], ignore_index=True)


def _final_from_v3(row: pd.Series, field: str) -> str:
    winner = norm(row.get("Cascade winner"))
    return norm(row.get(f"[{winner}] eval {field}")) if winner else ""


def _delta(old: str, new: str) -> str:
    if old not in LABEL_SCORE or new not in LABEL_SCORE:
        return "n/a"
    d = LABEL_SCORE[new] - LABEL_SCORE[old]
    return "improved" if d > 0 else "regressed" if d < 0 else "same"


def compare_sheets(results_df: pd.DataFrame, baseline: Path, label: str = "v3", new: str = "v4") -> Dict[str, pd.DataFrame]:
    if not baseline.exists():
        logger.warning("Baseline %s not found -- skipping comparison sheets.", baseline)
        return {}
    old = pd.read_excel(baseline, sheet_name="Results")
    old["_key"] = old[TABLE_COL].map(norm) + "|" + old[COLUMN_COL].map(norm)
    old = old.drop_duplicates("_key").set_index("_key")
    rows = []
    for _, r in results_df.iterrows():
        key = f"{norm(r[TABLE_COL])}|{norm(r[COLUMN_COL])}"
        o = old.loc[key] if key in old.index else None
        v3_desc = _final_from_v3(o, "desc") if o is not None else ""
        v3_title = _final_from_v3(o, "title") if o is not None else ""
        rows.append({
            "Tabel": r[TABLE_COL], "Kolom": r[COLUMN_COL],
            "gt business title": r[v3.GT_TITLE_COL], "gt column description": r[v3.GT_DESC_COL],
            f"{label} cascade winner": norm(o.get("Cascade winner")) if o is not None else "",
            f"{label} kata_similar title": norm(o.get("[kata_similar] business title")) if o is not None else "",
            f"{label} kata_similar eval desc": norm(o.get("[kata_similar] eval desc")) if o is not None else "",
            f"{label} final eval desc": v3_desc, f"{label} final eval title": v3_title,
            f"{new} kata_similar title": r["[kata_similar] business title"],
            f"{new} kata_similar eval desc": r["[kata_similar] eval desc"],
            f"{new} cascade winner": r["Cascade winner"],
            f"{new} cascade eval desc (#2)": r["final eval desc"], f"{new} cascade eval title (#2)": r["final eval title"],
            f"{new} reranker step": r["[reranker] selected step"],
            f"{new} reranker title": r["[reranker] business title"],
            f"{new} reranker eval desc (#1+#2)": r["[reranker] eval desc"],
            f"{new} reranker eval title (#1+#2)": r["[reranker] eval title"],
            f"desc: {label} -> {new} cascade": _delta(v3_desc, r["final eval desc"]),
            f"desc: {label} -> {new} reranker": _delta(v3_desc, r["[reranker] eval desc"]),
            f"title: {label} -> {new} reranker": _delta(v3_title, r["[reranker] eval title"]),
        })
    detail = pd.DataFrame(rows)

    def count(col: str, lab: str = "similar") -> int:
        return int((detail[col] == lab).sum())

    summary_rows = []
    for name, d_col, t_col in [
        (f"{label} cascade (baseline)", f"{label} final eval desc", f"{label} final eval title"),
        (f"{new} cascade -- #2 enriched kata_similar, first-hit-wins", f"{new} cascade eval desc (#2)", f"{new} cascade eval title (#2)"),
        (f"{new} reranker -- #1 + #2", f"{new} reranker eval desc (#1+#2)", f"{new} reranker eval title (#1+#2)"),
    ]:
        summary_rows.append({
            "Configuration": name, "Rows": len(detail),
            "Desc similar": count(d_col), "Desc similar %": lm._frac(count(d_col), len(detail)),
            "Desc partial": count(d_col, "partial"), "Desc unsimilar": count(d_col, "unsimilar"),
            "Title similar": count(t_col), "Title similar %": lm._frac(count(t_col), len(detail)),
        })
    summary_rows.append({
        "Configuration": f"kata_similar isolated: {label} -> {new} (similar desc)", "Rows": len(detail),
        "Desc similar": count(f"{new} kata_similar eval desc"),
        "Desc similar %": lm._frac(count(f"{new} kata_similar eval desc"), len(detail)),
        "Desc partial": count(f"{new} kata_similar eval desc", "partial"),
        "Desc unsimilar": count(f"{new} kata_similar eval desc", "unsimilar"),
        "Title similar": None, "Title similar %": None,
    })
    summary_rows.append({
        "Configuration": f"  ({label} kata_similar isolated, same rows)", "Rows": len(detail),
        "Desc similar": count(f"{label} kata_similar eval desc"),
        "Desc similar %": lm._frac(count(f"{label} kata_similar eval desc"), len(detail)),
        "Desc partial": count(f"{label} kata_similar eval desc", "partial"),
        "Desc unsimilar": count(f"{label} kata_similar eval desc", "unsimilar"),
        "Title similar": None, "Title similar %": None,
    })
    for col in (f"desc: {label} -> {new} cascade", f"desc: {label} -> {new} reranker", f"title: {label} -> {new} reranker"):
        vc = detail[col].value_counts()
        summary_rows.append({"Configuration": f"{col}: improved / regressed / same",
                             "Rows": len(detail),
                             "Desc similar": f"{int(vc.get('improved', 0))} / {int(vc.get('regressed', 0))} / {int(vc.get('same', 0))}"})
    return {"Compare Summary": pd.DataFrame(summary_rows), f"Compare vs {label}": detail}


NOTES = [
    ("What changed vs v3", "Two fixes from the v3 Error Analysis sheet. #2: kata_similar runs with enriched context (system context, table term knowledge, abbreviation expansion, sibling columns + data types) and explicit token-coverage/specificity, generic-name and data-type-compatibility rules; generic column names get a table-anchored rescue query. #1: a cross-layer reranker picks among all steps' outputs instead of first-hit-wins. All other steps are identical to v3."),
    ("How to read the two results", "Waterfall 'cascade total' / Results 'final eval *' = #2 alone (still first-hit-wins). Waterfall 'reranker total' / Results '[reranker] *' = #1 + #2. 'oracle ceiling' = best any step achieved per row (upper bound)."),
    ("Reranker", "Generator LLM (LLM_PROVIDER). Sees every DISTINCT candidate description (identical texts collapsed onto the earliest step), its evidence, kata_similar's selection confidence and the GPT-4.1 truthfulness verdict as SIGNALS (not a gate). Rows with one distinct candidate skip the LLM call. On LLM failure it falls back to the cascade winner."),
    ("Judging", "Every step's output is judged once (same judge and prompt as v3); the reranker's pick inherits the label of the step it picked, so cascade vs reranker is an exact comparison on the same judge labels."),
    ("Sibling-column caveat", "Sibling columns come from the TEST SET (~10 columns per table), not the full table schema. Production would see all columns -- likely more context, not less."),
    ("Term dictionary", "The BM25 abbreviation dictionary (localhost:8003) was extended with AS400 codes (car, dsc, spr, msp, fcs, dcd, drn, card) before this run. This affects every step that reads abbreviation context (bm25_*, llm, enriched kata_similar), so v3 -> v4 differences on AS400 rows are partly due to that (#4)."),
    ("Comparison sheets", "'Compare vs v3' matches rows by Tabel+Kolom against layer_eval_results_v3.xlsx; v3's final eval is recomputed from its Cascade winner column (same as its formula columns)."),
    ("Judge / generator", f"Generator: LLM_PROVIDER endpoint. Judge: {v3.JUDGE_DEPLOYMENT}."),
]


def build_report(df: pd.DataFrame, long_df: pd.DataFrame, rerank: pd.DataFrame, baseline: Path,
                 baseline_label: str = "v3", new_label: str = "v4") -> Dict[str, pd.DataFrame]:
    n_rows = len(df)
    n_as400 = int(sum(_is_as400_table(norm(t)) for t in df[TABLE_COL]))
    summary = lm.layer_summary(long_df, n_rows, STEP_ORDER)
    for step, label in v3.STEP_LABELS.items():
        summary.loc[summary["layer_key"] == step, "Layer"] = label
    ranking = lm.ranking_table(summary, layer_order=STEP_ORDER).drop(columns=["layer_key"])
    results_df = results_sheet(df, long_df, rerank)
    report = {
        "Ranking": ranking,
        "Waterfall": waterfall_sheet(long_df, results_df, n_rows),
        "Resolver Breakdown": lm.resolver_breakdown(long_df, n_rows, n_as400, STEP_ORDER),
        "Results": results_df,
        "Step Detail": v3.step_detail(df, long_df),
    }
    report.update(compare_sheets(results_df, baseline, baseline_label, new_label))
    report["Notes"] = pd.DataFrame(NOTES, columns=["Topic", "Description"])
    return report


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Hierarchy v4: enriched kata_similar (#2) + cross-layer reranker (#1)")
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    parser.add_argument("--sample", type=int, default=None)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--resume-raw", default=None)
    args = parser.parse_args()

    stamp = f"{dt.datetime.now():%Y%m%d_%H%M%S}"
    out_dir = BASE_DIR / "evaluation"
    out_dir.mkdir(parents=True, exist_ok=True)
    output = Path(args.output) if args.output else BASE_DIR / "layer_eval_results_v4.xlsx"
    logger.info("LLM_PROVIDER=%s | judge deployment=%s", os.getenv("LLM_PROVIDER", "bedrock"), v3.JUDGE_DEPLOYMENT)

    llm = LLMGeneration(get_llm_client())
    if args.resume_raw:
        raw = json.loads(Path(args.resume_raw).read_text(encoding="utf-8"))
        df = pd.DataFrame(raw["rows"])
        results = raw["results"]
        logger.info("Resumed %d rows from %s (generation skipped).", len(df), args.resume_raw)
    else:
        input_path = v3._resolve_input(args.input)
        logger.info("Reading %s ...", input_path)
        df = pd.read_excel(input_path)
        missing = [c for c in (TABLE_COL, COLUMN_COL, v3.GT_TITLE_COL, v3.GT_DESC_COL) if c not in df.columns]
        if missing:
            raise ValueError(f"{input_path.name} is missing columns {missing}. Found: {list(df.columns)}")
        df = df.dropna(subset=[TABLE_COL, COLUMN_COL]).reset_index(drop=True)
        if args.sample and args.sample < len(df):
            df = df.sample(n=args.sample, random_state=42).reset_index(drop=True)
            logger.info("Sampled %d rows.", len(df))
        settings = config.default_settings()
        deps = v3.Deps(BM25Client(), llm, settings, KataDatasetPool())
        results = run_all_rows(df, deps, args.workers)
        raw_path = out_dir / f"hierarchy_v4_raw_{stamp}.json"
        raw_path.write_text(
            json.dumps({"rows": json.loads(df.to_json(orient="records", force_ascii=False)), "results": results},
                       ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        logger.info("Raw run saved -> %s  (re-judge/re-rank later with --resume-raw)", raw_path)

    long_df = v3.build_long_df(df, results)
    logger.info("Judging similarity (%s) ...", v3.JUDGE_DEPLOYMENT)
    long_df = v3.judge_similarity(long_df)
    logger.info("Judging truthfulness (kata_similar only) ...")
    long_df = v3.judge_truthfulness(long_df, df)
    logger.info("Reranking across steps ...")
    rerank = run_reranker(df, results, long_df, llm.client, args.workers)

    report = build_report(df, long_df, rerank, Path(args.baseline))
    logger.info("\n=== WATERFALL ===\n%s", report["Waterfall"].to_string(index=False))
    if "Compare Summary" in report:
        logger.info("\n=== COMPARE vs v3 ===\n%s", report["Compare Summary"].to_string(index=False))
    saved = v3.write_workbook(output, report)
    logger.info("Saved -> %s", saved)


if __name__ == "__main__":
    main()
