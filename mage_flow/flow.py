"""Orchestrator — the table-level and single-column generation flow.

Synchronous port of src/services/metadata_generation_service.py
(generate_metadata_async + process_column_async + helpers) and
src/core/table/validator.py (validate_table / metadata_validation).

Public entry points:
  - generate_metadata(table, force_generate=..., settings=...)  -> full table
  - generate_column(table_name, col_name, settings=...)         -> single column
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from . import config
from .clients import BM25Client, get_llm_client
from .common import ai_prefix, is_missing_desc, norm
from .confluence import ConfluenceDiscoveryRequest, ConfluenceDiscoveryResponse, ConfluenceDiscoveryService
from .llm_generation import LLMGeneration
from .resolvers import ResolverContext, build_resolver_chain

logger = logging.getLogger(__name__)

_RESOLVER_CHAIN = build_resolver_chain()


# ===========================================================================
# Table validation (port of src/core/table/validator.py)
# ===========================================================================

def _dedupe_columns(columns: Any) -> List[Dict[str, Any]]:
    if not isinstance(columns, list):
        return []
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for col in columns:
        if not isinstance(col, dict):
            continue
        key = norm(col.get("ColumnName")).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(col)
    return out


def validate_table(table: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if table == {}:
        return None, "table kosong"
    if (not table) or ("TableName" not in table) or (table.get("TableName") in ("", None, "none")):
        return None, "tidak ada nama table"
    if "Columns" not in table or not table.get("Columns"):
        return None, "tidak ada kolom yang ditemukan"
    table["Columns"] = _dedupe_columns(table["Columns"])
    if not table["Columns"]:
        return None, "tidak ada kolom valid yang ditemukan"
    return table, None


def metadata_validation(table: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not table or not table.get("Columns"):
        return None, "table or columns is None"
    if all(val.get("ColumnDescription") not in (None, "", "None") for val in table["Columns"]):
        return None, "semua kolom telah memiliki deskripsi"
    return table, None


# ===========================================================================
# Formatting helpers (port of metadata_generation_service helpers)
# ===========================================================================

def _format_col_knowledge(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    shaped: List[Dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        shaped.append({
            "TableName": item.get("table_name"),
            "ColumnName": item.get("column_name"),
            "ColumnDescription": item.get("column_description"),
            "SourceSchema": item.get("source_schema"),
            "SourceType": item.get("source_type"),
            "PageId": item.get("page_id"),
            "Score": item.get("final_score", item.get("bm25_score")),
            "DataElementId": item.get("data_element_id"),
            "DataElementName": item.get("data_element_name"),
            "EvidenceUrl": item.get("evidence_url"),
            "MatchReason": item.get("match_reason"),
        })
    return shaped


def _format_table_knowledge(items: List[Dict[str, Any]], limit: int = 5) -> List[Dict[str, Any]]:
    shaped: List[Dict[str, Any]] = []
    for item in (items or [])[:limit]:
        if not isinstance(item, dict):
            continue
        shaped.append({
            "TableName": item.get("table_name"),
            "SourceSchema": item.get("source_schema"),
            "SourceType": item.get("source_type"),
            "PageId": item.get("page_id"),
            "Score": item.get("final_score", item.get("bm25_score")),
            "Document": item.get("document"),
        })
    return shaped


def _compact_columns_for_table_description(columns: List[Dict[str, Any]], limit: int = 40) -> List[Dict[str, Any]]:
    compacted: List[Dict[str, Any]] = []
    for column in (columns or [])[:limit]:
        if not isinstance(column, dict):
            continue
        name = norm(column.get("ColumnName"))
        if not name:
            continue
        item: Dict[str, Any] = {"ColumnName": name}
        data_type = norm(column.get("ColumnDataType"))
        business_title = norm(column.get("ColumnBusinessTitle"))
        description = norm(column.get("ColumnDescription"))
        if data_type:
            item["ColumnDataType"] = data_type
        if business_title:
            item["ColumnBusinessTitle"] = business_title
        if description and not is_missing_desc(description):
            item["ColumnDescription"] = description
        compacted.append(item)
    return compacted


def _score_value(item: Dict[str, Any]) -> float:
    try:
        return float(item.get("final_score") or item.get("bm25_score") or item.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _max_table_score(table_hits: List[Dict[str, Any]]) -> float:
    return max((_score_value(i) for i in table_hits if isinstance(i, dict)), default=0.0)


# ===========================================================================
# Confluence fallback orchestration
# ===========================================================================

def _empty_confluence_fallback_summary(enabled: bool) -> Dict[str, Any]:
    return {
        "enabled": enabled,
        "attempted": False,
        "status": "disabled" if not enabled else "skipped",
        "candidate_count": 0,
        "usable_candidates": 0,
        "used_fields": 0,
        "matched_columns": [],
        "error": None,
    }


def _should_attempt_confluence_fallback(
    *, settings: Dict[str, Any], table_hits: List[Dict[str, Any]], columns: List[Dict[str, Any]]
) -> bool:
    cfg = settings.get("confluence_fallback", {})
    if not cfg.get("enabled", False):
        return False
    missing = [c for c in columns if is_missing_desc(c.get("ColumnDescription"))]
    if not missing:
        return False
    return _max_table_score(table_hits) < float(cfg.get("min_table_score", 8.0))


def _temporary_knowledge_from_confluence(
    *, table_name: str, response: ConfluenceDiscoveryResponse, min_confidence: float,
    allowed_labels: Optional[List[str]] = None,
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Any]]:
    labels = {lbl.strip() for lbl in (allowed_labels or ["usable_metadata"]) if lbl}
    by_column: Dict[str, List[Dict[str, Any]]] = {}
    usable_candidates = 0
    for candidate in response.candidates:
        if candidate.label not in labels:
            continue
        if float(candidate.confidence or 0.0) < min_confidence:
            continue
        fields = candidate.extracted_fields or []
        if not fields:
            continue
        usable_candidates += 1
        for field in fields:
            field_name = norm(field.get("field_name"))
            description = norm(field.get("description"))
            if not field_name or not description or is_missing_desc(description):
                continue
            key = field_name.lower()
            item = {
                "table_name": table_name, "column_name": field_name, "column_description": description,
                "source_schema": candidate.source_schema, "source_type": "confluence",
                "page_id": candidate.page_id, "final_score": candidate.score,
                "data_type": norm(field.get("data_type")), "evidence_candidate_id": candidate.candidate_id,
                "evidence_title": candidate.title, "evidence_confidence": candidate.confidence,
            }
            existing = by_column.get(key, [])
            if existing and _score_value(existing[0]) >= _score_value(item):
                existing.append(item)
            else:
                by_column[key] = [item, *existing]
    stats = {
        "candidate_count": len(response.candidates),
        "usable_candidates": usable_candidates,
        "used_fields": sum(len(v) for v in by_column.values()),
        "matched_columns": sorted(by_column.keys()),
    }
    return by_column, stats


# ===========================================================================
# Business title + table description
# ===========================================================================

def _generate_business_title(
    *, table_name: str, col_name: str, col_description: str, system_context: str,
    knowledge: Optional[List[Dict[str, Any]]], llm: LLMGeneration, sampling_params: Optional[Dict[str, Any]],
) -> str:
    if knowledge:
        for item in knowledge:
            bt = norm(item.get("business_title"))
            if bt:
                return bt
    result = llm.col_business_title_generate(
        table_name=table_name, col_name=col_name, col_description=col_description,
        system_context=system_context, sampling_params=sampling_params,
    )
    return norm(result.get("ColumnBusinessTitle")) or ""


def _generate_table_description(
    *, table_name: str, system_context: str, table_hits: List[Dict[str, Any]],
    columns: List[Dict[str, Any]], llm: LLMGeneration, sampling_params: Optional[Dict[str, Any]],
) -> str:
    out = llm.table_desc_generate(
        table_name=table_name, system_context=system_context,
        table_knowledge=_format_table_knowledge(table_hits),
        columns=_compact_columns_for_table_description(columns), 
        sampling_params=sampling_params,
    )
    return ai_prefix(out.get("TableDescription"))


# ===========================================================================
# Auto-approve eligibility (port)
# ===========================================================================

def _check_auto_approve_eligible(
    outcomes: List[Dict[str, Any]], columns: List[Dict[str, Any]], required_source_type: str = "confluence"
) -> bool:
    if not outcomes or not columns:
        return False
    if any(o.get("status") == "error" for o in outcomes):
        return False
    active = [o.get("resolution") for o in outcomes if o.get("resolution") != "skipped"]
    if not active or not all(r == "exact_lookup" for r in active):
        return False
    required = required_source_type.lower()
    for col in columns:
        if is_missing_desc(col.get("ColumnDescription")):
            return False
        knowledge_list = col.get("Knowledge", [])
        if not knowledge_list:
            return False
        for k in knowledge_list:
            if str(k.get("SourceType") or "").strip().lower() != required:
                return False
    return True


# ===========================================================================
# Column processing (port of process_column_async)
# ===========================================================================

def process_column(
    *, table_name: str, column: Dict[str, Any], system_context: str, table_hits: List[Dict[str, Any]],
    bm25: BM25Client, llm: LLMGeneration, sampling_params: Dict[str, Any], bm25_params: Dict[str, Any],
    settings: Dict[str, Any], force_generate: bool, request_id: Optional[str] = None,
    bt_enabled: bool = False, bt_sampling_params: Optional[Dict[str, Any]] = None,
    force_bt: bool = False,
    temporary_knowledge_by_column: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    table_term_context: Optional[List[Dict[str, str]]] = None,
) -> str:
    """Process a SINGLE column via the resolver chain. Mutates ``column`` in place."""
    col_name = norm(column.get("ColumnName"))
    if not col_name:
        return "skipped"

    if not force_generate and not is_missing_desc(column.get("ColumnDescription")):
        logger.info("[chain] %s: description already present -> skip resolver chain", col_name)
        # Description exists — generate a business title if enabled and missing (or force_bt).
        if bt_enabled and (force_bt or is_missing_desc(column.get("ColumnBusinessTitle"))):
            try:
                bt = _generate_business_title(
                    table_name=table_name, col_name=col_name,
                    col_description=norm(column.get("ColumnDescription")), system_context=system_context,
                    knowledge=None, llm=llm, sampling_params=bt_sampling_params,
                )
                if bt:
                    column["ColumnBusinessTitle"] = ai_prefix(bt)
                    return "bt_only"
            except Exception as exc:
                logger.warning("business_title_failed %s.%s: %s", table_name, col_name, exc)
        return "skipped"

    ctx = ResolverContext(
        table_name=table_name, col_name=col_name, system_context=system_context, table_hits=table_hits,
        bm25=bm25, llm=llm, sampling_params=sampling_params, bm25_params=bm25_params, settings=settings,
        request_id=request_id, temporary_knowledge_by_column=temporary_knowledge_by_column or {},
        table_term_context=table_term_context or [],
    )

    logger.info("[chain] %s: start resolver chain (force=%s)", col_name, force_generate)
    for resolver in _RESOLVER_CHAIN:
        result = resolver.resolve(ctx)
        if result.resolved:
            logger.info("[chain] %s: RESOLVED by %s -> tag=%s", col_name, resolver.name, result.resolution_tag)
            column["ColumnDescription"] = result.description
            if result.knowledge:
                column["Knowledge"] = _format_col_knowledge(result.knowledge)
            if bt_enabled and result.description:
                try:
                    bt = _generate_business_title(
                        table_name=table_name, col_name=col_name, col_description=result.description,
                        system_context=system_context, knowledge=result.knowledge, llm=llm,
                        sampling_params=bt_sampling_params,
                    )
                    if bt:
                        column["ColumnBusinessTitle"] = ai_prefix(bt)
                except Exception as exc:
                    logger.warning("business_title_failed %s.%s: %s", table_name, col_name, exc)
            return result.resolution_tag

    logger.info("[chain] %s: no resolver matched -> none", col_name)
    return "none"


# ===========================================================================
# Table-level flow (port of generate_metadata_async)
# ===========================================================================

def generate_metadata(
    table: Dict[str, Any],
    *,
    force_generate: bool = False,
    force_bt: bool = False,
    settings: Optional[Dict[str, Any]] = None,
    bm25: Optional[BM25Client] = None,
    llm: Optional[LLMGeneration] = None,
) -> Dict[str, Any]:
    """Generate metadata for a whole table dict. Returns the table + GenerationSummary."""
    settings = settings if settings is not None else config.default_settings()

    validated_table, err_msg = validate_table(table)
    if err_msg:
        raise ValueError(f"validation error: {err_msg}")

    bt_cfg = settings.get("business_title", {})
    bt_enabled = bool(bt_cfg.get("enabled", False))
    bt_sampling_params = bt_cfg.get("sampling") if bt_enabled else None

    table_desc_cfg = settings.get("table_description", {})
    table_desc_enabled = bool(table_desc_cfg.get("enabled", True))
    table_desc_sampling = table_desc_cfg.get("sampling", {"temperature": 0.1, "top_p": 0.9, "max_tokens": 500})

    table_description_needs_generation = force_generate or is_missing_desc(validated_table.get("TableDescription"))

    # Short-circuit only when nothing needs generating and force is off.
    if not force_generate:
        _, meta_err = metadata_validation(validated_table)
        if meta_err == "semua kolom telah memiliki deskripsi":
            bt_pending = bt_enabled and (
                force_bt or any(
                    is_missing_desc(c.get("ColumnBusinessTitle")) for c in validated_table.get("Columns", [])
                )
            )
            if not (bt_pending or table_description_needs_generation):
                raise NoGenerationNeeded(meta_err)

    table_name = norm(validated_table.get("TableName"))
    sampling_params = settings.get("llm", {}).get("sampling", {"temperature": 0.1, "top_p": 0.9, "max_tokens": 1200})

    bm25_cfg = settings.get("bm25", {})
    bm25_table_cfg = bm25_cfg.get("table_search", {})
    bm25_params = {
        "table": {"top_k": int(bm25_table_cfg.get("top_k", 20)), "threshold": float(bm25_table_cfg.get("threshold", 8.0))},
        "term": bm25_cfg.get("term_search", {}),
        "global": bm25_cfg.get("global_search", {}),
    }

    request_id = str(uuid.uuid4())
    bm25 = bm25 or BM25Client()
    # NOTE: was `LLMGeneration(BedrockLLMClient())` — always used Bedrock/GPT OSS
    # regardless of the LLM_PROVIDER env var. get_llm_client() respects
    # LLM_PROVIDER=bedrock/ollama/llama from .env instead (see clients.py).
    llm = llm or LLMGeneration(get_llm_client())

    logger.info("[flow] === table=%s force=%s request_id=%s ===", table_name, force_generate, request_id)

    # ---- System context ----
    logger.info("[flow] fetch system context (BM25 /system/context)")
    system_payload = bm25.get_system_context(table_name=table_name, request_id=request_id)
    if not isinstance(system_payload, dict):
        raise RuntimeError("invalid system context response")
    system_name = system_payload.get("system_name")
    system_context = system_payload.get("context", "")
    logger.info("[flow] system=%s context_len=%d", system_name, len(system_context or ""))

    # ---- Table-level term context (istilah tabel, e.g. 'ddyp2a') ----
    # Fetched once per table (like system context) and reused across all columns.
    logger.info("[flow] === STEP: table term knowledge (istilah tabel) ===")
    table_term_context = bm25.get_table_term_context(table_name, request_id=request_id)
    if table_term_context:
        for entry in table_term_context:
            for code, meaning in entry.items():
                logger.info("[flow] table_term: MATCH code=%r -> %s", code, norm(meaning) or "(no description)")
        logger.info(
            "[flow] table_term: %d code(s) found -> will be injected as extra context for every column",
            len(table_term_context),
        )
    else:
        logger.info("[flow] table_term: no code in table name matched the dictionary -> no extra context")

    # ---- 1. Table search ----
    logger.info("[flow] BM25 table search (top_k=%s threshold=%s)", bm25_params["table"]["top_k"], bm25_params["table"]["threshold"])
    table_hits = bm25.get_table_knowledge(
        table_name=table_name,
        top_k=int(bm25_params["table"]["top_k"]),
        threshold=float(bm25_params["table"]["threshold"]),
        request_id=request_id,
    )
    logger.info("[flow] table hits=%d max_score=%.2f", len(table_hits or []), _max_table_score(table_hits))

    # ---- 1b. Optional Confluence fallback discovery ----
    confluence_cfg = settings.get("confluence_fallback", {})
    confluence_summary = _empty_confluence_fallback_summary(bool(confluence_cfg.get("enabled", False)))
    temporary_knowledge_by_column: Dict[str, List[Dict[str, Any]]] = {}
    columns = validated_table.get("Columns", [])
    if _should_attempt_confluence_fallback(settings=settings, table_hits=table_hits, columns=columns):
        logger.info("[flow] confluence fallback: weak table score + missing columns -> discovering")
        confluence_summary["attempted"] = True
        confluence_summary["status"] = "searching"
        try:
            column_names = [norm(c.get("ColumnName")) for c in columns if norm(c.get("ColumnName"))]
            response = ConfluenceDiscoveryService().discover(
                ConfluenceDiscoveryRequest(
                    table_name=table_name, columns=column_names,
                    source_schema=norm(validated_table.get("SourceSchema")) or None,
                    source_system=norm(system_name) or None,
                    limit=int(confluence_cfg.get("limit", 10)), request_id=request_id,
                )
            )
            temporary_knowledge_by_column, stats = _temporary_knowledge_from_confluence(
                table_name=table_name, response=response,
                min_confidence=float(confluence_cfg.get("min_confidence", 0.8)),
                allowed_labels=confluence_cfg.get("allowed_labels"),
            )
            confluence_summary.update(stats)
            confluence_summary["status"] = "used" if temporary_knowledge_by_column else "no_usable_evidence"
            logger.info(
                "[flow] confluence discovery: candidates=%d usable=%d matched_columns=%s status=%s",
                stats.get("candidate_count", 0), stats.get("usable_candidates", 0),
                stats.get("matched_columns", []), confluence_summary["status"],
            )
        except Exception as exc:
            logger.warning("confluence_fallback_failed table=%s error=%s", table_name, exc, exc_info=True)
            confluence_summary["status"] = "error"
            confluence_summary["error"] = str(exc)
    elif confluence_summary["enabled"]:
        logger.info("[flow] confluence fallback: skipped (strong BM25 or no missing columns)")
        confluence_summary["status"] = "skipped_strong_bm25_or_complete"

    # ---- 2. Process columns (sequential) ----
    outcomes: List[Dict[str, Any]] = []
    for col in validated_table.get("Columns", []):
        col_name = norm(col.get("ColumnName")) or "<unknown>"
        try:
            resolution = process_column(
                table_name=table_name, column=col, system_context=system_context, table_hits=table_hits,
                bm25=bm25, llm=llm, sampling_params=sampling_params, bm25_params=bm25_params, settings=settings,
                force_generate=force_generate, request_id=request_id, bt_enabled=bt_enabled,
                bt_sampling_params=bt_sampling_params, force_bt=force_bt,
                temporary_knowledge_by_column=temporary_knowledge_by_column,
                table_term_context=table_term_context,
            )
            outcomes.append({"column_name": col_name, "status": "ok", "resolution": resolution or "none"})
        except Exception as exc:
            logger.exception("column_generation_failed table=%s column=%s", table_name, col_name)
            outcomes.append({"column_name": col_name, "status": "error", "error": str(exc)})

    failures = [o for o in outcomes if o.get("status") == "error"]
    if failures:
        logger.warning("partial failures table=%s failures=%d", table_name, len(failures))

    # ---- 3. Table description ----
    table_description_generated = False
    if table_desc_enabled and table_description_needs_generation:
        try:
            table_description = _generate_table_description(
                table_name=table_name, system_context=system_context, table_hits=table_hits,
                columns=validated_table.get("Columns", []), llm=llm, sampling_params=table_desc_sampling,
            )
            if table_description:
                validated_table["TableDescription"] = table_description
                table_description_generated = True
        except Exception as exc:
            logger.warning("table_description_generation_failed table=%s error=%s", table_name, exc)

    # ---- 4. Auto-approve ----
    required_source = settings.get("auto_approve", {}).get("required_source_type", "confluence")
    auto_approve_eligible = _check_auto_approve_eligible(
        outcomes, validated_table.get("Columns", []), required_source_type=required_source
    )

    return {
        **validated_table,
        "GenerationSummary": {
            "request_id": request_id,
            "total_columns": len(validated_table.get("Columns", [])),
            "failed_columns": len(failures),
            "failures": failures,
            "outcomes": outcomes,
            "auto_approve_eligible": auto_approve_eligible,
            "table_description_generated": table_description_generated,
            "has_table_description": not is_missing_desc(validated_table.get("TableDescription")),
            "confluence_fallback": confluence_summary,
        },
    }


# ===========================================================================
# Single-column convenience entry (keeps the old CLI shape)
# ===========================================================================

def generate_column(
    table_name: str,
    col_name: str,
    *,
    settings: Optional[Dict[str, Any]] = None,
    bm25: Optional[BM25Client] = None,
    llm: Optional[LLMGeneration] = None,
) -> Dict[str, Any]:
    """Run the full resolver chain for ONE column (no table description / auto-approve)."""
    settings = settings if settings is not None else config.default_settings()
    table = {"TableName": table_name, "Columns": [{"ColumnName": col_name, "ColumnDescription": ""}]}

    # Reuse the table flow but disable table-description generation for a single column.
    settings = dict(settings)
    settings["table_description"] = {**settings.get("table_description", {}), "enabled": False}

    result = generate_metadata(table, force_generate=True, settings=settings, bm25=bm25, llm=llm)
    column = result["Columns"][0]
    outcome = next((o for o in result["GenerationSummary"]["outcomes"] if o["column_name"] == col_name), {})
    return {
        "table_name": table_name,
        "col_name": col_name,
        "description": column.get("ColumnDescription"),
        "business_title": column.get("ColumnBusinessTitle"),
        "resolver": outcome.get("resolution", "none"),
        "knowledges": column.get("Knowledge", []),
    }


class NoGenerationNeeded(Exception):
    """Raised when nothing needs generating (all descriptions already present)."""


__all__ = [
    "generate_metadata",
    "generate_column",
    "process_column",
    "validate_table",
    "metadata_validation",
    "NoGenerationNeeded",
]