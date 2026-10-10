"""
Scoped-only KATA-similarity resolver — full dataset evaluation.

Changes vs test_kata_dual_approach.py:
  - GLOBAL approach dropped entirely — scoped (dataset-first) approach only,
    since it was validated as clearly superior (90.77%/92.31% similar vs
    80.95%/78.57%, and much lower fallback: 5.80% vs 39.13%).
  - Runs on the FULL dataset (data_test_new.xlsx) instead of the 69-row
    kata_test_dataset.csv — that file only existed because it had a
    "Link KATA Dataset" ground truth column; that ground truth is no longer
    required (not every row has one), so we no longer need to restrict to
    rows that happen to have it. The dataset link found during search is
    still recorded in the output for historical/audit purposes — it's just
    not used as an evaluation metric anymore.
  - NEW: LLM keyword extraction (GPT OSS) at two points:
      1. Dataset search fallback — table names have underscores that hurt
         literal text search; if the raw table_name search finds nothing,
         retry with an LLM-cleaned keyword.
      2. Column-element rescue search — if the scoped candidate pool has no
         genuine match (LLM rejects all), extract a clean keyword from the
         raw column name (which also has underscores) and retry against the
         GLOBAL element index as a last resort, rather than giving up.
    This didn't exist before because kata_test_dataset.csv happened to carry
    a "Predicted Business Title" column we could lean on for search text;
    data_test_new.xlsx does not, so raw column names (with underscores) are
    now the only fallback signal — making keyword cleanup necessary.
  - NEW: Truthfulness check (True/False), evaluated by a model SEPARATE from
    the one that made the selection (selection = GPT OSS, truthfulness judge
    = GPT-4.1) — same "judge != generator" principle used throughout this
    project (see the GT-arbitration step). Checks whether the selection is
    genuinely supported by the column/table/candidate data, or stretches an
    unsupported connection (the CAR -> "Capital Adequacy Ratio" pattern).
  - Evaluator upgraded from gpt-4.1-mini to gpt-4.1 (same Azure endpoint/key,
    different deployment — set via TRUTHFULNESS_DEPLOYMENT / JUDGE_DEPLOYMENT
    below, or override with AZURE_OPENAI_DEPLOYMENT_FULL in .env).

Usage:
    python test_kata_scoped_full.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from mage_flow.clients import get_llm_client
from mage_flow.common import parse_llm_output
from mage_flow.kata import KATA_ELEMENT_BASE_URL, KataOpenSearchClient, normalize_alias_key
from evaluate_column_description import evaluate_all, build_summary  # noqa: E402

BASE_DIR = Path(__file__).parent
INPUT_FILE = BASE_DIR / "all data eval 6.xlsx"
OUTPUT_FILE = BASE_DIR / "evaluation" / "kata_scoped_full_results.xlsx"

TABLE_COL = "Tabel"
COLUMN_COL = "Kolom"
DATA_TYPE_COL = "Tipe Data"
GT_TITLE_COL = "gt business title"
GT_DESC_COL = "gt column description"
# Candidate header names for the pre-existing predicted business title column
# — present in "all data eval 6.xlsx" (unlike data_test_new.xlsx, which has
# no such column). Checked in order; first match wins. If none match, the
# script falls back to column-name-only queries (same as data_test_new.xlsx
# behavior) rather than erroring out, so this script still works on either
# input file.
PREDICTED_TITLE_COL_CANDIDATES = ["Predicted Business Title", "Predicted"]

# Set to None to run the whole file; set to an int for a quick smoke test.
SAMPLE_SIZE = None
RANDOM_SEED = 42

SELECTION_MAX_TOKENS = 900
KEYWORD_MAX_TOKENS = 150
TRUTHFULNESS_MAX_TOKENS = 250

# Judge deployment for description/title similarity AND truthfulness.
# "Link/API-nya sama, cuma ganti model" — same Azure endpoint+key, different
# deployment name. Override via AZURE_OPENAI_DEPLOYMENT_FULL in .env if your
# gpt-4.1 deployment has a different name than the default below.
JUDGE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT_FULL", "gpt-4.1")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Keyword extraction (GPT OSS) — cleans up underscore-laden raw identifiers
# (table or column names) into short, searchable Indonesian phrases, WITHOUT
# inventing a specific business meaning it isn't confident about (same
# anti-fabrication principle as the main generation prompts).
# ---------------------------------------------------------------------------

KEYWORD_SYSTEM_PROMPT = """\
You are helping build a search query for a metadata search engine.

TASK:
Given a raw technical identifier (a table or column name, often with
underscores and abbreviations, e.g. "eform_id", "0000_staging_raw_as4_ddyp3a"),
produce a short, clean, human-readable Indonesian search phrase capturing
its likely general meaning.

RULES:
- Split on underscores/digits and expand ONLY well-known, safe abbreviations
  (id -> ID, tgl -> tanggal, no/nomor -> nomor, cd/kode -> kode, dt -> tanggal,
  desc -> deskripsi, amt -> jumlah). If a token's meaning isn't confidently
  known, keep it as-is rather than guessing a specific expansion — do NOT
  invent a specific business meaning or domain (e.g. do not expand an
  unfamiliar 3-4 letter code into a specific named concept unless it is one
  of the safe abbreviations above).
- Keep it SHORT — a few words, not a sentence.
- Output plain Indonesian text only, no punctuation, no explanation.

OUTPUT FORMAT (STRICT JSON ONLY):
{"keyword": "<cleaned search phrase>"}
"""


def _extract_keyword(llm_client, raw_identifier: str) -> str:
    if not raw_identifier:
        return ""
    messages = [
        {"role": "system", "content": KEYWORD_SYSTEM_PROMPT},
        {"role": "user", "content": raw_identifier},
    ]
    try:
        raw = llm_client.create_response(
            messages=messages, sampling_params={"temperature": 0.1, "max_tokens": KEYWORD_MAX_TOKENS}
        )
        parsed = parse_llm_output(raw)
        return str(parsed.get("keyword") or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Keyword extraction failed for %r: %s", raw_identifier, exc)
        return ""


# ---------------------------------------------------------------------------
# Candidate-selection prompt (unchanged from test_kata_dual_approach.py)
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
    return str(candidate.get("id") or candidate.get("data_element_name") or "").strip()


def _select_candidate(llm_client, table_name: str, col_name: str, data_type: str, candidates: list[dict]) -> dict:
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


def _backfill_definition(kata_client: KataOpenSearchClient, candidate: dict) -> dict:
    """Dataset-embedded elements often carry a name but an EMPTY definition —
    look it up by exact name against the full data-element index."""
    if candidate.get("definition"):
        return candidate

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


# ---------------------------------------------------------------------------
# Truthfulness check — SEPARATE model (GPT-4.1) from the one that selected
# the candidate (GPT OSS). Judges whether the selection is genuinely
# supported, or stretches/invents an unsupported connection.
# ---------------------------------------------------------------------------

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
- Selected candidate aliases: {candidate_aliases}

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
only the table/column/data-type context actually provided.

OUTPUT FORMAT (STRICT JSON ONLY):
{{"truthful": true, "reasoning": "<one short sentence, in Indonesian>"}}
"""


async def _check_truthfulness_one(
    client, semaphore: asyncio.Semaphore, idx: int,
    table_name: str, col_name: str, data_type: str, candidate: dict | None,
) -> tuple[int, str, str]:
    """Returns (idx, 'true'|'false'|'n/a', reasoning)."""
    if not candidate:
        return idx, "n/a", "no candidate was selected"

    prompt = TRUTHFULNESS_SYSTEM_PROMPT.format(
        table_name=table_name, col_name=col_name, data_type=data_type or "(unknown)",
        candidate_name=candidate.get("data_element_name") or "",
        candidate_definition=candidate.get("definition") or "",
        candidate_aliases=", ".join(candidate.get("data_element_alias", []) or []),
    )
    messages = [{"role": "system", "content": prompt}, {"role": "user", "content": "Assess now."}]

    async with semaphore:
        for attempt in range(1, 4):
            try:
                response = await client.chat.completions.create(
                    model=JUDGE_DEPLOYMENT, messages=messages, temperature=0,
                    max_tokens=TRUTHFULNESS_MAX_TOKENS,
                )
                raw = response.choices[0].message.content.strip()
                cleaned = raw.replace("```json", "").replace("```", "").strip()
                parsed = json.loads(cleaned)
                truthful = bool(parsed.get("truthful"))
                reasoning = str(parsed.get("reasoning") or "").strip()
                return idx, ("true" if truthful else "false"), reasoning
            except Exception as exc:  # noqa: BLE001
                if attempt < 3:
                    await asyncio.sleep(2 * attempt)
                else:
                    logger.warning("Truthfulness check failed for row %d: %s", idx, exc)
                    return idx, "error", str(exc)
    return idx, "error", "unreachable"


def _find_predicted_title_col(df: pd.DataFrame) -> str | None:
    for name in PREDICTED_TITLE_COL_CANDIDATES:
        if name in df.columns:
            return name
    return None


def _build_query(row: pd.Series, predicted_title_col: str | None) -> str:
    """Prefer a grounded (non-'[AI]'-prefixed) predicted business title when
    the input file has one (e.g. "all data eval 6.xlsx") — same query logic
    used for kata_test_dataset.csv. Falls back to the raw column name when
    the title is missing/still an AI guess, or the input file (e.g.
    data_test_new.xlsx) doesn't have that column at all."""
    if predicted_title_col:
        title = str(row.get(predicted_title_col, "") or "").strip()
        if title and not title.startswith("[AI]"):
            return title
    return str(row.get(COLUMN_COL, "") or "").strip()


def main() -> None:
    if not INPUT_FILE.exists():
        logger.error("Input file not found: %s", INPUT_FILE)
        sys.exit(1)

    logger.info("Reading %s …", INPUT_FILE)
    df = pd.read_excel(INPUT_FILE)
    missing = [c for c in (TABLE_COL, COLUMN_COL, GT_TITLE_COL, GT_DESC_COL) if c not in df.columns]
    if missing:
        raise ValueError(f"{INPUT_FILE.name} missing columns: {missing}. Found: {list(df.columns)}")

    predicted_title_col = _find_predicted_title_col(df)
    if predicted_title_col:
        logger.info("Found predicted-title column %r — using it (when grounded) for rescue search queries.",
                    predicted_title_col)
        if predicted_title_col == "Predicted Business Title":
            # Our own output is also written to a column named "Predicted
            # Business Title" further down — without this rename, that
            # write would silently overwrite (not create a new column for)
            # this INPUT value, losing the old prediction from the output
            # entirely. Preserve it under a distinct name first.
            df = df.rename(columns={"Predicted Business Title": "Predicted Business Title (input, old flow)"})
            predicted_title_col = "Predicted Business Title (input, old flow)"
    else:
        logger.info("No predicted-title column found — rescue search will use raw column names only.")

    if SAMPLE_SIZE is not None and SAMPLE_SIZE < len(df):
        df = df.sample(n=SAMPLE_SIZE, random_state=RANDOM_SEED).reset_index(drop=True)
        logger.info("Sampled %d row(s) (SAMPLE_SIZE=%d).", len(df), SAMPLE_SIZE)
    else:
        logger.info("Running all %d row(s).", len(df))

    logger.info("Judge deployment (description/title similarity + truthfulness): %r", JUDGE_DEPLOYMENT)
    logger.info("Initializing KATA OpenSearch client and LLM client …")
    kata_client = KataOpenSearchClient()
    llm_client = get_llm_client()

    # Cache dataset search per table.
    dataset_cache: dict[str, dict] = {}

    def _get_dataset_info(table_name: str) -> dict:
        if table_name not in dataset_cache:
            try:
                datasets = kata_client.search_datasets(table_name, table_name=table_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("search_datasets failed for %s: %s", table_name, exc)
                datasets = []

            used_keyword_fallback = False
            search_query_used = table_name  # what actually found the dataset (for audit)
            chosen = next((ds for ds in datasets if ds.get("data_elements")), None)

            if not chosen:
                # Raw table_name search found nothing usable — try an
                # LLM-cleaned keyword as a fallback.
                keyword = _extract_keyword(llm_client, table_name)
                if keyword:
                    try:
                        datasets = kata_client.search_datasets(keyword, table_name=table_name)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("search_datasets (keyword fallback) failed for %s: %s", table_name, exc)
                        datasets = []
                    chosen = next((ds for ds in datasets if ds.get("data_elements")), None)
                    used_keyword_fallback = chosen is not None
                    if used_keyword_fallback:
                        search_query_used = keyword

            dataset_cache[table_name] = {
                "id": chosen.get("id", "") if chosen else "",
                "data_elements": chosen.get("data_elements", []) if chosen else [],
                "used_keyword_fallback": used_keyword_fallback,
                "search_query_used": search_query_used if chosen else "",
            }
        return dataset_cache[table_name]

    out = {
        "search_query_rescue": [], "dataset_used_id": [], "dataset_keyword_fallback": [],
        "dataset_search_query": [],
        "n_cand_scoped": [], "used_rescue": [], "n_cand_rescue": [],
        "sel_id": [], "sel_link": [], "desc": [], "title": [], "reasoning": [],
    }

    for _, row in tqdm(df.iterrows(), total=len(df), desc="Scoped KATA resolver (full)"):
        table_name, col_name = str(row[TABLE_COL]), str(row[COLUMN_COL])
        data_type = str(row.get(DATA_TYPE_COL, ""))

        dataset_info = _get_dataset_info(table_name)
        out["dataset_used_id"].append(dataset_info["id"])
        out["dataset_keyword_fallback"].append(dataset_info["used_keyword_fallback"])
        out["dataset_search_query"].append(dataset_info["search_query_used"])

        scoped_candidates = dataset_info["data_elements"]
        out["n_cand_scoped"].append(len(scoped_candidates))
        result = _select_candidate(llm_client, table_name, col_name, data_type, scoped_candidates)
        selected = result["selected"]
        reasoning = result["reasoning"]

        used_rescue = False
        n_cand_rescue = 0
        rescue_query = ""
        if not selected:
            # Scoped pool had no genuine match — rescue via global element
            # search. If we have a grounded predicted business title, it's
            # already clean human text — use it as-is. Only run the raw
            # column name (the underscore-laden case) through the LLM
            # keyword extractor.
            raw_query = _build_query(row, predicted_title_col)
            if raw_query == col_name:
                rescue_query = _extract_keyword(llm_client, raw_query) or raw_query
            else:
                rescue_query = raw_query
            try:
                rescue_candidates = kata_client.search_data_elements(rescue_query) if rescue_query else []
            except Exception as exc:  # noqa: BLE001
                logger.warning("Rescue search_data_elements failed for %r: %s", rescue_query, exc)
                rescue_candidates = []
            n_cand_rescue = len(rescue_candidates)
            if rescue_candidates:
                used_rescue = True
                rescue_result = _select_candidate(llm_client, table_name, col_name, data_type, rescue_candidates)
                selected = rescue_result["selected"]
                reasoning = rescue_result["reasoning"] or reasoning

        if selected:
            selected = _backfill_definition(kata_client, selected)

        out["search_query_rescue"].append(rescue_query)
        out["used_rescue"].append(used_rescue)
        out["n_cand_rescue"].append(n_cand_rescue)
        out["sel_id"].append(_display_key(selected) if selected else "")
        out["sel_link"].append(f"{KATA_ELEMENT_BASE_URL}/{selected['id']}" if selected and selected.get("id") else "")
        out["desc"].append(selected.get("definition") or "" if selected else "")
        out["title"].append(selected.get("data_element_name") or "" if selected else "")
        out["reasoning"].append(reasoning)

        # Stash the resolved candidate dict itself for the truthfulness pass below.
        dataset_cache.setdefault("__selected_candidates__", []).append(selected)

    df["Dataset Used ID"] = out["dataset_used_id"]
    df["Dataset Search Query"] = out["dataset_search_query"]
    df["Dataset Found via Keyword Fallback?"] = out["dataset_keyword_fallback"]
    df["N Candidates (scoped)"] = out["n_cand_scoped"]
    df["Used Rescue Search?"] = out["used_rescue"]
    df["Rescue Search Query"] = out["search_query_rescue"]
    df["N Candidates (rescue)"] = out["n_cand_rescue"]
    df["Selected ID"] = out["sel_id"]
    df["Selected KATA Link"] = out["sel_link"]
    df["Predicted Column Description"] = out["desc"]
    df["Predicted Business Title"] = out["title"]
    df["Selection Reasoning"] = out["reasoning"]

    logger.info("Evaluating description/title similarity against ground truth (judge=%s) …", JUDGE_DEPLOYMENT)

    async def _run_eval() -> tuple[list[str], list[str]]:
        return await asyncio.gather(
            evaluate_all(df, GT_DESC_COL, "Predicted Column Description",
                         "column descriptions", "column description", deployment=JUDGE_DEPLOYMENT),
            evaluate_all(df, GT_TITLE_COL, "Predicted Business Title",
                         "business titles", "business title", deployment=JUDGE_DEPLOYMENT),
        )

    desc_labels, title_labels = asyncio.run(_run_eval())
    df["Eval Column Description"] = desc_labels
    df["Eval Business Title"] = title_labels

    logger.info("Running truthfulness check (judge=%s, independent of the selecting model) …", JUDGE_DEPLOYMENT)

    from openai import AsyncAzureOpenAI
    import evaluate_column_description as ecd

    async def _run_truthfulness() -> tuple[list[str], list[str]]:
        client = AsyncAzureOpenAI(
            azure_endpoint=ecd.AZURE_ENDPOINT, api_key=ecd.AZURE_API_KEY, api_version=ecd.AZURE_API_VER,
        )
        semaphore = asyncio.Semaphore(ecd.CONCURRENCY)
        selected_candidates = dataset_cache.get("__selected_candidates__", [])
        tasks = [
            _check_truthfulness_one(
                client, semaphore, i,
                str(df.at[i, TABLE_COL]), str(df.at[i, COLUMN_COL]), str(df.at[i, DATA_TYPE_COL]),
                selected_candidates[i] if i < len(selected_candidates) else None,
            )
            for i in range(len(df))
        ]
        results = [""] * len(df)
        reasons = [""] * len(df)
        for coro in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Truthfulness check"):
            idx, label, reason = await coro
            results[idx] = label
            reasons[idx] = reason
        return results, reasons

    truthfulness_labels, truthfulness_reasons = asyncio.run(_run_truthfulness())
    df["Truthfulness"] = truthfulness_labels
    df["Truthfulness Reasoning"] = truthfulness_reasons

    desc_summary = build_summary(desc_labels, "Column Description (scoped)")
    title_summary = build_summary(title_labels, "Business Title (scoped)")

    truth_series = pd.Series(truthfulness_labels)
    truth_counts = truth_series.value_counts()
    truth_judged_total = int(truth_series.isin(["true", "false"]).sum())
    truth_rows = []
    for label in ("true", "false", "n/a", "error"):
        count = int(truth_counts.get(label, 0))
        denom = truth_judged_total if label in ("true", "false") else len(truth_series)
        denom = denom if denom > 0 else 1
        truth_rows.append({"Dimension": "Truthfulness", "Label": label, "Count": count,
                            "Percentage": f"{count / denom * 100:.2f}%"})
    truthfulness_summary = pd.DataFrame(truth_rows)

    summary = pd.concat([desc_summary, title_summary, truthfulness_summary], ignore_index=True)
    logger.info("Evaluation summary:\n%s", summary.to_string(index=False))

    rescue_used_count = int(df["Used Rescue Search?"].sum())
    keyword_fallback_count = int(df["Dataset Found via Keyword Fallback?"].sum())
    logger.info(
        "Diagnostics: dataset search needed keyword fallback for %d/%d table(s); "
        "rescue element search was used for %d/%d row(s).",
        keyword_fallback_count, len(dataset_cache) - 1 if dataset_cache else 0,
        rescue_used_count, len(df),
    )

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    def _write(path: Path) -> None:
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Results", index=False)
            summary.to_excel(writer, sheet_name="Summary", index=False)

    try:
        _write(OUTPUT_FILE)
        logger.info("Saved %d rows → %s", len(df), OUTPUT_FILE)
    except PermissionError:
        # Most likely OUTPUT_FILE is still open in Excel from a previous
        # inspection — a locked file must never cost us the whole run's
        # results after 10+ minutes of paid API calls. Fall back to a
        # timestamped path instead of losing everything.
        import datetime as _dt
        fallback = OUTPUT_FILE.with_name(
            f"{OUTPUT_FILE.stem}_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}{OUTPUT_FILE.suffix}"
        )
        logger.warning(
            "Could not write to %s (likely still open in Excel) — saving to %s instead. "
            "Close the original file and re-run if you specifically need that exact filename.",
            OUTPUT_FILE, fallback,
        )
        _write(fallback)
        logger.info("Saved %d rows → %s", len(df), fallback)


if __name__ == "__main__":
    main()