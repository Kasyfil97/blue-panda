"""Confluence evidence discovery — standalone port.

Ported from:
  - src/clients/confluence.py                       (ConfluenceClient.search)
  - src/services/confluence_discovery_service.py    (heuristics + discover())

The Postgres persistence (discovery runs / candidates / curation) is dropped —
research does not need to persist. The valuable, tweakable logic (query-term
building, candidate scoring, field extraction, technical-mapping rejection) is
ported verbatim. Only used when SETTINGS["confluence_fallback"]["enabled"].
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional, Sequence

import requests

from . import config

logger = logging.getLogger(__name__)

GENERIC_COLUMNS = {
    "id", "ids", "ds", "dt", "date", "time", "created", "updated",
    "created_at", "updated_at", "inserted_at", "modified_at", "createdby", "updatedby",
}

METADATA_MARKERS = {
    "column", "columns", "field", "fields", "description", "deskripsi", "definition",
    "definisi", "data type", "datatype", "tipe data", "source", "target", "mapping",
    "table name", "nama tabel", "business rule", "aturan bisnis",
}


# ---------------------------------------------------------------------------
# Request / response dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfluenceDiscoveryRequest:
    table_name: str
    columns: List[str] = field(default_factory=list)
    source_schema: Optional[str] = None
    source_system: Optional[str] = None
    limit: int = 10
    request_id: Optional[str] = None


@dataclass
class ConfluenceDiscoveryCandidate:
    candidate_id: str
    page_id: str
    title: str
    url: str
    label: str
    confidence: float
    score: float
    reason: str
    matched_terms: List[str]
    snippet: str
    verification_status: str = "heuristic_only"
    extracted_fields: List[Dict[str, Any]] = field(default_factory=list)
    filtered_fields: List[Dict[str, Any]] = field(default_factory=list)
    body_hash: str = ""
    source_schema: Optional[str] = None
    source_system: Optional[str] = None


@dataclass(frozen=True)
class ConfluenceDiscoveryResponse:
    run_id: str
    query_terms: List[str]
    candidates: List[ConfluenceDiscoveryCandidate]


# ---------------------------------------------------------------------------
# Text helpers (verbatim)
# ---------------------------------------------------------------------------

def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _lower(value: Any) -> str:
    return _clean(value).lower()


def _unique(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: set = set()
    for value in values:
        clean = _clean(value)
        key = clean.lower()
        if clean and key not in seen:
            seen.add(key)
            out.append(clean)
    return out


def _tokens(value: str) -> List[str]:
    return [token for token in re.split(r"[^A-Za-z0-9]+", value or "") if len(token) >= 3]


def _is_high_signal_column(column: str) -> bool:
    clean = _lower(column)
    if not clean or clean in GENERIC_COLUMNS:
        return False
    parts = _tokens(clean)
    return len(clean) >= 4 and any(part not in GENERIC_COLUMNS for part in parts)


def _body_hash(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _field_rejection_reason(value: str) -> Optional[str]:
    description = _clean(value)
    if not description:
        return "empty description"
    lower = description.lower()
    if re.search(r"\b[a-z]\.[A-Z0-9_#]{3,}\b", description):
        return "technical mapping / SQL-like content"
    if re.search(r"\b(select|from|where|join|case\s+when|end\s+as|group\s+by|order\s+by)\b", lower):
        return "technical mapping / SQL-like content"
    if re.search(r"\b(sum|count|coalesce|cast|substr|substring|concat)\s*\(", lower):
        return "technical mapping / SQL-like content"
    uppercase_identifiers = re.findall(r"\b[A-Z][A-Z0-9_#]{3,}\b", description)
    if description.count("=") >= 1 and len(uppercase_identifiers) >= 2:
        return "technical mapping / SQL-like content"
    if "," in description and len(uppercase_identifiers) >= 4:
        return "technical mapping / SQL-like content"
    return None


# ---------------------------------------------------------------------------
# Confluence REST client (CQL search)
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []

    def handle_data(self, data: str) -> None:
        value = str(data or "").strip()
        if value:
            self.parts.append(value)

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.parts)).strip()


def _html_to_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(unescape(value or ""))
    return parser.text()


class ConfluenceClient:
    def __init__(self) -> None:
        self.base_url = config.CONFLUENCE_BASE_URL.rstrip("/")
        self.pat = config.CONFLUENCE_PAT
        self.timeout = (config.CONFLUENCE_TIMEOUT_CONNECT, config.CONFLUENCE_TIMEOUT_READ)
        self.session = requests.Session()

    def search(self, cql: str, *, limit: int = 10, request_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if not self.pat:
            logger.warning("confluence_pat_not_configured request_id=%s", request_id)
            return []
        url = f"{self.base_url}/rest/api/content/search"
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.pat}"}
        if request_id:
            headers["X-Request-ID"] = request_id
        resp = self.session.get(
            url,
            params={"cql": cql, "limit": max(1, min(int(limit or 10), 50)), "expand": "body.storage,version,space"},
            headers=headers,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
        parsed: List[Dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            page_id = str(item.get("id") or "").strip()
            body_storage = ""
            if isinstance(item.get("body"), dict):
                body_storage = ((item["body"].get("storage") or {}).get("value")) or ""
            body_text = _html_to_text(str(body_storage))
            parsed.append(
                {
                    "page_id": page_id,
                    "title": str(item.get("title") or "").strip(),
                    "url": f"{self.base_url}/pages/viewpage.action?pageId={page_id}" if page_id else self.base_url,
                    "snippet": body_text[:500],
                    "body_text": body_text,
                }
            )
        return parsed


# ---------------------------------------------------------------------------
# Discovery service (no DB persistence)
# ---------------------------------------------------------------------------

class ConfluenceDiscoveryService:
    def __init__(self, confluence_client: Optional[ConfluenceClient] = None) -> None:
        self.confluence = confluence_client or ConfluenceClient()

    def build_query_terms(self, req: ConfluenceDiscoveryRequest) -> List[str]:
        table_name = _clean(req.table_name)
        table_tokens = [
            token for token in _tokens(table_name)
            if token.lower() not in {"tbl", "table", "data", "raw", "staging", "tmp"}
        ]
        business_tokens = [
            token for token in table_tokens if token.lower() not in {"db", "dwh", "as4", "hive", "ods"}
        ]
        bigrams = [
            f"{business_tokens[i]} {business_tokens[i + 1]}"
            for i in range(max(0, len(business_tokens) - 1))
        ]
        trigrams = [
            f"{business_tokens[i]} {business_tokens[i + 1]} {business_tokens[i + 2]}"
            for i in range(max(0, len(business_tokens) - 2))
        ]
        columns = [col for col in (req.columns or []) if _is_high_signal_column(col)]
        return _unique(
            [
                table_name,
                *trigrams[:4],
                *bigrams[:5],
                *columns[:8],
                req.source_schema or "",
                req.source_system or "",
            ]
        )

    def discover(self, req: ConfluenceDiscoveryRequest) -> ConfluenceDiscoveryResponse:
        query_terms = self.build_query_terms(req)
        raw_results: Dict[str, Dict[str, Any]] = {}
        for cql in self._build_cql_queries(query_terms):
            try:
                for data in self.confluence.search(cql, limit=max(1, min(int(req.limit or 10), 20)), request_id=req.request_id):
                    page_id = _clean(data.get("page_id") or data.get("id"))
                    key = page_id or f"{_lower(data.get('title'))}::{_lower(data.get('url'))}"
                    if key and key not in raw_results:
                        raw_results[key] = data
            except Exception:
                logger.warning("confluence_discovery_search_failed cql=%s", cql, exc_info=True)

        candidates = [self._candidate_from_result(req, item, query_terms) for item in raw_results.values()]
        candidates.sort(key=lambda item: item.score, reverse=True)
        limited = candidates[: max(1, min(int(req.limit or 10), 30))]
        return ConfluenceDiscoveryResponse(run_id="local", query_terms=query_terms, candidates=limited)

    def _build_cql_queries(self, query_terms: Sequence[str]) -> List[str]:
        queries: List[str] = []
        for term in query_terms[:12]:
            safe = term.replace("\\", "\\\\").replace('"', '\\"')
            queries.append(f'type=page AND (title ~ "{safe}" OR text ~ "{safe}")')
        return queries

    def _candidate_from_result(
        self, req: ConfluenceDiscoveryRequest, item: Dict[str, Any], query_terms: Sequence[str]
    ) -> ConfluenceDiscoveryCandidate:
        page_id = _clean(item.get("page_id") or item.get("id"))
        title = _clean(item.get("title") or "Confluence page")
        url = _clean(item.get("url")) or (
            f"{self.confluence.base_url}/pages/viewpage.action?pageId={page_id}" if page_id else ""
        )
        body = str(item.get("body_text") or item.get("document") or item.get("snippet") or "")
        snippet = _clean(item.get("snippet") or body[:500])[:500]
        score, confidence, label, reason, matched = self._score_candidate(
            req=req, title=title, body=body, query_terms=query_terms
        )
        fields, filtered_fields = self._extract_fields(req, body)
        if fields and label in {"mention_only", "supporting_context"}:
            label = "usable_metadata"
            confidence = max(confidence, 0.78)
            reason = f"{reason}; metadata-like table with field descriptions found"
        elif fields and "metadata-like table" not in reason.lower():
            reason = f"{reason}; metadata-like table with field descriptions found"
        elif label == "usable_metadata":
            label = "supporting_context"
            confidence = min(confidence, 0.68)
            reason = f"{reason}; exact terms were found, but no structured field definitions were extracted for curation"
        if filtered_fields:
            reason = f"{reason}; {len(filtered_fields)} technical mapping row(s) filtered"
        return ConfluenceDiscoveryCandidate(
            candidate_id=f"{page_id or _lower(title)}",
            page_id=page_id,
            title=title,
            url=url,
            label=label,
            confidence=round(confidence, 4),
            score=round(score, 4),
            reason=reason,
            matched_terms=matched,
            snippet=snippet,
            verification_status="heuristic_only",
            extracted_fields=fields,
            filtered_fields=filtered_fields,
            body_hash=_body_hash(body),
            source_schema=req.source_schema,
            source_system=req.source_system,
        )

    def _score_candidate(
        self, *, req: ConfluenceDiscoveryRequest, title: str, body: str, query_terms: Sequence[str]
    ):
        text = _lower(f"{title}\n{body}")
        matched: List[str] = []
        score = 0.0
        table_name = _lower(req.table_name)
        if table_name and table_name in text:
            matched.append(req.table_name)
            score += 28.0

        matched_columns = []
        for column in req.columns or []:
            if not _is_high_signal_column(column):
                continue
            if _lower(column) in text:
                matched.append(column)
                matched_columns.append(column)
                score += 6.0

        marker_count = sum(1 for marker in METADATA_MARKERS if marker in text)
        score += min(marker_count * 4.0, 24.0)

        if req.source_schema and _lower(req.source_schema) in text:
            matched.append(req.source_schema)
            score += 5.0
        if req.source_system and _lower(req.source_system) in text:
            matched.append(req.source_system)
            score += 5.0

        if "|" in body and marker_count >= 2:
            score += 12.0

        has_metadata = marker_count >= 2 and bool(matched_columns)
        has_context = marker_count >= 1 or len(body) >= 400
        if not matched:
            label = "not_relevant"
            confidence = 0.0
            reason = "No target table, column, source, or business token matched."
        elif has_metadata:
            label = "usable_metadata"
            confidence = min(0.95, 0.62 + (len(matched_columns) * 0.06) + (marker_count * 0.02))
            reason = "Exact metadata signals and target columns found."
        elif has_context:
            label = "supporting_context"
            confidence = min(0.72, 0.42 + (marker_count * 0.04))
            reason = "Relevant terms found with limited metadata context."
        else:
            label = "mention_only"
            confidence = 0.35
            reason = "Target table or terms are mentioned without usable metadata evidence."

        return min(score, 100.0), confidence, label, reason, _unique(matched)

    def _extract_fields(self, req: ConfluenceDiscoveryRequest, body: str):
        body_text = body or ""
        requested = {
            _lower(column): column for column in req.columns or [] if _is_high_signal_column(column)
        }
        if not requested:
            return [], []

        fields: List[Dict[str, Any]] = []
        filtered_fields: List[Dict[str, Any]] = []
        lines = [line.strip() for line in body_text.splitlines() if line.strip()]
        for line in lines:
            parts = [part.strip() for part in re.split(r"\s*\|\s*|\t+", line) if part.strip()]
            if len(parts) < 2:
                continue
            first = _lower(parts[0])
            if first in {"column", "column name", "field", "field name", "nama kolom"}:
                continue
            if first not in requested:
                continue
            data_type = ""
            description = ""
            if len(parts) >= 3:
                data_type = parts[1]
                description = " ".join(parts[2:])
            else:
                description = parts[1]
            rejection_reason = _field_rejection_reason(description)
            if rejection_reason:
                filtered_fields.append(
                    {"field_name": requested[first], "data_type": data_type,
                     "description": _clean(description)[:500], "reason": rejection_reason}
                )
            else:
                fields.append(
                    {"field_name": requested[first], "data_type": data_type, "description": description}
                )

        if fields:
            return fields, filtered_fields

        for column_lower, original in requested.items():
            pattern = re.compile(
                rf"\b{re.escape(original)}\b\s*[:\-]\s*(?P<desc>[^\n\r]{{12,240}})", re.IGNORECASE
            )
            match = pattern.search(body_text)
            if not match:
                continue
            description = match.group("desc")
            rejection_reason = _field_rejection_reason(description)
            if rejection_reason:
                filtered_fields.append(
                    {"field_name": original, "data_type": "", "description": _clean(description)[:500],
                     "reason": rejection_reason}
                )
            else:
                fields.append({"field_name": original, "data_type": "", "description": _clean(description)})
        return fields, filtered_fields


__all__ = [
    "ConfluenceDiscoveryRequest",
    "ConfluenceDiscoveryCandidate",
    "ConfluenceDiscoveryResponse",
    "ConfluenceClient",
    "ConfluenceDiscoveryService",
]
