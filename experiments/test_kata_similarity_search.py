"""
Standalone test for a KATA-similarity resolver (NOT wired into the main
resolver chain yet — this is a separate exploration script, per design
discussion: does the AI pick the same KATA data element a human curator
would have picked?

Flow per row:
  1. Build a search query from Predicted Business Title (if it's grounded,
     i.e. does NOT start with "[AI]") — otherwise fall back to the column
     name alone. Table name is NOT put into the search query text (mirrors
     the production frontend's own query-building logic); it's passed to
     the LLM as disambiguation context instead.
  2. Call KataOpenSearchClient().search_data_elements(query) -> up to 8
     Active candidates (id, data_element_name, definition, aliases).
  3. Ask an LLM to pick the single best-matching candidate — or explicitly
     reject all of them if none genuinely fits the table's domain — with a
     one-line reasoning and a list of runner-up alternatives (for audit).
  4. If a candidate was picked, its `definition` becomes the predicted
     column description and `data_element_name` becomes the predicted
     business title.
  5. Evaluate (semantic LLM judge, same as prior evaluations) the picked
     candidate's definition/name against gt column description / gt
     business title.

Usage:
    python test_kata_similarity_search.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from mage_flow.clients import get_llm_client
from mage_flow.common import parse_llm_output
from mage_flow.kata import KataOpenSearchClient
from evaluate_column_description import evaluate_all, build_summary  # noqa: E402

BASE_DIR = Path(__file__).parent
INPUT_FILE = BASE_DIR / "kata_test_dataset.csv"
OUTPUT_FILE = BASE_DIR / "evaluation" / "kata_similarity_test_results.xlsx"

TABLE_COL = "Tabel"
COLUMN_COL = "Kolom"
DATA_TYPE_COL = "Tipe Data"
PREDICTED_TITLE_COL = "Predicted Business Title"
GT_TITLE_COL = "gt business title"
GT_DESC_COL = "gt column description"

# Edit this to control how many rows to test. Set to None to run the whole file.
SAMPLE_SIZE = 15
RANDOM_SEED = 42

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Candidate-selection prompt (kept inline here, not in mage_flow/prompts.py,
# since this is a standalone exploration — promote it later if it proves out)
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
- Candidates (retrieved from KATA by text search, NOT guaranteed relevant):
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
  "alternative_ids": ["<id>", "..."]   // other candidates that were plausible but not chosen; empty list if none
}}
"""


def _build_query(row: pd.Series) -> str:
    title = str(row.get(PREDICTED_TITLE_COL, "") or "").strip()
    if title and not title.startswith("[AI]"):
        return title
    return str(row.get(COLUMN_COL, "") or "").strip()


def _search_candidates(query: str, kata_client: KataOpenSearchClient) -> list[dict]:
    if not query:
        return []
    try:
        return kata_client.search_data_elements(query)
    except Exception as exc:  # noqa: BLE001
        logger.warning("KATA search failed for query %r: %s", query, exc)
        return []


def _select_candidate(
    llm_client, table_name: str, col_name: str, data_type: str, candidates: list[dict],
) -> dict:
    """Returns {'selected': dict|None, 'reasoning': str, 'alternatives': list[dict]}."""
    if not candidates:
        return {"selected": None, "reasoning": "no candidates retrieved", "alternatives": []}

    candidates_json = json.dumps(
        [{"id": c["id"], "data_element_name": c["data_element_name"],
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
        raw = llm_client.create_response(messages=messages, sampling_params={"temperature": 0.1, "max_tokens": 400})
        parsed = parse_llm_output(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Candidate selection LLM call failed for %s.%s: %s", table_name, col_name, exc)
        return {"selected": None, "reasoning": f"LLM error: {exc}", "alternatives": []}

    selected_id = str(parsed.get("selected_id") or "").strip()
    reasoning = str(parsed.get("reasoning") or "").strip()
    alt_ids = [str(a).strip() for a in (parsed.get("alternative_ids") or []) if str(a).strip()]

    by_id = {c["id"]: c for c in candidates}
    selected = by_id.get(selected_id)
    alternatives = [by_id[i] for i in alt_ids if i in by_id]
    return {"selected": selected, "reasoning": reasoning, "alternatives": alternatives}


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

    predicted_desc: list[str] = []
    predicted_title: list[str] = []
    selected_id_col: list[str] = []
    reasoning_col: list[str] = []
    alternatives_col: list[str] = []
    n_candidates_col: list[int] = []
    search_query_col: list[str] = []

    for _, row in tqdm(df.iterrows(), total=len(df), desc="KATA similarity search"):
        query = _build_query(row)
        search_query_col.append(query)

        candidates = _search_candidates(query, kata_client)
        n_candidates_col.append(len(candidates))

        result = _select_candidate(
            llm_client,
            table_name=str(row[TABLE_COL]), col_name=str(row[COLUMN_COL]),
            data_type=str(row.get(DATA_TYPE_COL, "")), candidates=candidates,
        )

        selected = result["selected"]
        if selected:
            predicted_desc.append(selected.get("definition") or "")
            predicted_title.append(selected.get("data_element_name") or "")
            selected_id_col.append(selected.get("id") or "")
        else:
            predicted_desc.append("")
            predicted_title.append("")
            selected_id_col.append("")

        reasoning_col.append(result["reasoning"])
        alternatives_col.append(
            "; ".join(f"{a['data_element_name']} ({a['id']})" for a in result["alternatives"])
        )

    df["Search Query"] = search_query_col
    df["N Candidates"] = n_candidates_col
    df["Selected KATA ID"] = selected_id_col
    df["Predicted Column Description (KATA)"] = predicted_desc
    df["Predicted Business Title (KATA)"] = predicted_title
    df["Selection Reasoning"] = reasoning_col
    df["Alternative Candidates"] = alternatives_col

    logger.info("Evaluating predicted description/title against ground truth …")

    async def _run_eval() -> tuple[list[str], list[str]]:
        return await asyncio.gather(
            evaluate_all(df, GT_DESC_COL, "Predicted Column Description (KATA)",
                         "column descriptions", "column description"),
            evaluate_all(df, GT_TITLE_COL, "Predicted Business Title (KATA)",
                         "business titles", "business title"),
        )

    desc_labels, title_labels = asyncio.run(_run_eval())
    df["LLM Evaluation (Column Description)"] = desc_labels
    df["LLM Evaluation (Business Title)"] = title_labels

    desc_summary = build_summary(desc_labels, "Column Description (KATA similarity)")
    title_summary = build_summary(title_labels, "Business Title (KATA similarity)")
    summary = pd.concat([desc_summary, title_summary], ignore_index=True)

    # Extra diagnostic: how often did the resolver find ZERO candidates,
    # versus find candidates but reject all of them (both count as "fallback"
    # in the evaluation above, but they're different failure modes worth
    # distinguishing when deciding whether this resolver is viable).
    no_candidates = int((df["N Candidates"] == 0).sum())
    rejected_all = int(((df["N Candidates"] > 0) & (df["Selected KATA ID"] == "")).sum())
    logger.info(
        "Diagnostic: %d row(s) had zero candidates retrieved; %d row(s) had candidates but the LLM rejected all of them.",
        no_candidates, rejected_all,
    )

    logger.info("Evaluation summary:\n%s", summary.to_string(index=False))

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Results", index=False)
        summary.to_excel(writer, sheet_name="Summary", index=False)

    logger.info("Saved %d rows → %s", len(df), OUTPUT_FILE)


if __name__ == "__main__":
    main()
