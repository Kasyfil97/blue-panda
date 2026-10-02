"""
layer_eval.py — evaluate each layer of the NEW resolver hierarchy on its own,
then derive the sequential (first-hit-wins) cascade.

Hierarchy (top = tried first):
    1. Exact Match   ExactMatchResolver  (BM25 /exact/lookup, AS400 priority)
    2. KATA          KataEvidenceResolver (exact alias / technical relation)
                     -> KataSimilarResolver (dataset-scoped LLM selection)
                        [AS400 tables: BM25 dictionary knowledge is retrieved
                         first and passed in as extra evidence]
    3. BM25          BM25Resolver AS400 (run_if: AS400 tables only)
                     -> BM25Resolver Confluence -> BM25Resolver Informatica
    4. Fallback      ConfluenceFallbackResolver -> PureLLMResolver

Method
------
* Every layer runs ISOLATED on every row (a layer never sees another layer's
  result), so each layer's coverage and accuracy is measured independently.
  The sequential waterfall is then derived from those isolated outcomes —
  identical to what a real cascade would return, without re-running anything.
* Generator model = whatever LLM_PROVIDER points at (expected: llama).
  Judge (description/title similarity + truthfulness) = GPT-4.1.
* Business title: KATA layers return the KATA data-element name; every other
  layer gets a title from the flow's own business-title step (LLM, from the
  finished description) — same rule the production flow applies.

Usage
-----
    python layer_eval.py --input "test set eval v2.xlsx"
    python layer_eval.py --input "test set eval v2.xlsx" --sample 20     # smoke test
    python layer_eval.py --resume-raw evaluation/layer_eval_raw_XXXX.json  # re-judge only
"""

from __future__ import annotations

import sys
from pathlib import Path

# This script can live outside the project root (e.g. experiments/layer_eval.py)
# — walk up from here until a folder containing mage_flow/ is found, and put
# that folder AND this script's own folder on sys.path, BEFORE importing
# anything that needs them (mage_flow.*, evaluate_column_description,
# layer_metrics — the latter two are expected to sit next to this file).
# This must run before any of those imports below, not after.
def _find_project_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / "mage_flow").is_dir():
            return candidate
    raise RuntimeError(
        f"Could not find a 'mage_flow' package above {start} — "
        "layer_eval.py must live somewhere inside (or under) the blue-panda project root."
    )


_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _find_project_root(_SCRIPT_DIR)
for _p in (str(_PROJECT_ROOT), str(_SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from tqdm import tqdm

import evaluate_column_description as ecd
import layer_metrics as lm
from mage_flow import config
from mage_flow.clients import BM25Client, get_llm_client
from mage_flow.common import is_missing_desc, norm
from mage_flow.confluence import ConfluenceDiscoveryRequest, ConfluenceDiscoveryService
from mage_flow.confluence_similar import ConfluenceSimilarResolver
from mage_flow.flow import (
    _generate_business_title,
    _max_table_score,
    _should_attempt_confluence_fallback,
    _temporary_knowledge_from_confluence,
)
from mage_flow.kata_similar import KataDatasetPool, KataSimilarResolver, extract_keyword, retrieve_bm25_knowledge
from mage_flow.llm_generation import LLMGeneration
from mage_flow.resolvers import (
    _AS400_SOURCE_TYPES,
    BM25Resolver,
    ConfluenceFallbackResolver,
    ExactMatchResolver,
    KataEvidenceResolver,
    PureLLMResolver,
    ResolverContext,
    _is_as400_table,
)
from mage_flow.source_priority import KNOWLEDGE_PRIORITY_AS400, KNOWLEDGE_PRIORITY_INFORMATICA_CERTIFIED

# Inputs/outputs default to the PROJECT ROOT (so "test set eval v2 mage.xlsx"
# in blue-panda/ is found even when this script runs from experiments/), not
# to this script's own folder. evaluation/ output also lands at the project
# root, next to where run_pipeline.py's own evaluation/ folder already is.
BASE_DIR = _PROJECT_ROOT

TABLE_COL = "Tabel"
COLUMN_COL = "Kolom"
DATA_TYPE_COL = "Tipe Data"
GT_TITLE_COL = "gt business title"
GT_DESC_COL = "gt column description"
# Same names as the earlier experiments so the judge prompts stay comparable.
PRED_DESC_COL = "Predicted Column Description"
PRED_TITLE_COL = "Predicted Business Title"

DEFAULT_INPUT_CANDIDATES = [
    "test set eval v2 mage.xlsx", "test set eval v2.xlsx", "test set eval 6.xlsx", "test_set_eval_v2.xlsx",
]
JUDGE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT_FULL", "gpt-4.1")
ALL_LAYERS = lm.LAYER_ORDER + lm.VARIANT_LAYERS
KATA_LAYERS = ("kata", "kata_noctx")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("layer_eval")


# ---------------------------------------------------------------------------
# Layer construction
# ---------------------------------------------------------------------------

def build_layers(pool: KataDatasetPool) -> Dict[str, Any]:
    """Resolvers are instantiated exactly like production's build_resolver_chain()."""
    as400_bm25 = BM25Resolver(
        allowed_priorities=None,
        source_types=_AS400_SOURCE_TYPES,
        run_if=lambda ctx: _is_as400_table(ctx.table_name),
        resolution_tag="bm25_as400",
    )
    return {
        "as400_bm25": as400_bm25,  # also used (retrieval only) to feed the KATA layer
        "exact_match": [ExactMatchResolver(allowed_priorities={KNOWLEDGE_PRIORITY_AS400}, resolution_tag="exact_as400")],
        "kata_exact": KataEvidenceResolver(),
        "kata_similar_ctx": KataSimilarResolver(pool=pool, use_bm25_context=True),
        "kata_similar_noctx": KataSimilarResolver(pool=pool, use_bm25_context=False),
        "confluence_similar": ConfluenceSimilarResolver(),
        "bm25": [
            as400_bm25,
            BM25Resolver(allowed_priorities=None, source_types=["confluence"], resolution_tag="bm25_confluence"),
            BM25Resolver(
                allowed_priorities={KNOWLEDGE_PRIORITY_INFORMATICA_CERTIFIED},
                source_types=["informatica"],
                resolution_tag="bm25_informatica_certified",
            ),
        ],
        "fallback": [ConfluenceFallbackResolver(), PureLLMResolver()],
    }


def run_resolvers(resolvers: List[Any], ctx: ResolverContext) -> Tuple[Optional[Any], str]:
    """Run one layer's resolvers in order; first usable (non-empty) result wins.

    PureLLMResolver returns resolved=True with an EMPTY description when it
    does not understand the column ("unknown") — that must not count as
    coverage, hence the description check.
    """
    last_error = ""
    for resolver in resolvers:
        try:
            result = resolver.resolve(ctx)
        except Exception as exc:  # noqa: BLE001 - one bad resolver must not sink the row
            logger.warning("%s failed on %s.%s: %s", type(resolver).__name__, ctx.table_name, ctx.col_name, exc)
            last_error = f"{type(resolver).__name__}: {exc}"
            continue
        if result.resolved and not is_missing_desc(result.description or ""):
            return result, ""
    return None, last_error


def derive_title(result: Any, ctx: ResolverContext, llm: LLMGeneration, bt_sampling: Optional[dict]) -> str:
    """KATA layers -> the KATA data-element name; others -> flow's title step."""
    for item in result.knowledge or []:
        title = norm(item.get("business_title"))
        if title:
            return title
    for item in result.knowledge or []:
        if norm(item.get("source_type")).lower() == "kata" and norm(item.get("data_element_name")):
            return norm(item.get("data_element_name"))
    try:
        return _generate_business_title(
            table_name=ctx.table_name, col_name=ctx.col_name, col_description=norm(result.description),
            system_context=ctx.system_context, knowledge=result.knowledge, llm=llm, sampling_params=bt_sampling,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("business title generation failed for %s.%s: %s", ctx.table_name, ctx.col_name, exc)
        return ""


def to_outcome(result: Optional[Any], error: str, ctx: ResolverContext, llm: LLMGeneration, bt_sampling: Optional[dict]) -> Dict[str, Any]:
    """Flatten a ResolverResult into a JSON-safe dict (so raw runs can be saved)."""
    if result is None:
        return {"covered": False, "tag": "", "desc": "", "title": "", "reason": "", "error": error,
                "n_candidates": None, "evidence": "", "meta": {}, "page_titles": "", "page_links": ""}
    first = (result.knowledge or [{}])[0]
    meta = dict(first.get("kata_similar") or {})
    evidence = meta.pop("bm25_evidence", "") or ""
    # confluence_similar can cite MULTIPLE pages (one knowledge item per
    # candidate used) — join titles/links across all of them, not just the
    # first, so the audit trail shows everything the synthesis actually saw.
    page_titles = "; ".join(norm(k.get("PageTitle")) for k in (result.knowledge or []) if norm(k.get("PageTitle")))
    page_links = "; ".join(norm(k.get("PageUrl")) for k in (result.knowledge or []) if norm(k.get("PageUrl")))
    return {
        "covered": True,
        "tag": result.resolution_tag,
        "desc": norm(result.description),
        "title": derive_title(result, ctx, llm, bt_sampling),
        "reason": norm(first.get("match_reason")),
        "error": "",
        "n_candidates": meta.get("n_candidates"),
        "evidence": evidence,
        "meta": meta,
        "page_titles": page_titles,
        "page_links": page_links,
    }


# ---------------------------------------------------------------------------
# Per-table prep (mirrors flow.generate_metadata up to the column loop)
# ---------------------------------------------------------------------------

class Deps:
    def __init__(self, bm25: BM25Client, llm: LLMGeneration, settings: Dict[str, Any], pool: KataDatasetPool) -> None:
        self.bm25 = bm25
        self.llm = llm
        self.settings = settings
        self.pool = pool
        self.sampling = settings.get("llm", {}).get("sampling", {"temperature": 0.1, "top_p": 0.9, "max_tokens": 1200})
        bm25_cfg = settings.get("bm25", {})
        table_cfg = bm25_cfg.get("table_search", {})
        self.bm25_params = {
            "table": {"top_k": int(table_cfg.get("top_k", 20)), "threshold": float(table_cfg.get("threshold", 8.0))},
            "term": bm25_cfg.get("term_search", {}),
            "global": bm25_cfg.get("global_search", {}),
        }
        self.bt_sampling = settings.get("business_title", {}).get("sampling")


def prepare_table(table: str, columns: List[str], deps: Deps) -> Dict[str, Any]:
    """Everything the flow computes once per table, so columns can run in parallel."""
    request_id = str(uuid.uuid4())
    payload = deps.bm25.get_system_context(table_name=table, request_id=request_id)
    payload = payload if isinstance(payload, dict) else {}
    table_term = deps.bm25.get_table_term_context(table, request_id=request_id) or []
    table_hits = deps.bm25.get_table_knowledge(
        table_name=table, top_k=deps.bm25_params["table"]["top_k"],
        threshold=deps.bm25_params["table"]["threshold"], request_id=request_id,
    ) or []

    temporary: Dict[str, List[Dict[str, Any]]] = {}
    confluence_candidates: List[Any] = []  # raw discover() hits, shared with ConfluenceSimilarResolver
    column_dicts = [{"ColumnName": c, "ColumnDescription": ""} for c in columns]
    # NOTE: the production gate (_should_attempt_confluence_fallback — only
    # attempt Confluence when the table's BM25 score is weak) is deliberately
    # BYPASSED here: this evaluation needs to measure the Confluence
    # resolver's own coverage/accuracy in isolation, same as every other
    # layer. In a first full run every single table had a comfortably high
    # BM25 score, so the gate never opened and Confluence was never even
    # tried — its 0% coverage reflected the gate, not the resolver's own
    # quality. `gate_open` is still recorded below so the report can note,
    # per row, whether production's real gate would actually have allowed
    # this attempt.
    gate_open = _should_attempt_confluence_fallback(settings=deps.settings, table_hits=table_hits, columns=column_dicts)
    gate_note = "gate_open" if gate_open else "gate_would_be_closed_in_prod"
    confluence_diag = gate_note
    cfg = deps.settings.get("confluence_fallback", {})
    if not norm(config.CONFLUENCE_PAT):
        # ConfluenceClient.search() would silently return [] here (only a
        # warning from its own logger, no exception) — surfaced explicitly
        # so it's distinguishable from "found nothing" in the log line below.
        confluence_diag = f"{gate_note} | no_pat_configured"
    else:
        try:
            response = ConfluenceDiscoveryService().discover(
                ConfluenceDiscoveryRequest(
                    table_name=table, columns=columns, source_schema=None,
                    source_system=norm(payload.get("system_name")) or None,
                    limit=int(cfg.get("limit", 10)), request_id=request_id,
                )
            )
            n_raw_candidates = len(response.candidates)
            confluence_candidates = list(response.candidates)
            temporary, _stats = _temporary_knowledge_from_confluence(
                table_name=table, response=response,
                min_confidence=float(cfg.get("min_confidence", 0.8)), allowed_labels=cfg.get("allowed_labels"),
            )
            if n_raw_candidates:
                # Breaks down exactly WHY candidates get filtered out: never
                # reaching "usable_metadata" at all (the pages found don't
                # have a clear field-level table) vs. reaching it but below
                # min_confidence (a threshold-tuning question instead).
                from collections import Counter
                label_counts = Counter(c.label for c in response.candidates)
                usable = [c for c in response.candidates if c.label == "usable_metadata"]
                max_conf_usable = max((c.confidence for c in usable), default=None)
                outcome = (
                    f"ok: {n_raw_candidates} raw, labels={dict(label_counts)}, "
                    f"max_confidence_among_usable_metadata={max_conf_usable}, "
                    f"{len(temporary)} column(s) survived filter"
                )
            else:
                outcome = "no_candidates_found"
            confluence_diag = f"{gate_note} | {outcome}"
        except Exception as exc:  # noqa: BLE001
            logger.warning("confluence discovery failed for %s: %s", table, exc)
            confluence_diag = f"{gate_note} | error: {exc}"

    # Warm the KATA dataset cache up front (fails fast if KATA is unreachable,
    # and keeps worker threads from contending on the pool lock).
    deps.pool.get(table, extract_keyword_fn=lambda raw: extract_keyword(deps.llm.client, raw))

    logger.info(
        "prepared %s: as400=%s max_table_score=%.1f table_terms=%d temporary_evidence_cols=%d | confluence: %s",
        table, _is_as400_table(table), _max_table_score(table_hits), len(table_term), len(temporary), confluence_diag,
    )
    return {
        "request_id": request_id, "system_context": payload.get("context", "") or "",
        "table_term": table_term, "table_hits": table_hits, "temporary": temporary,
        "confluence_candidates": confluence_candidates,
    }


# ---------------------------------------------------------------------------
# Per-row execution
# ---------------------------------------------------------------------------

def process_row(ri: int, row: pd.Series, prep: Dict[str, Any], layers: Dict[str, Any], deps: Deps) -> Dict[str, Any]:
    table, col = norm(row[TABLE_COL]), norm(row[COLUMN_COL])
    tp = prep[table]
    ctx = ResolverContext(
        table_name=table, col_name=col, system_context=tp["system_context"], table_hits=tp["table_hits"],
        bm25=deps.bm25, llm=deps.llm, sampling_params=deps.sampling, bm25_params=deps.bm25_params,
        settings=deps.settings, request_id=tp["request_id"],
        temporary_knowledge_by_column=tp["temporary"], table_term_context=tp["table_term"],
    )
    # Extra attributes read by KataSimilarResolver / ConfluenceSimilarResolver
    # (kept off the shared dataclass).
    ctx.data_type = norm(row.get(DATA_TYPE_COL))
    ctx.extra_knowledge = []
    ctx.confluence_candidates = tp["confluence_candidates"]

    def outcome(resolvers: List[Any]) -> Dict[str, Any]:
        result, error = run_resolvers(resolvers, ctx)
        return to_outcome(result, error, ctx, deps.llm, deps.bt_sampling)

    outcomes: Dict[str, Dict[str, Any]] = {}

    # Layer 1 — exact match
    outcomes["exact_match"] = outcome(layers["exact_match"])

    # AS400 tables: retrieve BM25 dictionary knowledge (no synthesis) as KATA context.
    try:
        evidence = retrieve_bm25_knowledge(layers["as400_bm25"], ctx)
    except Exception as exc:  # noqa: BLE001
        logger.warning("BM25 evidence retrieval failed for %s.%s: %s", table, col, exc)
        evidence = []
    ctx.extra_knowledge = evidence

    # Layer 2 — KATA: exact first; similar only if exact missed.
    kata_exact = outcome([layers["kata_exact"]])
    if kata_exact["covered"]:
        outcomes["kata"] = kata_exact
        outcomes["kata_noctx"] = kata_exact
    else:
        outcomes["kata"] = outcome([layers["kata_similar_ctx"]])
        # The two KATA variants can only differ when BM25 evidence exists.
        outcomes["kata_noctx"] = outcome([layers["kata_similar_noctx"]]) if evidence else outcomes["kata"]

    # Layer 3 — BM25 resolvers; Layer 4 — fallback
    outcomes["bm25"] = outcome(layers["bm25"])
    outcomes["fallback"] = outcome(layers["fallback"])

    # Experimental variant — LLM-synthesized alternative to the regex-based
    # ConfluenceFallbackResolver already inside "fallback" above. Isolated,
    # not ranked, not part of the waterfall; reuses the same per-table
    # Confluence search (no extra network calls).
    outcomes["confluence_similar"] = outcome([layers["confluence_similar"]])

    return {"row_id": ri, "is_as400": _is_as400_table(table), "n_evidence": len(evidence), "outcomes": outcomes}


def run_all_rows(df: pd.DataFrame, deps: Deps, workers: int) -> List[Dict[str, Any]]:
    layers = build_layers(deps.pool)
    columns_by_table: Dict[str, List[str]] = {}
    for _, row in df.iterrows():
        columns_by_table.setdefault(norm(row[TABLE_COL]), []).append(norm(row[COLUMN_COL]))

    prep: Dict[str, Any] = {}
    for table, cols in tqdm(columns_by_table.items(), desc="Preparing tables"):
        prep[table] = prepare_table(table, cols, deps)

    results: List[Optional[Dict[str, Any]]] = [None] * len(df)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(process_row, ri, row, prep, layers, deps): ri for ri, row in df.iterrows()}
        for future in tqdm(as_completed(futures), total=len(futures), desc="Running layers"):
            ri = futures[future]
            try:
                results[ri] = future.result()
            except Exception as exc:  # noqa: BLE001
                logger.error("row %d failed entirely: %s", ri, exc)
                empty = {"covered": False, "tag": "", "desc": "", "title": "", "reason": "", "error": str(exc),
                         "n_candidates": None, "evidence": "", "meta": {}}
                results[ri] = {"row_id": ri, "is_as400": _is_as400_table(norm(df.at[ri, TABLE_COL])),
                               "n_evidence": 0, "outcomes": {layer: dict(empty) for layer in ALL_LAYERS}}
    return [r for r in results if r is not None]


# ---------------------------------------------------------------------------
# Long table, judging
# ---------------------------------------------------------------------------

def build_long_df(df: pd.DataFrame, results: List[Dict[str, Any]]) -> pd.DataFrame:
    records = []
    for res in results:
        ri = res["row_id"]
        for layer in ALL_LAYERS:
            o = res["outcomes"][layer]
            records.append({
                "row_id": ri, "Tabel": norm(df.at[ri, TABLE_COL]), "Kolom": norm(df.at[ri, COLUMN_COL]),
                "is_as400": res["is_as400"], "layer": layer, "covered": bool(o["covered"]), "tag": o["tag"],
                "pred_desc": o["desc"], "pred_title": o["title"], "reason": o["reason"], "error": o["error"],
                "n_candidates": o["n_candidates"], "evidence": o["evidence"],
                "page_titles": o.get("page_titles", ""), "page_links": o.get("page_links", ""),
                "gt_desc": df.at[ri, GT_DESC_COL], "gt_title": df.at[ri, GT_TITLE_COL],
                "eval_desc": "", "eval_title": "", "truthfulness": "", "truthfulness_reason": "",
            })
    return pd.DataFrame(records)


def judge_similarity(long_df: pd.DataFrame) -> pd.DataFrame:
    """Score covered rows' description and title against ground truth (GPT-4.1)."""
    covered_idx = long_df.index[long_df["covered"]]
    if len(covered_idx) == 0:
        return long_df

    def labels_for(gt_col: str, pred_col: str, gt_name: str, pred_name: str, field: str, singular: str) -> Dict[int, str]:
        out: Dict[int, str] = {}
        sendable = []
        for i in covered_idx:
            gt_missing, pred_missing = is_missing_desc(norm(long_df.at[i, gt_col])), is_missing_desc(norm(long_df.at[i, pred_col]))
            if gt_missing:
                out[i] = "no_gt"
            elif pred_missing:
                out[i] = "no_pred"
            else:
                sendable.append(i)
        if sendable:
            frame = pd.DataFrame({
                gt_name: [long_df.at[i, gt_col] for i in sendable],
                pred_name: [long_df.at[i, pred_col] for i in sendable],
            })
            labels = asyncio.run(ecd.evaluate_all(frame, gt_name, pred_name, field, singular, deployment=JUDGE_DEPLOYMENT))
            out.update(dict(zip(sendable, labels)))
        return out

    desc = labels_for("gt_desc", "pred_desc", GT_DESC_COL, PRED_DESC_COL, "column descriptions", "column description")
    title = labels_for("gt_title", "pred_title", GT_TITLE_COL, PRED_TITLE_COL, "business titles", "business title")
    long_df.loc[list(desc), "eval_desc"] = pd.Series(desc)
    long_df.loc[list(title), "eval_title"] = pd.Series(title)
    return long_df


TRUTHFULNESS_SYSTEM_PROMPT = """\
You are an independent auditor checking whether a metadata-matching decision
was genuinely justified, or fabricated a connection that isn't really there.

You did NOT make this decision — a different system selected a KATA data
element as the match for a database column. Your job is only to verify it.

INPUTS:
- Table name: {table_name}
- Column name: {col_name}
- Column data type: {data_type}
- Selected candidate name: {candidate_name}
- Selected candidate definition: {candidate_definition}
- Retrieved supporting evidence for this column (may be empty; when present
  it counts as explicit support for interpreting the column):
{evidence_block}

TASK:
Judge whether picking this candidate for this column is genuinely, directly
supported by the information given — not a stretch, not a superficial
wording coincidence, and not reliant on an assumption about what an
abbreviation "probably" means when that assumption isn't stated anywhere in
the inputs above.

Answer "false" (not truthful / hallucinated) when:
- The candidate's actual meaning belongs to a different concept/domain than
  what the column plausibly represents, even if some words overlap.
- The match requires assuming a specific unstated expansion or meaning for
  an abbreviation/acronym in the column or table name.
- The connection is vague/generic enough that it could just as easily match
  many unrelated columns.

Answer "true" (genuinely supported) when the candidate's name/definition
directly and specifically corresponds to what the column represents, given
only the table/column/data-type/evidence context actually provided.

OUTPUT FORMAT (STRICT JSON ONLY):
{{"truthful": true, "reasoning": "<one short sentence, in Indonesian>"}}
"""


async def truthfulness_batch(items: List[Dict[str, str]]) -> List[Tuple[str, str]]:
    """Independent GPT-4.1 audit of KATA selections. Returns (label, reasoning) per item."""
    from openai import AsyncAzureOpenAI

    client = AsyncAzureOpenAI(azure_endpoint=ecd.AZURE_ENDPOINT, api_key=ecd.AZURE_API_KEY, api_version=ecd.AZURE_API_VER)
    semaphore = asyncio.Semaphore(ecd.CONCURRENCY)

    async def one(item: Dict[str, str]) -> Tuple[str, str]:
        prompt = TRUTHFULNESS_SYSTEM_PROMPT.format(**item)
        messages = [{"role": "system", "content": prompt}, {"role": "user", "content": "Assess now."}]
        async with semaphore:
            for attempt in range(1, 4):
                try:
                    resp = await client.chat.completions.create(
                        model=JUDGE_DEPLOYMENT, messages=messages, temperature=0, max_tokens=250,
                    )
                    raw = resp.choices[0].message.content.strip().replace("```json", "").replace("```", "").strip()
                    parsed = json.loads(raw)
                    return ("true" if bool(parsed.get("truthful")) else "false"), norm(parsed.get("reasoning"))
                except Exception as exc:  # noqa: BLE001
                    if attempt == 3:
                        return "error", str(exc)
                    await asyncio.sleep(2 * attempt)
        return "error", "unreachable"

    return list(await asyncio.gather(*[one(item) for item in items]))


def judge_truthfulness(long_df: pd.DataFrame, df: pd.DataFrame) -> pd.DataFrame:
    idx = long_df.index[long_df["covered"] & long_df["layer"].isin(KATA_LAYERS)]
    if len(idx) == 0:
        return long_df
    items = []
    for i in idx:
        row = long_df.loc[i]
        items.append({
            "table_name": row["Tabel"], "col_name": row["Kolom"],
            "data_type": norm(df.at[row["row_id"], DATA_TYPE_COL]) or "(unknown)",
            "candidate_name": row["pred_title"], "candidate_definition": row["pred_desc"],
            "evidence_block": row["evidence"] or "(none)",
        })
    verdicts = asyncio.run(truthfulness_batch(items))
    long_df.loc[idx, "truthfulness"] = [v[0] for v in verdicts]
    long_df.loc[idx, "truthfulness_reason"] = [v[1] for v in verdicts]
    return long_df


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

NOTES = [
    ("Jira ticket mapping", "[EV] Mengukur Failure Rate -> Ranking sheet, 'Failure Rate (execution errors / rows)' + 'Execution Errors' / 'No Match (legitimate, no error)' columns. [EV] Mengukur Persentase Coverage -> Ranking sheet, 'Coverage %'. [MO] Menyusun Hierarki -> Ranking sheet, sort by whichever 'Rank (...)' column matches what was agreed with the manager (yield/precision/coverage/failure rate are all provided since the primary metric wasn't pinned down). [EV] Mengevaluasi Performa Akhir pada Seluruh Data Uji -> Waterfall sheet, 'cascade total' row — only valid once this was run on the FULL test set, not a --sample."),
    ("Failure Rate vs Coverage", "These are NOT complements of each other. Coverage % = rows with a usable answer. Failure Rate = execution errors only (exceptions, API failures, judge parse failures) / all rows. A row that is 'not covered' but has NO error (e.g. KATA correctly rejecting every candidate) counts against coverage but NOT against failure rate — it was a working resolver making the correct call to abstain, not a failure."),
    ("What this measures", "Each layer of the new resolver hierarchy is run ISOLATED on every row of the test set, so its coverage and accuracy are measured independently of the other layers."),
    ("Coverage", "Share of rows for which the layer returned a non-empty description. (Pure-LLM 'unknown' answers count as NOT covered.)"),
    ("Accuracy (Desc Similar %)", "Among covered rows that have a ground truth, the share the GPT-4.1 judge rated 'similar'. Partial/Unsimilar are the other two labels."),
    ("Yield", "Similar rows / ALL rows = coverage x precision. Primary ranking key: it rewards a layer that answers many rows correctly, not one that answers few rows perfectly or many rows badly. Precision-only and coverage-only ranks are shown next to it."),
    ("Waterfall", "Sequential cascade in hierarchy order: each row is served by the first layer that covers it, then the run stops. Derived from the isolated outcomes, so it equals a real first-hit-wins run. 'Newly covered' + its accuracy show each layer's marginal contribution."),
    ("Layer 1", "ExactMatchResolver = BM25 service /exact/lookup restricted to AS400-priority sources (same resolver as the current chain)."),
    ("Layer 2", "KataEvidenceResolver (exact alias / technical relation) then KataSimilarResolver (dataset-scoped LLM selection + rescue). For AS400 tables, retrieved BM25 dictionary knowledge is passed to the selector (and to the truthfulness judge) as evidence. Variant 2b runs the same without that evidence."),
    ("Layer 3", "BM25Resolver AS400 (AS400 tables only, via _is_as400_table) -> Confluence -> Informatica. The AS400 resolver's coverage is reported against AS400 rows only."),
    ("Layer 4", "ConfluenceFallbackResolver (production gate: only when the table's BM25 score is weak) -> PureLLMResolver."),
    ("Business title", "KATA layers: the KATA data-element name. Other layers: title generated from the finished description by the flow's own business-title step. Title accuracy is therefore not perfectly like-for-like across layers."),
    ("Truthfulness", "Independent GPT-4.1 audit (True/False) of KATA selections only. Strict standard: an abbreviation interpretation must be supported by the inputs; for AS400 tables retrieved BM25 knowledge counts as support. Earlier runs showed the strict standard also flags correct matches, so read it as 'evidence-backed', not 'hallucinated'."),
    ("Judge / generator", "Generator: LLM_PROVIDER endpoint (llama). Judge: " + JUDGE_DEPLOYMENT + "."),
]


def wide_results(df: pd.DataFrame, long_df: pd.DataFrame) -> pd.DataFrame:
    winners = lm.winner_by_row(long_df, lm.LAYER_ORDER)
    base = df[[TABLE_COL, COLUMN_COL, DATA_TYPE_COL, GT_TITLE_COL, GT_DESC_COL]].copy() if DATA_TYPE_COL in df.columns \
        else df[[TABLE_COL, COLUMN_COL, GT_TITLE_COL, GT_DESC_COL]].copy()
    base["AS400 table"] = [ _is_as400_table(norm(t)) for t in base[TABLE_COL] ]
    base["Cascade winner"] = [winners.get(i, "") for i in base.index]
    for layer in ALL_LAYERS:
        sub = long_df[long_df["layer"] == layer].set_index("row_id")
        prefix = f"[{layer}]"
        base[f"{prefix} covered"] = [bool(sub["covered"].get(i, False)) for i in base.index]
        base[f"{prefix} resolver"] = [sub["tag"].get(i, "") for i in base.index]
        base[f"{prefix} description"] = [sub["pred_desc"].get(i, "") for i in base.index]
        base[f"{prefix} business title"] = [sub["pred_title"].get(i, "") for i in base.index]
        base[f"{prefix} eval desc"] = [sub["eval_desc"].get(i, "") for i in base.index]
        base[f"{prefix} eval title"] = [sub["eval_title"].get(i, "") for i in base.index]
        if layer in KATA_LAYERS:
            base[f"{prefix} truthfulness"] = [sub["truthfulness"].get(i, "") for i in base.index]
            base[f"{prefix} truthfulness reasoning"] = [sub["truthfulness_reason"].get(i, "") for i in base.index]
            base[f"{prefix} selection reasoning"] = [sub["reason"].get(i, "") for i in base.index]
            base[f"{prefix} BM25 evidence used"] = [sub["evidence"].get(i, "") for i in base.index]
        if layer == "confluence_similar":
            base[f"{prefix} page titles used"] = [sub["page_titles"].get(i, "") for i in base.index]
            base[f"{prefix} page links used"] = [sub["page_links"].get(i, "") for i in base.index]
    return base


def write_workbook(path: Path, sheets: Dict[str, pd.DataFrame]) -> Path:
    """Write all sheets; if the target is locked (open in Excel) fall back to a timestamped file."""
    from openpyxl.styles import Alignment, Font, PatternFill

    def _write(target: Path) -> None:
        with pd.ExcelWriter(target, engine="openpyxl") as writer:
            for name, frame in sheets.items():
                frame.to_excel(writer, sheet_name=name, index=False)
                ws = writer.sheets[name]
                ws.freeze_panes = "A2"
                for cell in ws[1]:
                    cell.font = Font(bold=True, color="FFFFFF")
                    cell.fill = PatternFill(start_color="2E75B6", end_color="2E75B6", fill_type="solid")
                    cell.alignment = Alignment(wrap_text=True, vertical="center")
                for idx, col in enumerate(frame.columns, start=1):
                    letter = ws.cell(row=1, column=idx).column_letter
                    is_pct = "%" in str(col) or "Yield" in str(col)
                    ws.column_dimensions[letter].width = 16 if is_pct else min(48, max(12, len(str(col)) + 2))
                    if is_pct:
                        for r in range(2, len(frame) + 2):
                            ws.cell(row=r, column=idx).number_format = "0.0%"
            if "Notes" in sheets:
                writer.sheets["Notes"].column_dimensions["A"].width = 28
                writer.sheets["Notes"].column_dimensions["B"].width = 120
                for row in writer.sheets["Notes"].iter_rows(min_row=2):
                    for cell in row:
                        cell.alignment = Alignment(wrap_text=True, vertical="top")

    try:
        _write(path)
        return path
    except PermissionError:
        fallback = path.with_name(f"{path.stem}_{dt.datetime.now():%Y%m%d_%H%M%S}{path.suffix}")
        logger.warning("Could not write %s (open in Excel?) -- saving to %s instead.", path, fallback)
        _write(fallback)
        return fallback


def build_report(df: pd.DataFrame, long_df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    n_rows = len(df)
    n_as400 = int(sum(_is_as400_table(norm(t)) for t in df[TABLE_COL]))
    summary = lm.layer_summary(long_df, n_rows, ALL_LAYERS)
    ranking = lm.ranking_table(summary).drop(columns=["layer_key"])
    return {
        "Ranking": ranking,
        "Waterfall": lm.waterfall(long_df, n_rows),
        "Resolver Breakdown": lm.resolver_breakdown(long_df, n_rows, n_as400, ALL_LAYERS),
        "Results": wide_results(df, long_df),
        "Notes": pd.DataFrame(NOTES, columns=["Topic", "Description"]),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _resolve_input(arg: Optional[str]) -> Path:
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Layer-by-layer evaluation of the resolver hierarchy")
    parser.add_argument("--input", default=None, help="Excel test set (Tabel, Kolom, Tipe Data, gt business title, gt column description)")
    parser.add_argument("--output", default=None, help="Output .xlsx (default: evaluation/layer_eval_<date>.xlsx)")
    parser.add_argument("--sample", type=int, default=None, help="Random sample of N rows (smoke test)")
    parser.add_argument("--workers", type=int, default=4, help="Parallel row workers (default 4)")
    parser.add_argument("--resume-raw", default=None, help="Skip generation; load a saved raw JSON and re-run judging/aggregation")
    args = parser.parse_args()

    stamp = f"{dt.datetime.now():%Y%m%d_%H%M%S}"
    out_dir = BASE_DIR / "evaluation"
    out_dir.mkdir(parents=True, exist_ok=True)
    output = Path(args.output) if args.output else out_dir / f"layer_eval_{dt.datetime.now():%Y%m%d}.xlsx"

    provider = os.getenv("LLM_PROVIDER", "bedrock")
    logger.info("LLM_PROVIDER=%s | judge deployment=%s", provider, JUDGE_DEPLOYMENT)
    if provider.strip().lower() != "llama":
        logger.warning("LLM_PROVIDER is %r, not 'llama' -- generation will not use the Llama/Qwen endpoint.", provider)

    if args.resume_raw:
        raw = json.loads(Path(args.resume_raw).read_text(encoding="utf-8"))
        df = pd.DataFrame(raw["rows"])
        results = raw["results"]
        logger.info("Resumed %d rows from %s (generation skipped).", len(df), args.resume_raw)
    else:
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

        settings = config.default_settings()
        llm = LLMGeneration(get_llm_client())
        logger.info("Generator client: %s", type(llm.client).__name__)
        deps = Deps(BM25Client(), llm, settings, KataDatasetPool())
        results = run_all_rows(df, deps, args.workers)

        # Checkpoint BEFORE judging/writing: a locked output file or a judge
        # outage must never cost a whole generation run.
        raw_path = out_dir / f"layer_eval_raw_{stamp}.json"
        raw_path.write_text(
            json.dumps({"rows": json.loads(df.to_json(orient="records", force_ascii=False)), "results": results},
                       ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        logger.info("Raw run saved -> %s  (re-judge later with --resume-raw)", raw_path)

    long_df = build_long_df(df, results)
    logger.info("Judging similarity (%s) ...", JUDGE_DEPLOYMENT)
    long_df = judge_similarity(long_df)
    logger.info("Judging truthfulness of KATA selections (%s) ...", JUDGE_DEPLOYMENT)
    long_df = judge_truthfulness(long_df, df)

    report = build_report(df, long_df)
    logger.info("\n=== RANKING ===\n%s", report["Ranking"].to_string(index=False))
    logger.info("\n=== WATERFALL ===\n%s", report["Waterfall"].to_string(index=False))
    saved = write_workbook(output, report)
    logger.info("Saved -> %s", saved)


if __name__ == "__main__":
    main()