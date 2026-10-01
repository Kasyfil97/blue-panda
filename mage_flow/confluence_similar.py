"""ConfluenceSimilarResolver — LLM-synthesized description from Confluence
page evidence.

The existing ConfluenceFallbackResolver relies purely on a strict regex
field-table extractor (_extract_fields in confluence.py) — if a found page
doesn't have a perfectly-formatted "Column | Type | Description" table (or
the narrower "columnname: description" line pattern), NOTHING is extracted,
even when the page is clearly on-topic. In practice (see the 20-row probe
that prompted this module) Confluence search reliably finds candidate pages
(10/10 tables), but 0 ever survive the regex extractor + usable_metadata
label + min_confidence=0.8 filter — the resolver never gets a chance to use
what was actually found.

This resolver instead reuses the SAME synthesis call BM25Resolver already
uses (ctx.llm.col_desc_generate), feeding it Confluence page content as
`col_knowledge` — regex-extracted field text when available (higher
precision), a raw snippet/body excerpt otherwise — so the LLM judges
relevance and writes/rejects a description itself, the same way it already
does for BM25 dictionary knowledge, instead of a rigid parser doing that
job. PROMPT_COL_DESC_GENERATE already has domain-mismatch-discard and
low-confidence-empty-output guardrails built in (see prompts.py), so this
does not require a new prompt.

Standalone experimental variant — NOT a replacement for
ConfluenceFallbackResolver in the production/base hierarchy; used to
measure whether an LLM-synthesis step recovers the coverage the regex
extractor is discarding.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from .common import ai_prefix, norm
from .resolvers import BaseResolver, ResolverContext, ResolverResult

logger = logging.getLogger(__name__)

MAX_SNIPPET_CHARS = 800
TOP_K_PAGES = 5


class ConfluenceSimilarResolver(BaseResolver):
    """Expects ctx.confluence_candidates to be pre-populated with this
    TABLE's Confluence search candidates (one search per table, shared
    across all its columns — same principle as the KATA dataset pool and
    the production Confluence fallback's own per-table discover() call;
    avoids one extra network round-trip per column)."""

    def __init__(self, top_k: int = TOP_K_PAGES) -> None:
        self.top_k = top_k

    @property
    def name(self) -> str:
        return "confluence_similar"

    def _knowledge_from_candidates(self, ctx: ResolverContext, candidates: List[Any]) -> List[Dict[str, Any]]:
        col_key = norm(ctx.col_name).lower()
        items: List[Dict[str, Any]] = []
        for cand in candidates[: self.top_k]:
            # Prefer the regex-extracted, column-specific text when the
            # strict extractor DID find something for this exact column —
            # highest-precision signal available. Fall back to the page's
            # snippet/body excerpt so the LLM still has something to judge
            # even when the regex extractor found nothing at all.
            extracted_text = ""
            for f in getattr(cand, "extracted_fields", None) or []:
                if norm(f.get("field_name")).lower() == col_key:
                    extracted_text = norm(f.get("description"))
                    break
            body = extracted_text or norm(getattr(cand, "snippet", "")) or norm(getattr(cand, "body_text", ""))[:MAX_SNIPPET_CHARS]
            if not body:
                continue
            items.append({
                "TableName": ctx.table_name,
                "ColumnName": ctx.col_name,
                "ColumnDescription": body,
                "Score": getattr(cand, "score", None),
                "SourceType": "confluence_page",
                "PageTitle": getattr(cand, "title", ""),
                "PageId": getattr(cand, "page_id", ""),
                "PageUrl": getattr(cand, "url", ""),
                "IsColumnSpecificExtract": bool(extracted_text),
            })
        return items

    def resolve(self, ctx: ResolverContext) -> ResolverResult:
        candidates = getattr(ctx, "confluence_candidates", None) or []
        if not candidates:
            logger.info("[confluence_similar] %s.%s: no candidate pages -> skip", ctx.table_name, ctx.col_name)
            return ResolverResult(False, self.name)

        col_knowledge = self._knowledge_from_candidates(ctx, candidates)
        if not col_knowledge:
            logger.info(
                "[confluence_similar] %s.%s: %d candidate page(s) but none had readable text -> skip",
                ctx.table_name, ctx.col_name, len(candidates),
            )
            return ResolverResult(False, self.name)

        abbr_context = ctx.get_abbr_context()
        n_specific = sum(1 for k in col_knowledge if k["IsColumnSpecificExtract"])
        logger.info(
            "[confluence_similar] %s.%s: synthesizing from %d page(s) (%d column-specific extract, %d snippet-only)",
            ctx.table_name, ctx.col_name, len(col_knowledge), n_specific, len(col_knowledge) - n_specific,
        )
        out = ctx.llm.col_desc_generate(
            table_name=ctx.table_name,
            col_name=ctx.col_name,
            system_context=ctx.system_context,
            col_knowledge=col_knowledge,
            term_knowledge=abbr_context,
            table_term_knowledge=ctx.table_term_context,
            sampling_params=ctx.sampling_params,
        )
        description = ai_prefix(out.get("ColumnDescription"))
        logger.info("[confluence_similar] RESOLVED %s.%s -> %r", ctx.table_name, ctx.col_name, description)
        return ResolverResult(True, self.name, description=description, knowledge=col_knowledge)


__all__ = ["ConfluenceSimilarResolver"]