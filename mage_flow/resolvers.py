"""The resolver chain — synchronous port of src/services/resolvers/*.

Chain order (same as production _RESOLVER_CHAIN):
  1. ExactMatchResolver   (AS400 priority)      -> exact_as400  (covers confluence too)
  2. BM25Resolver         (AS400 + Confluence)  -> bm25_as400_confluence
  3. KataEvidenceResolver                        -> kata_technical_relation | kata_alias
  4. BM25Resolver         (Informatica cert.)   -> bm25_informatica_certified
  5. ConfluenceFallbackResolver                  -> confluence_fallback
  6. PureLLMResolver                             -> llm | unknown

Each resolver is a small class with .resolve(ctx) -> ResolverResult. The first
one that returns resolved=True wins.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from . import kata as kata_backend
from .clients import BM25Client
from .common import ai_prefix, is_missing_desc, norm
from .llm_generation import LLMGeneration
from .source_priority import (
    KNOWLEDGE_PRIORITY_AS400,
    KNOWLEDGE_PRIORITY_INFORMATICA_CERTIFIED,
    filter_by_priority,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result + context
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolverResult:
    resolved: bool
    resolution_tag: str
    description: Optional[str] = None
    knowledge: Optional[List[Dict[str, Any]]] = None


@dataclass
class ResolverContext:
    """Everything a resolver needs to process ONE column.

    ``get_hypothesis()`` / ``get_abbr_context()`` are lazy: the BM25 abbreviation
    fetch + LLM hypothesis call only happen once, and only for columns that reach
    a stage that needs them (exact/kata/confluence hits skip them entirely).
    """

    table_name: str
    col_name: str
    system_context: str
    table_hits: List[Dict[str, Any]]
    bm25: BM25Client
    llm: LLMGeneration
    sampling_params: Dict[str, Any]
    bm25_params: Dict[str, Any]
    settings: Dict[str, Any]
    request_id: Optional[str] = None
    temporary_knowledge_by_column: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    #: Table-level term context (istilah tabel), fetched once per table in the flow
    #: and shared across all columns. Prepended to the per-column abbreviation context.
    table_term_context: List[Dict[str, str]] = field(default_factory=list)

    _hypothesis: Optional[str] = field(default=None, repr=False)
    _abbr_context: Optional[Any] = field(default=None, repr=False)
    _lazy_loaded: bool = field(default=False, repr=False)

    def get_abbr_context(self) -> Any:
        if not self._lazy_loaded:
            self._load_lazy()
        return self._abbr_context

    def get_hypothesis(self) -> str:
        if not self._lazy_loaded:
            self._load_lazy()
        return self._hypothesis or ""

    def _load_lazy(self) -> None:
        logger.info("[context] %s: fetch abbreviation context (BM25 /terms/context)", self.col_name)
        # Column abbreviations stay their own context; table term knowledge (istilah
        # tabel) is kept SEPARATE and passed to the LLM under its own labeled section
        # so the model knows it describes the whole table's domain, not a column token.
        self._abbr_context = self.bm25.get_abbreviation_context(self.col_name, request_id=self.request_id)
        if self.table_term_context:
            codes = [c for entry in self.table_term_context for c in entry.keys()]
            logger.info(
                "[context] %s: table term knowledge (istilah tabel) codes=%s -> passed to LLM as table domain context",
                self.col_name, codes,
            )
        logger.info(
            "[context] %s: term knowledge column_abbr=%d table_terms=%d",
            self.col_name, len(self._abbr_context or []), len(self.table_term_context or []),
        )
        logger.info("[context] %s: generate hypothesis (LLM col_desc_hypothesis)", self.col_name)
        hypothesis_out = self.llm.col_desc_hypothesis(
            table_name=self.table_name,
            col_name=self.col_name,
            system_context=self.system_context,
            term_knowledge=self._abbr_context,
            table_term_knowledge=self.table_term_context,
            sampling_params=self.sampling_params,
        )
        self._hypothesis = norm(hypothesis_out.get("ColumnDescription"))
        logger.info("[context] %s: hypothesis=%r", self.col_name, self._hypothesis or "")
        self._lazy_loaded = True


class BaseResolver(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def resolve(self, ctx: ResolverContext) -> ResolverResult: ...


# ---------------------------------------------------------------------------
# 1. Exact match
# ---------------------------------------------------------------------------

class ExactMatchResolver(BaseResolver):
    def __init__(self, *, allowed_priorities: Optional[Set[int]] = None, resolution_tag: str = "exact_lookup") -> None:
        self.allowed_priorities = frozenset(allowed_priorities) if allowed_priorities else None
        self.resolution_tag = resolution_tag

    @property
    def name(self) -> str:
        return "exact_match"

    def resolve(self, ctx: ResolverContext) -> ResolverResult:
        logger.info("[exact:%s] %s.%s: BM25 exact lookup (/exact/lookup)", self.resolution_tag, ctx.table_name, ctx.col_name)
        exact_match = ctx.bm25.get_exact_lookup(ctx.table_name, ctx.col_name, request_id=ctx.request_id)
        exact_text = norm(exact_match.get("description")) if isinstance(exact_match, dict) else norm(exact_match)
        if not exact_text or is_missing_desc(exact_text):
            logger.info("[exact:%s] no exact match -> skip", self.resolution_tag)
            return ResolverResult(False, "exact_lookup")

        if isinstance(exact_match, dict):
            knowledge_item = {
                "table_name": exact_match.get("table_name") or ctx.table_name,
                "column_name": exact_match.get("field_name") or ctx.col_name,
                "column_description": exact_text,
                "source_schema": exact_match.get("source_schema"),
                "source_type": exact_match.get("source_type"),
                "page_id": exact_match.get("page_id"),
                "final_score": None,
            }
        else:
            knowledge_item = {
                "table_name": ctx.table_name, "column_name": ctx.col_name,
                "column_description": exact_text, "source_schema": None,
                "source_type": None, "page_id": None, "final_score": None,
            }

        if not filter_by_priority([knowledge_item], self.allowed_priorities):
            logger.info(
                "[exact:%s] match found (source_type=%r) but rejected by priority filter -> skip",
                self.resolution_tag, knowledge_item.get("source_type"),
            )
            return ResolverResult(False, self.resolution_tag)

        logger.info("[exact:%s] RESOLVED %s.%s (source_type=%r)", self.resolution_tag, ctx.table_name, ctx.col_name, knowledge_item.get("source_type"))
        return ResolverResult(True, self.resolution_tag, description=exact_text, knowledge=[knowledge_item])


# ---------------------------------------------------------------------------
# 2 & 4. BM25 (table-filtered -> global) + LLM synthesis
# ---------------------------------------------------------------------------

class BM25Resolver(BaseResolver):
    def __init__(
        self,
        *,
        allowed_priorities: Optional[Set[int]] = None,
        source_types: Optional[List[str]] = None,
        source_types_selector=None,
        resolution_tag: str = "llm",
    ) -> None:
        self.allowed_priorities = frozenset(allowed_priorities) if allowed_priorities else None
        self.source_types = list(source_types) if source_types else None
        self.source_types_selector = source_types_selector
        self.resolution_tag = resolution_tag

    def _get_source_types(self, table_name: str) -> Optional[List[str]]:
        if self.source_types_selector is not None:
            return self.source_types_selector(table_name)
        return self.source_types

    @property
    def name(self) -> str:
        return "bm25"

    def resolve(self, ctx: ResolverContext) -> ResolverResult:
        term_cfg = ctx.bm25_params.get("term", {})
        global_cfg = ctx.bm25_params.get("global", {})

        effective_source_types = self._get_source_types(ctx.table_name)
        logger.info("[bm25:%s] %s.%s: effective source_types=%r", self.resolution_tag, ctx.table_name, ctx.col_name, effective_source_types)
        logger.info("[bm25:%s] %s.%s: term search table-filtered (docs=%d)", self.resolution_tag, ctx.table_name, ctx.col_name, len(ctx.table_hits or []))
        raw_tf = self._table_filtered_search(ctx, term_cfg)
        col_knowledges = filter_by_priority(raw_tf, self.allowed_priorities)
        logger.info("[bm25:%s] table-filtered hits=%d, kept after priority=%d", self.resolution_tag, len(raw_tf or []), len(col_knowledges))
        if not col_knowledges:
            logger.info("[bm25:%s] falling back to GLOBAL term search", self.resolution_tag)
            raw_g = self._global_search(ctx, global_cfg)
            col_knowledges = filter_by_priority(raw_g, self.allowed_priorities)
            logger.info("[bm25:%s] global hits=%d, kept after priority=%d", self.resolution_tag, len(raw_g or []), len(col_knowledges))
        if not col_knowledges:
            logger.info("[bm25:%s] no usable knowledge -> skip", self.resolution_tag)
            return ResolverResult(False, "bm25")

        abbr_context = ctx.get_abbr_context()
        logger.info("[bm25:%s] synthesizing description via LLM from %d knowledge item(s)", self.resolution_tag, len(col_knowledges))
        out = ctx.llm.col_desc_generate(
            table_name=ctx.table_name,
            col_name=ctx.col_name,
            system_context=ctx.system_context,
            col_knowledge=col_knowledges,
            term_knowledge=abbr_context,
            table_term_knowledge=ctx.table_term_context,
            sampling_params=ctx.sampling_params,
        )
        description = ai_prefix(out.get("ColumnDescription"))
        logger.info("[bm25:%s] RESOLVED %s.%s -> %r", self.resolution_tag, ctx.table_name, ctx.col_name, description)
        return ResolverResult(True, self.resolution_tag, description=description, knowledge=col_knowledges)

    def _table_filtered_search(self, ctx: ResolverContext, term_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not ctx.table_hits:
            return []
        table_scores = {
            norm(h.get("table_name")): float(h.get("final_score", h.get("bm25_score", 0.0)) or 0.0)
            for h in ctx.table_hits
        }
        return ctx.bm25.get_term_knowledge(
            term=ctx.col_name,
            docs=ctx.table_hits,
            topk=int(term_cfg.get("top_k", 5)),
            threshold=float(term_cfg.get("threshold", 1.0)),
            table_name_boost=float(term_cfg.get("table_name_boost", 2.0)),
            table_scores=table_scores,
            table_score_alpha=float(term_cfg.get("table_score_alpha", 0.9)),
            source_types=self._get_source_types(ctx.table_name),
            request_id=ctx.request_id,
        )

    def _global_search(self, ctx: ResolverContext, global_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        return ctx.bm25.get_term_knowledge(
            term=ctx.col_name,
            docs=None,
            topk=int(global_cfg.get("top_k", 5)),
            threshold=float(global_cfg.get("threshold", 1.0)),
            table_name_boost=float(global_cfg.get("table_name_boost", 2.0)),
            source_types=self._get_source_types(ctx.table_name),
            request_id=ctx.request_id,
        )


# ---------------------------------------------------------------------------
# 3. KATA evidence (technical relation -> alias). Gated by settings["kata"].
# ---------------------------------------------------------------------------

class KataEvidenceResolver(BaseResolver):
    @property
    def name(self) -> str:
        return "kata_evidence"

    def resolve(self, ctx: ResolverContext) -> ResolverResult:
        kata_cfg = ctx.settings.get("kata", {})
        if not kata_cfg.get("enabled", False):
            logger.info("[kata] disabled in settings -> skip")
            return ResolverResult(False, self.name)

        normalized_column = norm(ctx.col_name)
        if not normalized_column:
            return ResolverResult(False, self.name)

        # 3a. Exact technical relation (table + field)
        if kata_cfg.get("technical_relation_enabled", True):
            logger.info("[kata] %s.%s: technical-relation lookup (Postgres cache)", ctx.table_name, normalized_column)
            try:
                relation_documents = kata_backend.search_active_kata_data_elements_by_technical_relation(
                    ctx.table_name, normalized_column
                )
                logger.info("[kata] technical-relation docs=%d", len(relation_documents or []))
            except Exception as exc:
                logger.warning("[kata] technical-relation lookup FAILED %s.%s: %s", ctx.table_name, normalized_column, exc)
                relation_documents = []
            relation_evidence = next(
                (
                    d for d in relation_documents or []
                    if isinstance(d, dict) and norm(d.get("status")).lower() == "active"
                    and norm(d.get("id")) and norm(d.get("data_element_name")) and norm(d.get("definition"))
                ),
                None,
            )
            if relation_evidence is not None:
                evidence_id = norm(relation_evidence.get("id"))
                definition = norm(relation_evidence.get("definition"))
                knowledge = [{
                    "table_name": ctx.table_name, "column_name": normalized_column,
                    "column_description": definition, "source_schema": "KATA", "source_type": "kata",
                    "data_element_id": evidence_id, "data_element_name": norm(relation_evidence.get("data_element_name")),
                    "evidence_url": f"{kata_backend.KATA_ELEMENT_BASE_URL}/{evidence_id}",
                    "match_reason": f"Exact KATA technical relation: {ctx.table_name}.{normalized_column}",
                    "final_score": 100.0,
                }]
                logger.info("[kata] RESOLVED via technical-relation %s.%s (element_id=%s)", ctx.table_name, normalized_column, evidence_id)
                return ResolverResult(True, "kata_technical_relation", description=definition, knowledge=knowledge)
            logger.info("[kata] no usable technical-relation evidence")

        # 3b. Exact alias (skip generic column names)
        if not kata_cfg.get("alias_enabled", True):
            logger.info("[kata] alias lookup disabled -> skip")
            return ResolverResult(False, self.name)
        if normalized_column.lower() in kata_backend.GENERIC_COLUMN_NAMES:
            logger.info("[kata] %r is a generic column name -> skip alias lookup", normalized_column)
            return ResolverResult(False, self.name)

        logger.info("[kata] %s: alias lookup (Postgres cache)", normalized_column)
        try:
            documents = kata_backend.search_active_kata_data_elements_by_alias(normalized_column)
            logger.info("[kata] alias docs=%d", len(documents or []))
        except Exception as exc:
            logger.warning("[kata] alias lookup FAILED %s.%s: %s", ctx.table_name, normalized_column, exc)
            documents = []
            if not kata_cfg.get("live_opensearch_fallback", False):
                return ResolverResult(False, self.name)

        if not documents and kata_cfg.get("live_opensearch_fallback", False):
            logger.info("[kata] cache miss -> live OpenSearch fallback for %r", normalized_column)
            try:
                documents = kata_backend.KataOpenSearchClient().search_data_elements(normalized_column) or []
                logger.info("[kata] OpenSearch docs=%d", len(documents))
            except Exception as exc:
                logger.warning("[kata] live OpenSearch fallback FAILED %s.%s: %s", ctx.table_name, normalized_column, exc)
                return ResolverResult(False, self.name)

        evidence = kata_backend.select_kata_evidence(documents or [], normalized_column)
        if evidence is None:
            logger.info("[kata] no exact alias evidence -> skip")
            return ResolverResult(False, self.name)

        knowledge = [{
            "table_name": ctx.table_name, "column_name": normalized_column,
            "column_description": evidence["definition"], "source_schema": "KATA", "source_type": "kata",
            "data_element_id": evidence["id"], "data_element_name": evidence["data_element_name"],
            "evidence_url": f"{kata_backend.KATA_ELEMENT_BASE_URL}/{evidence['id']}",
            "match_reason": f"Exact alias match: {normalized_column}", "final_score": 95.0,
        }]
        logger.info("[kata] RESOLVED via alias %s (element_id=%s)", normalized_column, evidence["id"])
        return ResolverResult(True, "kata_alias", description=evidence["definition"], knowledge=knowledge)


# ---------------------------------------------------------------------------
# 5. Confluence fallback (consumes temporary evidence collected up front)
# ---------------------------------------------------------------------------

class ConfluenceFallbackResolver(BaseResolver):
    @property
    def name(self) -> str:
        return "confluence_fallback"

    def resolve(self, ctx: ResolverContext) -> ResolverResult:
        evidence_items = ctx.temporary_knowledge_by_column.get(norm(ctx.col_name).lower(), [])
        logger.info("[confluence] %s: temporary evidence items=%d", ctx.col_name, len(evidence_items))
        for item in evidence_items:
            if not isinstance(item, dict):
                continue
            description = norm(
                item.get("column_description") or item.get("description") or item.get("ColumnDescription")
            )
            if not description or is_missing_desc(description):
                continue
            knowledge_item = {
                "table_name": item.get("table_name") or ctx.table_name,
                "column_name": item.get("column_name") or ctx.col_name,
                "column_description": description,
                "source_schema": item.get("source_schema"),
                "source_type": item.get("source_type") or "confluence",
                "page_id": item.get("page_id"),
                "final_score": item.get("final_score"),
            }
            logger.info("[confluence] RESOLVED %s.%s (page_id=%s)", ctx.table_name, ctx.col_name, knowledge_item.get("page_id"))
            return ResolverResult(True, self.name, description=description, knowledge=[knowledge_item])
        logger.info("[confluence] no usable evidence -> skip")
        return ResolverResult(False, self.name)


# ---------------------------------------------------------------------------
# 6. Pure LLM (terminal) with understanding-check gate
# ---------------------------------------------------------------------------

class PureLLMResolver(BaseResolver):
    @property
    def name(self) -> str:
        return "pure_llm"

    def resolve(self, ctx: ResolverContext) -> ResolverResult:
        hypothesis = ctx.get_hypothesis()
        logger.info("[pure_llm] %s.%s: hypothesis=%s", ctx.table_name, ctx.col_name, "present" if hypothesis else "EMPTY")

        if not hypothesis:
            logger.info("[pure_llm] no hypothesis -> running understanding check (LLM)")
            abbr_context = ctx.get_abbr_context()
            chk = ctx.llm.col_understanding_check(
                table_name=ctx.table_name,
                col_name=ctx.col_name,
                system_context=ctx.system_context,
                abbr_context=abbr_context,
                table_term_knowledge=ctx.table_term_context,
                sampling_params=ctx.sampling_params,
            )
            if not chk.get("understood", False):
                unknown = chk.get("unknown_terms", [])
                logger.info("[pure_llm] NOT understood -> report unknown terms=%s to BM25, return empty", unknown)
                ctx.bm25.add_unknown_terms(unknown, request_id=ctx.request_id)
                return ResolverResult(True, "unknown", description="")
            logger.info("[pure_llm] understood -> proceed to generation")

        abbr_context = ctx.get_abbr_context()
        logger.info("[pure_llm] generating description (LLM col_desc_generate)")
        out = ctx.llm.col_desc_generate(
            table_name=ctx.table_name,
            col_name=ctx.col_name,
            system_context=ctx.system_context,
            col_knowledge="",
            term_knowledge=abbr_context,
            table_term_knowledge=ctx.table_term_context,
            sampling_params=ctx.sampling_params,
        )
        description = ai_prefix(out.get("ColumnDescription"))
        logger.info("[pure_llm] RESOLVED %s.%s -> %r", ctx.table_name, ctx.col_name, description)
        return ResolverResult(True, "llm", description=description)


# ---------------------------------------------------------------------------
# The chain (same order + priorities as production _RESOLVER_CHAIN)
# ---------------------------------------------------------------------------

def _as400_confluence_source_types(table_name: str) -> List[str]:
    """Route to AS400 sources for tables whose name contains 'as4', else Confluence."""
    if "as4" in norm(table_name).lower():
        return ["as400", "as_400", "as-400", "kamus as400"]
    return ["confluence"]


def build_resolver_chain() -> List[BaseResolver]:
    return [
        ExactMatchResolver(allowed_priorities={KNOWLEDGE_PRIORITY_AS400}, resolution_tag="exact_as400"),
        BM25Resolver(
            allowed_priorities=None,
            source_types_selector=_as400_confluence_source_types,
            resolution_tag="bm25_as400_confluence",
        ),
        KataEvidenceResolver(),
        BM25Resolver(
            allowed_priorities={KNOWLEDGE_PRIORITY_INFORMATICA_CERTIFIED},
            source_types=["informatica"],
            resolution_tag="bm25_informatica_certified",
        ),
        ConfluenceFallbackResolver(),
        PureLLMResolver(),
    ]


__all__ = [
    "ResolverResult",
    "ResolverContext",
    "BaseResolver",
    "ExactMatchResolver",
    "BM25Resolver",
    "KataEvidenceResolver",
    "ConfluenceFallbackResolver",
    "PureLLMResolver",
    "build_resolver_chain",
]
