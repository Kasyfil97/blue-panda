"""KATAEvidenceSimilar resolver — dataset-scoped KATA element selection.

Turns the validated experiment (test_kata_scoped_full.py) into a proper
``BaseResolver`` so it can sit in the "KATA layer" next to the existing
``KataEvidenceResolver`` (the exact-alias / technical-relation matcher).

Flow per column
---------------
1. Find the KATA dataset linked to the table (search_datasets(table_name);
   cached per table). Its pre-curated ``data_elements`` are the candidate pool.
   If the raw table name finds nothing, retry once with an LLM-cleaned keyword.
2. LLM picks the best candidate for the column — or rejects them all.
3. If everything was rejected, RESCUE: LLM-clean the raw column name (drops
   underscores/abbreviation noise), search the global data-element index,
   and let the LLM select again.
4. Dataset-embedded elements often have an empty definition; backfill it via
   an exact-name lookup so the returned description is never empty.

Optional BM25 context
---------------------
With ``use_bm25_context=True`` the resolver also reads ``ctx.extra_knowledge``
(dictionary entries the AS400 BM25 resolver retrieved for this column, e.g.
"ddcod" -> "Currency code") and shows them to the LLM as supporting evidence.
The caller decides whether to set it; it is only ever populated for AS400
tables, mirroring the hierarchy's "if AS400 -> retrieve BM25 knowledge first".
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import kata as kata_backend
from .common import is_missing_desc, norm, parse_llm_output
from .resolvers import BaseResolver, BM25Resolver, ResolverContext, ResolverResult
from .source_priority import filter_by_priority

logger = logging.getLogger(__name__)

SELECTION_MAX_TOKENS = 2000
KEYWORD_MAX_TOKENS = 400
MAX_EVIDENCE_ITEMS = 5
MAX_EVIDENCE_CHARS = 300


# ---------------------------------------------------------------------------
# Prompts. KEYWORD_SYSTEM_PROMPT is sent raw (never .format()-ed), so it uses
# SINGLE braces; SELECTION_SYSTEM_PROMPT IS .format()-ed, so its literal braces
# are doubled. (Mixing these up made the keyword prompt show models a
# double-brace JSON example, which stricter models copied verbatim.)
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
- Supporting evidence (dictionary entries retrieved for this column; may be
  empty; NOT candidates, only context for interpreting the column):
{evidence_block}
- Candidates (may or may not be relevant):
{candidates_json}

REASONING STEPS (MANDATORY ORDER):
1. Infer the table's business domain from the table name (e.g. loan, deposit,
   customer master, transaction log, product/parameter table).
2. Interpret what the column likely represents within that domain, using the
   column name, the data type and — when present — the supporting evidence.
   Evidence can itself be a false friend (e.g. a generic "id" column matched
   to an unrelated table's id); trust it only if its domain fits this table.
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

# Enriched variant (error-analysis suggestion #2/#3): same task, but the model
# also sees the table's system context, table term knowledge, the column's
# abbreviation expansion and its sibling columns, plus explicit specificity,
# generic-name and data-type compatibility rules. .format()-ed -> double braces.
ENRICHED_SELECTION_SYSTEM_PROMPT = """\
You are a banking data steward selecting the correct KATA data element for a
database column, from a set of retrieved candidates. This choice becomes
official metadata, so accuracy matters more than always picking something.

INTERNAL REASONING REQUIREMENT:
- Reason internally, in the order below.
- Do NOT reveal reasoning in the output — only the final JSON.

INPUTS:
- Table name: {table_name}
- Source system context (may be empty):
{system_context}
- Table term knowledge (codes found in the TABLE name; describes the whole
  table's domain, may be empty):
{table_terms}
- Column name (technical): {col_name}
- Column data type: {data_type}
- Abbreviation expansion of column-name tokens (dictionary lookup; may contain
  irrelevant partial matches — use only tokens that fit the table):
{abbr_terms}
- Other columns in the same table (name: data type):
{sibling_columns}
- Supporting evidence (dictionary entries retrieved for this column; may be
  empty; NOT candidates, only context):
{evidence_block}
- Steward-approved metadata of similarly-named columns in OTHER tables (shows
  naming conventions and what abbreviations meant elsewhere; NOT candidates,
  may be from a different domain):
{approved_examples}
- Candidates (may or may not be relevant):
{candidates_json}

REASONING STEPS (MANDATORY ORDER):
0. Use the approved examples only to interpret abbreviations and naming
   conventions (e.g. "...dat" = a date, "userid_ao" = the AO's personal
   number). The selection must still be one of the candidates and must fit
   THIS table.
1. Infer the table's business domain from the table name, system context,
   table term knowledge AND the sibling columns (e.g. a table that has
   carcod + cardsc + lnparr columns is about loan collectibility, not cards;
   a table named tbl_kodepos with kelurahan/kecamatan columns is a postal
   reference table).
2. Interpret the column within that domain. Split its name into tokens and
   explain EVERY token (e.g. "acctbranch" = account + branch -> the branch
   where the account was opened; "tramt" = transaction amount; "branch_pemasar"
   = branch of the marketer, not the marketer's id). Sibling columns often
   reveal what a token means (a "...cod" / "...dsc" pair = code + description
   of the same concept).
3. GENERIC column names (id, name, code, status, amount, branch, type, date,
   business, ...): their meaning comes ONLY from the table. Prefer a candidate
   tied to the table's own entity (e.g. "status" in a transaction table =
   transaction status; "name" in a merchant table = merchant name). If the
   table is not about the entity a candidate names (e.g. "Nama Pembeli" for a
   table whose rows are merchants), reject that candidate.
4. DATA TYPE COMPATIBILITY: the candidate must be able to hold this data type.
   - int / bigint / smallint / tinyint / boolean columns cannot be free text
     (Nama, Alamat, Deskripsi, Keterangan); an int column named like a text
     concept (e.g. address_main) is usually a FLAG or an ID.
   - a string column holding structured data (json, config, override payload)
     is not a single flag/code.
   - timestamp / date columns must map to a date/time concept, and its
     meaning must match the name (updated_at = last data change, NOT ingestion
     or load date).
   - "amount" / "jumlah" can be a money value OR a count; use the table and
     siblings to decide.
5. SPECIFICITY: among candidates that fit, choose the one that covers the
   MOST tokens / qualifiers of the column and the table's domain. Reject a
   generic candidate (e.g. "Kode Kantor Cabang", "Kurs", "Kode Status") when
   the column clearly carries an extra qualifier the candidate lacks
   (pembukaan rekening, spread, transaksi) — unless no more specific
   candidate exists, in which case still pick it but lower your confidence.
6. If NONE of the candidates is a genuine match, reject all of them. Picking
   nothing is the correct, expected outcome for many columns.

OUTPUT FORMAT (STRICT JSON ONLY):
{{
  "selected_id": "<candidate id, or empty string if none genuinely match>",
  "confidence": <number 0..1 — how sure you are the selected element is exactly this column's meaning>,
  "reasoning": "<one short sentence, in Indonesian, explaining the decision>",
  "alternative_ids": ["<id>", "..."]
}}
"""

MAX_SYSTEM_CONTEXT_CHARS = 1200
MAX_SIBLING_COLUMNS = 40


# ---------------------------------------------------------------------------
# Data-type compatibility pre-filter (error-analysis suggestion #3)
#
# KATA elements carry no usable data type (format_type is always empty), so a
# candidate's value kind is inferred from the FIRST word(s) of its name. Only
# clear-cut conflicts are rejected; anything ambiguous passes through to the
# LLM. Dates stored as numbers (AS400 decimal YYYYMMDD) stay allowed.
# ---------------------------------------------------------------------------

_NUMERIC_TYPES = frozenset({"int", "integer", "bigint", "smallint", "tinyint", "decimal", "numeric",
                            "double", "float", "real", "number", "boolean", "bool"})
_TEMPORAL_TYPES = frozenset({"timestamp", "datetime", "date", "time"})
_FREE_TEXT_PREFIXES = ("nama", "alamat", "deskripsi", "keterangan", "catatan", "uraian", "email", "judul", "teks")
_NON_TEMPORAL_PREFIXES = _FREE_TEXT_PREFIXES + (
    "jumlah", "nominal", "saldo", "total", "nilai", "kode", "nomor", "no ", "id ", "flag", "status", "persentase",
)


def _base_type(data_type: str) -> str:
    """'decimal(18,2)' -> 'decimal'."""
    return norm(data_type).lower().split("(")[0].strip()


def type_conflict(data_type: str, candidate: Dict[str, Any]) -> str:
    """Reason string when the candidate cannot hold this column's data type, else ''."""
    base = _base_type(data_type)
    name = norm(candidate.get("data_element_name")).lower() + " "
    if base in _NUMERIC_TYPES and name.startswith(_FREE_TEXT_PREFIXES):
        return f"free-text element for {base} column"
    if base in _TEMPORAL_TYPES and name.startswith(_NON_TEMPORAL_PREFIXES):
        return f"non-temporal element for {base} column"
    return ""


def filter_by_data_type(data_type: str, candidates: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Split candidates into (compatible, rejected element names)."""
    kept: List[Dict[str, Any]] = []
    rejected: List[str] = []
    for cand in candidates or []:
        if type_conflict(data_type, cand):
            rejected.append(norm(cand.get("data_element_name")))
        else:
            kept.append(cand)
    return kept, rejected


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def extract_keyword(llm_client: Any, raw_identifier: str) -> str:
    """LLM-clean an underscore-laden identifier into a short search phrase."""
    if not norm(raw_identifier):
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
        return norm(parsed.get("keyword")) if isinstance(parsed, dict) else ""
    except Exception as exc:  # noqa: BLE001 - keyword cleanup is best-effort
        logger.warning("[kata_similar] keyword extraction failed for %r: %s", raw_identifier, exc)
        return ""


def format_evidence(items: Optional[List[Dict[str, Any]]]) -> str:
    """Render retrieved BM25 dictionary entries as short evidence lines."""
    lines: List[str] = []
    for item in (items or [])[:MAX_EVIDENCE_ITEMS]:
        if not isinstance(item, dict):
            continue
        desc = norm(item.get("column_description") or item.get("description"))
        if not desc or is_missing_desc(desc):
            continue
        table = norm(item.get("table_name"))
        column = norm(item.get("column_name"))
        source = norm(item.get("source_type"))
        lines.append(f"- {table}.{column} [{source}]: {desc[:MAX_EVIDENCE_CHARS]}")
    return "\n".join(lines)


def format_term_entries(entries: Optional[List[Dict[str, Any]]]) -> str:
    """Render [{code: meaning}, ...] term lookups as '- code: meaning' lines,
    dropping empty meanings (partial-match noise such as {'a': ''})."""
    lines: List[str] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        for code, meaning in entry.items():
            if norm(code) and norm(meaning):
                lines.append(f"- {norm(code)}: {norm(meaning)}")
    return "\n".join(lines)


def format_sibling_columns(columns: Optional[List[Dict[str, Any]]], exclude: str) -> str:
    lines: List[str] = []
    for col in (columns or [])[:MAX_SIBLING_COLUMNS]:
        name = norm(col.get("ColumnName")) if isinstance(col, dict) else ""
        if not name or name.lower() == norm(exclude).lower():
            continue
        lines.append(f"- {name}: {norm(col.get('ColumnDataType')) or '?'}")
    return "\n".join(lines)


def retrieve_bm25_knowledge(as400_resolver: BM25Resolver, ctx: ResolverContext) -> List[Dict[str, Any]]:
    """Retrieval half of ``BM25Resolver.resolve`` — NO LLM synthesis.

    Mirrors the hierarchy's "if the table comes from AS400, retrieve its
    dictionary knowledge first": the resolver's own ``run_if`` predicate
    (``_is_as400_table``) decides applicability, so non-AS400 tables get [].
    Same search order as the resolver: table-filtered, then global fallback.
    """
    if as400_resolver.run_if is not None and not as400_resolver.run_if(ctx):
        return []
    term_cfg = ctx.bm25_params.get("term", {})
    global_cfg = ctx.bm25_params.get("global", {})
    items = filter_by_priority(as400_resolver._table_filtered_search(ctx, term_cfg), as400_resolver.allowed_priorities)
    if not items:
        items = filter_by_priority(as400_resolver._global_search(ctx, global_cfg), as400_resolver.allowed_priorities)
    return list(items or [])


def _display_key(candidate: Dict[str, Any]) -> str:
    """Handle the LLM uses to refer to a candidate. Dataset-embedded elements
    carry no id/doc_id (only a name), so fall back to the name — unique enough
    inside one dataset's small element list."""
    return norm(candidate.get("id") or candidate.get("data_element_name"))


# ---------------------------------------------------------------------------
# Dataset pool (per-table cache)
# ---------------------------------------------------------------------------

class KataDatasetPool:
    """Caches ``search_datasets`` per table; shared between resolver variants."""

    def __init__(self, kata_client: Optional[kata_backend.KataOpenSearchClient] = None) -> None:
        self._client = kata_client
        self._lock = threading.Lock()
        self._cache: Dict[str, Dict[str, Any]] = {}

    def client(self) -> kata_backend.KataOpenSearchClient:
        with self._lock:
            if self._client is None:
                self._client = kata_backend.KataOpenSearchClient()
            return self._client

    def get(self, table_name: str, extract_keyword_fn: Optional[Callable[[str], str]] = None) -> Dict[str, Any]:
        with self._lock:
            cached = self._cache.get(table_name)
        if cached is not None:
            return cached
        info = self._lookup(table_name, extract_keyword_fn)
        with self._lock:
            self._cache[table_name] = info
        return info

    def _lookup(self, table_name: str, extract_keyword_fn: Optional[Callable[[str], str]]) -> Dict[str, Any]:
        client = self.client()
        try:
            datasets = client.search_datasets(table_name, table_name=table_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[kata_similar] search_datasets failed for %s: %s", table_name, exc)
            datasets = []
        chosen = next((d for d in datasets if d.get("data_elements")), None)
        via_keyword = ""
        if chosen is None and extract_keyword_fn is not None:
            keyword = extract_keyword_fn(table_name)
            if keyword:
                try:
                    datasets = client.search_datasets(keyword, table_name=table_name)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[kata_similar] keyword dataset search failed for %s: %s", table_name, exc)
                    datasets = []
                chosen = next((d for d in datasets if d.get("data_elements")), None)
                if chosen is not None:
                    via_keyword = keyword
        return {
            "id": norm(chosen.get("id")) if chosen else "",
            "name": norm(chosen.get("data_name")) if chosen else "",
            "data_elements": list(chosen.get("data_elements") or []) if chosen else [],
            "via_keyword": via_keyword,
        }


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------

class KataSimilarResolver(BaseResolver):
    def __init__(
        self,
        *,
        pool: Optional[KataDatasetPool] = None,
        kata_client: Optional[kata_backend.KataOpenSearchClient] = None,
        use_bm25_context: bool = False,
        enable_rescue: bool = True,
        enriched_context: bool = False,
        type_filter: bool = False,
        examples_provider: Optional[Callable[[ResolverContext], str]] = None,
        evidence_resolver: Optional[BM25Resolver] = None,
    ) -> None:
        self._pool = pool or KataDatasetPool(kata_client)
        self.use_bm25_context = use_bm25_context
        self.enable_rescue = enable_rescue
        #: Use ENRICHED_SELECTION_SYSTEM_PROMPT (system context, table terms,
        #: abbreviations, sibling columns, specificity/data-type rules) and a
        #: table-aware rescue query for generic column names.
        self.enriched_context = enriched_context
        #: Drop candidates whose value kind conflicts with the column data type
        #: (filter_by_data_type) BEFORE the LLM sees them, for both pools.
        self.type_filter = type_filter
        #: Optional ``ctx -> str`` returning formatted steward-approved examples
        #: (see approved_examples.py); only used with enriched_context.
        self.examples_provider = examples_provider
        #: AS400 BM25Resolver used to fetch ctx.extra_knowledge when the caller
        #: did not pre-fill it (the production flow); its run_if limits it to AS400 tables.
        self.evidence_resolver = evidence_resolver

    @property
    def name(self) -> str:
        return "kata_similar"

    # -- LLM selection ----------------------------------------------------

    def _select(
        self, llm_client: Any, ctx: ResolverContext, data_type: str,
        candidates: List[Dict[str, Any]], evidence_text: str,
        enriched_fields: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        if not candidates:
            return {"selected": None, "reasoning": "no candidates", "confidence": None}
        candidates_json = json.dumps(
            [{"id": _display_key(c), "data_element_name": c.get("data_element_name"),
              "definition": c.get("definition"), "aliases": c.get("data_element_alias", [])}
             for c in candidates],
            ensure_ascii=False, indent=2,
        )
        if enriched_fields is not None:
            prompt = ENRICHED_SELECTION_SYSTEM_PROMPT.format(
                table_name=ctx.table_name, col_name=ctx.col_name, data_type=data_type or "(unknown)",
                evidence_block=evidence_text or "(none)", candidates_json=candidates_json, **enriched_fields,
            )
        else:
            prompt = SELECTION_SYSTEM_PROMPT.format(
                table_name=ctx.table_name, col_name=ctx.col_name, data_type=data_type or "(unknown)",
                evidence_block=evidence_text or "(none)", candidates_json=candidates_json,
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
            if not isinstance(parsed, dict):
                raise ValueError("selection output is not a JSON object")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[kata_similar] selection failed for %s.%s: %s", ctx.table_name, ctx.col_name, exc)
            return {"selected": None, "reasoning": f"LLM error: {exc}", "confidence": None}
        by_key = {_display_key(c): c for c in candidates}
        try:
            confidence = float(parsed.get("confidence")) if parsed.get("confidence") is not None else None
        except (TypeError, ValueError):
            confidence = None
        return {
            "selected": by_key.get(norm(parsed.get("selected_id"))),
            "reasoning": norm(parsed.get("reasoning")),
            "confidence": confidence,
        }

    def _enriched_fields(self, ctx: ResolverContext) -> Dict[str, str]:
        try:
            abbr = ctx.bm25.get_abbreviation_context(ctx.col_name, request_id=ctx.request_id)
        except Exception as exc:  # noqa: BLE001 - context is best-effort
            logger.warning("[kata_similar] abbreviation lookup failed for %s: %s", ctx.col_name, exc)
            abbr = []
        return {
            "system_context": norm(ctx.system_context)[:MAX_SYSTEM_CONTEXT_CHARS] or "(none)",
            "table_terms": format_term_entries(ctx.table_term_context) or "(none)",
            "abbr_terms": format_term_entries(abbr) or "(none)",
            "sibling_columns": format_sibling_columns(ctx.table_columns, ctx.col_name) or "(none)",
            "approved_examples": self._approved_examples(ctx) or "(none)",
        }

    def _approved_examples(self, ctx: ResolverContext) -> str:
        if self.examples_provider is None:
            return ""
        try:
            return self.examples_provider(ctx)
        except Exception as exc:  # noqa: BLE001 - examples are best-effort context
            logger.warning("[kata_similar] approved examples failed for %s.%s: %s", ctx.table_name, ctx.col_name, exc)
            return ""

    def _rescue_query(self, llm_client: Any, ctx: ResolverContext, dataset_name: str) -> str:
        keyword = extract_keyword(llm_client, ctx.col_name) or ctx.col_name
        # A generic column name ("code", "name", "status") searched alone returns
        # generic elements; anchor it to the table's entity instead.
        if self.enriched_context and norm(ctx.col_name).lower() in kata_backend.GENERIC_COLUMN_NAMES | {
            "amount", "branch", "business", "nik",
        }:
            table_keyword = dataset_name or extract_keyword(llm_client, ctx.table_name)
            if table_keyword:
                keyword = f"{keyword} {table_keyword}"
        return keyword

    def _backfill(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        """Recover a missing definition (and a real id) by exact-name lookup."""
        if norm(candidate.get("definition")):
            return candidate
        name = norm(candidate.get("data_element_name"))
        if not name:
            return candidate
        try:
            matches = self._pool.client().search_data_elements(name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[kata_similar] backfill search failed for %r: %s", name, exc)
            return candidate
        target = kata_backend.normalize_alias_key(name)
        full = next((m for m in matches if kata_backend.normalize_alias_key(m.get("data_element_name")) == target), None)
        full = full or (matches[0] if matches else None)
        if not full:
            return candidate
        merged = dict(candidate)
        merged["definition"] = full.get("definition") or merged.get("definition") or ""
        merged["id"] = full.get("id") or merged.get("id")
        return merged

    # -- resolve ------------------------------------------------------------

    def _evidence(self, ctx: ResolverContext) -> List[Dict[str, Any]]:
        if not self.use_bm25_context:
            return []
        if not ctx.extra_knowledge and self.evidence_resolver is not None:
            try:
                ctx.extra_knowledge = retrieve_bm25_knowledge(self.evidence_resolver, ctx)
            except Exception as exc:  # noqa: BLE001 - evidence is best-effort context
                logger.warning("[kata_similar] BM25 evidence retrieval failed for %s.%s: %s", ctx.table_name, ctx.col_name, exc)
        return list(ctx.extra_knowledge or [])

    def resolve(self, ctx: ResolverContext) -> ResolverResult:
        if not ctx.settings.get("kata_similar", {}).get("enabled", True):
            logger.info("[kata_similar] disabled in settings -> skip")
            return ResolverResult(False, self.name)
        llm_client = ctx.llm.client
        data_type = norm(ctx.data_type)
        evidence_items = self._evidence(ctx)
        evidence_text = format_evidence(evidence_items)
        enriched_fields = self._enriched_fields(ctx) if self.enriched_context else None

        info = self._pool.get(ctx.table_name, extract_keyword_fn=lambda raw: extract_keyword(llm_client, raw))
        scoped = info["data_elements"]
        logger.info(
            "[kata_similar] %s.%s: dataset=%s (%s) candidates=%d evidence_items=%d",
            ctx.table_name, ctx.col_name, info["id"] or "-", info["name"] or "not found",
            len(scoped), len(evidence_items),
        )

        type_rejected: List[str] = []
        if self.type_filter:
            scoped, type_rejected = filter_by_data_type(data_type, scoped)
            if type_rejected:
                logger.info("[kata_similar] %s.%s: type filter (%s) dropped %s",
                            ctx.table_name, ctx.col_name, data_type, type_rejected)

        tag = "kata_similar"
        rescue_query = ""
        n_candidates = len(scoped)
        selection = self._select(llm_client, ctx, data_type, scoped, evidence_text, enriched_fields)

        if selection["selected"] is None and self.enable_rescue:
            rescue_query = self._rescue_query(llm_client, ctx, info["name"])
            try:
                rescue_candidates = self._pool.client().search_data_elements(rescue_query)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[kata_similar] rescue search failed for %r: %s", rescue_query, exc)
                rescue_candidates = []
            if self.type_filter:
                rescue_candidates, dropped = filter_by_data_type(data_type, rescue_candidates)
                if dropped:
                    logger.info("[kata_similar] %s.%s: type filter (%s) dropped rescue %s",
                                ctx.table_name, ctx.col_name, data_type, dropped)
                type_rejected += dropped
            if rescue_candidates:
                n_candidates = len(rescue_candidates)
                rescue_selection = self._select(
                    llm_client, ctx, data_type, rescue_candidates, evidence_text, enriched_fields,
                )
                if rescue_selection["selected"] is not None:
                    selection = rescue_selection
                    tag = "kata_similar_rescue"

        selected = selection["selected"]
        if selected is None:
            logger.info("[kata_similar] %s.%s: no candidate accepted -> skip", ctx.table_name, ctx.col_name)
            return ResolverResult(False, self.name)

        selected = self._backfill(selected)
        definition = norm(selected.get("definition"))
        element_name = norm(selected.get("data_element_name"))
        if not definition or is_missing_desc(definition) or not element_name:
            logger.info("[kata_similar] %s.%s: accepted candidate has no usable definition -> skip", ctx.table_name, ctx.col_name)
            return ResolverResult(False, self.name)

        element_id = norm(selected.get("id"))
        # A real id only exists for global/rescue hits or after backfill; a
        # dataset-embedded element that fell back to its NAME as key has none.
        real_id = element_id if element_id and element_id != element_name else ""
        knowledge = [{
            "table_name": ctx.table_name, "column_name": ctx.col_name,
            "column_description": definition, "source_schema": "KATA", "source_type": "kata",
            "data_element_id": real_id or None, "data_element_name": element_name,
            # flow._generate_business_title reuses this instead of an extra LLM call.
            "business_title": element_name,
            "evidence_url": f"{kata_backend.KATA_ELEMENT_BASE_URL}/{real_id}" if real_id else None,
            "match_reason": selection["reasoning"] or "dataset-scoped similarity",
            "final_score": None,
            "kata_similar": {
                "dataset_id": info["id"], "dataset_name": info["name"],
                "dataset_via_keyword": info["via_keyword"], "n_candidates": n_candidates,
                "rescue_query": rescue_query, "used_bm25_context": bool(evidence_text),
                "bm25_evidence": evidence_text,
                "enriched_context": self.enriched_context, "confidence": selection.get("confidence"),
                "type_filter": self.type_filter, "type_rejected": type_rejected,
            },
        }]
        logger.info("[kata_similar] RESOLVED %s.%s -> %r (tag=%s)", ctx.table_name, ctx.col_name, element_name, tag)
        return ResolverResult(True, tag, description=definition, knowledge=knowledge)


__all__ = [
    "KataDatasetPool", "KataSimilarResolver", "extract_keyword", "filter_by_data_type", "format_evidence",
    "format_sibling_columns", "format_term_entries", "retrieve_bm25_knowledge", "type_conflict",
]
