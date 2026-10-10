"""
Dual-approach test for a KATA-similarity resolver — runs BOTH candidate
sourcing strategies per row and evaluates them side by side:

  Approach A — GLOBAL search_data_elements(query): free-text search across
    the entire KATA data-element index, unconstrained by table.

  Approach B — DATASET-SCOPED: search_datasets(table_name) to find the
    dataset already linked to this table, then use ONLY that dataset's
    pre-curated data_elements as the candidate pool (mirrors how a human
    curator actually works — browse the table's dataset, then pick from
    its associated elements — and was verified to have a 100% hit rate on
    a 6-table sample via test_kata_dataset_search.py).

Same LLM selection step (with reject-all guardrail) is used for both, so
any difference in outcome is attributable to the candidate pool, not the
selection logic.

Usage:
    python test_kata_dual_approach.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from mage_flow.clients import get_llm_client
from mage_flow.common import parse_llm_output
from mage_flow.kata import (
    KATA_DATASET_BASE_URL,
    KATA_ELEMENT_BASE_URL,
    KataOpenSearchClient,
    normalize_alias_key,
)
from evaluate_column_description import evaluate_all, build_summary  # noqa: E402

BASE_DIR = Path(__file__).parent
INPUT_FILE = BASE_DIR / "kata_test_dataset.csv"
OUTPUT_FILE = BASE_DIR / "evaluation" / "kata_dual_approach_results.xlsx"

TABLE_COL = "Tabel"
COLUMN_COL = "Kolom"
DATA_TYPE_COL = "Tipe Data"
PREDICTED_TITLE_COL = "Predicted Business Title"
GT_TITLE_COL = "gt business title"
GT_DESC_COL = "gt column description"
GT_LINK_COL = "Link KATA Dataset"

# Edit this to control how many rows to test. Set to None to run the whole file.
SAMPLE_SIZE = None
RANDOM_SEED = 42

# Raised from 400 -> 900: with 400, GPT OSS sometimes ran out of tokens mid
# reasoning-block and returned an empty completion ("Empty LLM output"),
# which was being miscounted as a deliberate candidate rejection.
SELECTION_MAX_TOKENS = 900

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


# ---------------------------------------------------------------------------
# Candidate-selection prompt (shared by both approaches — same candidate
# shape either way: id, data_element_name, definition, aliases)
# ---------------------------------------------------------------------------

SELECTION_SYSTEM_PROMPT = """\
You are a banking data steward selecting the correct KATA data element for a
database column, from a set of retrieved candidates. This choice becomes
official metadata, so accuracy matters more than always picking something.

INTERNAL REASONING REQUIREMENT:
- Reason internally, in the order below.
- Do NOT reveal reasoning in the output — only the final JSON.

INPUTS:
- Table name: {table_name}
- Column name (technical): {col_name}
- Column data type (light disambiguation signal, may be empty): {data_type}
- Candidates (may or may not be relevant):
{candidates_json}

REASONING STEPS (MANDATORY ORDER):
1. Infer the table's business domain from the table name (e.g. loan, deposit,
   customer master, transaction log, product/parameter table).
2. Interpret what the column likely represents within that domain, using the
   column name and data type.
3. For EACH candidate, check whether its data_element_name/definition/aliases
   are a genuine semantic match for the column AND consistent with the
   table's domain from step 1 — not just superficially similar wording.
   A candidate whose meaning belongs to a different domain (e.g. a "kode
   mata uang" candidate for a column that is clearly about branch codes) is
   a domain mismatch, even if the words look related.
4. If multiple candidates could plausibly fit, prefer the one most
   specifically and unambiguously matching the column — not the most
   generic-sounding one, and not one that requires stretching the table's
   inferred domain to justify.
5. If NONE of the candidates are a genuine match (all domain-mismatched, all
   only superficially similar, or the column's meaning is simply not
   represented among them), you MUST reject all of them — do not force a
   pick just because candidates exist. Picking nothing is the correct,
   expected outcome for many columns; it is not a failure.

OUTPUT FORMAT (STRICT JSON ONLY):
{{
  "selected_id": "<candidate id, or empty string if none genuinely match>",
  "reasoning": "<one short sentence, in Indonesian, explaining the decision>",
  "alternative_ids": ["<id>", "..."]
}}
"""


def _display_key(candidate: dict) -> str:
    """Key used to let the LLM refer back to a candidate. Global search-result
    candidates always have a real 'id'. Dataset-embedded candidates (from
    search_datasets()'s data_elements list) do NOT carry an id/doc_id field —
    only data_element_name/definition — so fall back to the name, which is
    unique enough within one dataset's small element list."""
    return str(candidate.get("id") or candidate.get("data_element_name") or "").strip()


def _select_candidate(llm_client, table_name: str, col_name: str, data_type: str, candidates: list[dict]) -> dict:
    """Returns {'selected': dict|None, 'reasoning': str, 'alternatives': list[dict]}."""
    if not candidates:
        return {"selected": None, "reasoning": "no candidates retrieved", "alternatives": []}

    candidates_json = json.dumps(
        [{"id": _display_key(c), "data_element_name": c["data_element_name"],
          "definition": c["definition"], "aliases": c.get("data_element_alias", [])}
         for c in candidates],
        ensure_ascii=False, indent=2,
    )
    prompt = SELECTION_SYSTEM_PROMPT.format(
        table_name=table_name, col_name=col_name, data_type=data_type or "(unknown)",
        candidates_json=candidates_json,
    )
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "Select the best candidate now, following the reasoning steps above."},
    ]
    try:
        raw = llm_client.create_response(
            messages=messages, sampling_params={"temperature": 0.1, "max_tokens": SELECTION_MAX_TOKENS}
        )
        parsed = parse_llm_output(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Candidate selection LLM call failed for %s.%s: %s", table_name, col_name, exc)
        return {"selected": None, "reasoning": f"LLM error: {exc}", "alternatives": []}

    selected_id = str(parsed.get("selected_id") or "").strip()
    reasoning = str(parsed.get("reasoning") or "").strip()
    alt_ids = [str(a).strip() for a in (parsed.get("alternative_ids") or []) if str(a).strip()]

    by_key = {_display_key(c): c for c in candidates}
    selected = by_key.get(selected_id)
    alternatives = [by_key[i] for i in alt_ids if i in by_key]
    return {"selected": selected, "reasoning": reasoning, "alternatives": alternatives}


def _build_query(row: pd.Series) -> str:
    title = str(row.get(PREDICTED_TITLE_COL, "") or "").strip()
    if title and not title.startswith("[AI]"):
        return title
    return str(row.get(COLUMN_COL, "") or "").strip()


def _backfill_definition(kata_client: KataOpenSearchClient, candidate: dict) -> dict:
    """Dataset-embedded elements (from search_datasets()'s data_elements list)
    often carry a name/alias but an EMPTY definition (the full glossary text
    apparently only lives on the standalone data-element document). If we
    picked a scoped candidate with no definition, look it up by exact name
    against the full data-element index — this also recovers a real id,
    letting us build a valid KATA link for it."""
    if candidate.get("definition"):
        return candidate  # already has a definition, nothing to backfill

    name = str(candidate.get("data_element_name") or "").strip()
    if not name:
        return candidate

    try:
        matches = kata_client.search_data_elements(name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Backfill search failed for %r: %s", name, exc)
        return candidate

    target_key = normalize_alias_key(name)
    exact = next((m for m in matches if normalize_alias_key(m.get("data_element_name")) == target_key), None)
    full = exact or (matches[0] if matches else None)
    if not full:
        return candidate

    merged = dict(candidate)
    merged["definition"] = full.get("definition") or merged.get("definition") or ""
    merged["id"] = full.get("id") or merged.get("id")
    return merged


def main() -> None:
    if not INPUT_FILE.exists():
        logger.error("Input file not found: %s", INPUT_FILE)
        sys.exit(1)

    logger.info("Reading %s …", INPUT_FILE)
    df = pd.read_csv(INPUT_FILE)
    missing = [c for c in (TABLE_COL, COLUMN_COL, GT_TITLE_COL, GT_DESC_COL) if c not in df.columns]
    if missing:
        raise ValueError(f"kata_test_dataset.csv missing columns: {missing}. Found: {list(df.columns)}")

    if SAMPLE_SIZE is not None and SAMPLE_SIZE < len(df):
        df = df.sample(n=SAMPLE_SIZE, random_state=RANDOM_SEED).reset_index(drop=True)
        logger.info("Sampled %d row(s) (SAMPLE_SIZE=%d).", len(df), SAMPLE_SIZE)
    else:
        logger.info("Running all %d row(s).", len(df))

    logger.info("Initializing KATA OpenSearch client and LLM client …")
    kata_client = KataOpenSearchClient()
    llm_client = get_llm_client()

    # Cache dataset search per table — every column of the same table shares
    # the same dataset lookup, no need to repeat it per row. Also cache the
    # dataset's own ID, so we can compare it against the GT dataset link.
    dataset_cache: dict[str, dict] = {}

    def _get_dataset_info(table_name: str) -> dict:
        """Returns {'id': str, 'data_elements': list[dict]}."""
        if table_name not in dataset_cache:
            try:
                datasets = kata_client.search_datasets(table_name, table_name=table_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("search_datasets failed for %s: %s", table_name, exc)
                datasets = []
            # Take the top-ranked dataset with a non-empty element list.
            chosen = next((ds for ds in datasets if ds.get("data_elements")), None)
            dataset_cache[table_name] = {
                "id": chosen.get("id", "") if chosen else "",
                "data_elements": chosen.get("data_elements", []) if chosen else [],
            }
        return dataset_cache[table_name]

    # --- per-row outputs, both approaches ---
    cols = {
        "global": {"n_cand": [], "sel_id": [], "desc": [], "title": [], "reasoning": []},
        "scoped": {"n_cand": [], "sel_id": [], "desc": [], "title": [], "reasoning": [], "real_id": []},
    }
    search_query_col: list[str] = []
    dataset_used_id_col: list[str] = []
    gt_dataset_id_col: list[str] = []

    for _, row in tqdm(df.iterrows(), total=len(df), desc="Dual KATA resolver test"):
        table_name, col_name = str(row[TABLE_COL]), str(row[COLUMN_COL])
        data_type = str(row.get(DATA_TYPE_COL, ""))
        query = _build_query(row)
        search_query_col.append(query)
        gt_dataset_id_col.append(_extract_id_from_link(row.get(GT_LINK_COL, "")))

        # --- Approach A: global data-element search ---
        try:
            global_candidates = kata_client.search_data_elements(query) if query else []
        except Exception as exc:  # noqa: BLE001
            logger.warning("search_data_elements failed for %s: %s", query, exc)
            global_candidates = []
        cols["global"]["n_cand"].append(len(global_candidates))
        result_a = _select_candidate(llm_client, table_name, col_name, data_type, global_candidates)
        sel_a = result_a["selected"]
        cols["global"]["sel_id"].append(_display_key(sel_a) if sel_a else "")
        cols["global"]["desc"].append(sel_a.get("definition") or "" if sel_a else "")
        cols["global"]["title"].append(sel_a.get("data_element_name") or "" if sel_a else "")
        cols["global"]["reasoning"].append(result_a["reasoning"])

        # --- Approach B: dataset-scoped search ---
        dataset_info = _get_dataset_info(table_name)
        dataset_used_id_col.append(dataset_info["id"])
        scoped_candidates = dataset_info["data_elements"]
        cols["scoped"]["n_cand"].append(len(scoped_candidates))
        result_b = _select_candidate(llm_client, table_name, col_name, data_type, scoped_candidates)
        sel_b = result_b["selected"]
        if sel_b:
            sel_b = _backfill_definition(kata_client, sel_b)
        cols["scoped"]["sel_id"].append(_display_key(sel_b) if sel_b else "")
        cols["scoped"]["desc"].append(sel_b.get("definition") or "" if sel_b else "")
        cols["scoped"]["title"].append(sel_b.get("data_element_name") or "" if sel_b else "")
        cols["scoped"]["reasoning"].append(result_b["reasoning"])
        cols["scoped"]["real_id"].append(sel_b.get("id") or "" if sel_b else "")

    df["Search Query (global)"] = search_query_col
    df["N Candidates (global)"] = cols["global"]["n_cand"]
    df["Selected ID (global)"] = cols["global"]["sel_id"]
    df["Selected KATA Link (global)"] = [
        f"{KATA_ELEMENT_BASE_URL}/{i}" if i else "" for i in cols["global"]["sel_id"]
    ]
    df["Predicted Column Description (global)"] = cols["global"]["desc"]
    df["Predicted Business Title (global)"] = cols["global"]["title"]
    df["Selection Reasoning (global)"] = cols["global"]["reasoning"]

    df["GT Dataset ID"] = gt_dataset_id_col
    df["Dataset Used ID (scoped)"] = dataset_used_id_col
    df["Dataset Match? (scoped)"] = [
        (gt == used and gt != "") for gt, used in zip(gt_dataset_id_col, dataset_used_id_col)
    ]
    df["N Candidates (scoped)"] = cols["scoped"]["n_cand"]
    df["Selected ID (scoped)"] = cols["scoped"]["sel_id"]
    # After backfill, a real id is often available (from the full
    # data-element record) — build a link when we have one, blank otherwise.
    df["Selected KATA Link (scoped)"] = [
        f"{KATA_ELEMENT_BASE_URL}/{i}" if i else "" for i in cols["scoped"]["real_id"]
    ]
    df["Predicted Column Description (scoped)"] = cols["scoped"]["desc"]
    df["Predicted Business Title (scoped)"] = cols["scoped"]["title"]
    df["Selection Reasoning (scoped)"] = cols["scoped"]["reasoning"]

    logger.info("Evaluating both approaches against ground truth …")

    async def _run_eval() -> tuple[list[str], list[str], list[str], list[str]]:
        return await asyncio.gather(
            evaluate_all(df, GT_DESC_COL, "Predicted Column Description (global)",
                         "column descriptions", "column description"),
            evaluate_all(df, GT_TITLE_COL, "Predicted Business Title (global)",
                         "business titles", "business title"),
            evaluate_all(df, GT_DESC_COL, "Predicted Column Description (scoped)",
                         "column descriptions", "column description"),
            evaluate_all(df, GT_TITLE_COL, "Predicted Business Title (scoped)",
                         "business titles", "business title"),
        )

    desc_g, title_g, desc_s, title_s = asyncio.run(_run_eval())
    df["Eval Column Description (global)"] = desc_g
    df["Eval Business Title (global)"] = title_g
    df["Eval Column Description (scoped)"] = desc_s
    df["Eval Business Title (scoped)"] = title_s

    summary = pd.concat(
        [
            build_summary(desc_g, "Column Description (global)"),
            build_summary(title_g, "Business Title (global)"),
            build_summary(desc_s, "Column Description (scoped)"),
            build_summary(title_s, "Business Title (scoped)"),
        ],
        ignore_index=True,
    )
    logger.info("Evaluation summary:\n%s", summary.to_string(index=False))

    dataset_match_count = int(df["Dataset Match? (scoped)"].sum())
    dataset_gt_available = int((df["GT Dataset ID"] != "").sum())
    logger.info(
        "Dataset match (scoped approach used the same dataset as GT): %d/%d rows with a GT link (%.1f%%)",
        dataset_match_count, dataset_gt_available,
        (dataset_match_count / dataset_gt_available * 100) if dataset_gt_available else 0.0,
    )

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Results", index=False)
        summary.to_excel(writer, sheet_name="Summary", index=False)

    logger.info("Saved %d rows → %s", len(df), OUTPUT_FILE)


if __name__ == "__main__":
    main()