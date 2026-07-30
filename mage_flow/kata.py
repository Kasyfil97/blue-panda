"""KATA evidence backend — OpenSearch live search + Postgres cache lookups.

Ported (standalone, sync) from:
  - src/clients/kata_opensearch.py                    (search_data_elements)
  - src/repositories/kata_data_element_cache.py       (alias / technical-relation)

This whole module is OPTIONAL. It is only touched when SETTINGS["kata"]["enabled"]
is True. Postgres (psycopg2) and a reachable KATA OpenSearch are only imported /
contacted on demand, so the flow runs fine with KATA disabled and neither
dependency installed.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional

import requests

from . import config
from .common import norm

logger = logging.getLogger(__name__)

KATA_ELEMENT_BASE_URL = "https://kata.bri.co.id/metadata-directory/element-detail"


def normalize_alias_key(value: Any) -> str:
    """Normalize a technical alias for exact matching across casing/separators."""
    return re.sub(r"[^a-z0-9]+", "", norm(value).lower())


def _is_active(value: Any) -> bool:
    return norm(value).lower() == "active"


# ---------------------------------------------------------------------------
# OpenSearch client (live data-element search) — used only as a cache-miss
# fallback when SETTINGS["kata"]["live_opensearch_fallback"] is True.
# ---------------------------------------------------------------------------

class KataOpenSearchClient:
    def __init__(self) -> None:
        base = (config.KATA_OPENSEARCH_URL or "").strip()
        if not base:
            raise ValueError("KATA_OPENSEARCH_URL is not configured")
        if not base.startswith(("http://", "https://")):
            base = f"http://{base}"
        self.base_url = base.rstrip("/")
        self.timeout = config.KATA_OPENSEARCH_TIMEOUT
        self.data_element_index = config.KATA_DATA_ELEMENT_INDEX
        self.use_dashboard_proxy = config.KATA_USE_DASHBOARD_PROXY
        self.session = requests.Session()
        username = (config.KATA_OPENSEARCH_USERNAME or "").strip()
        password = config.KATA_OPENSEARCH_PASSWORD or ""
        if not username or not password:
            raise ValueError("KATA_OPENSEARCH_USERNAME / KATA_OPENSEARCH_PASSWORD not configured")
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self.session.headers.update(
            {"Authorization": f"Basic {token}", "Accept": "application/json", "Content-Type": "application/json"}
        )
        if self.use_dashboard_proxy:
            self.session.headers.update({"osd-xsrf": "true"})

    def _search(self, index: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        path = f"{index}/_search"
        if self.use_dashboard_proxy:
            url = f"{self.base_url}/api/console/proxy"
            resp = self.session.post(url, params={"path": path, "method": "POST"}, json=payload, timeout=self.timeout)
        else:
            url = f"{self.base_url}/{path}"
            resp = self.session.post(url, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json() if resp.text else {}

    def search_data_elements(self, query: str) -> List[Dict[str, Any]]:
        normalized = norm(query)
        if not normalized:
            return []
        response = self._search(
            self.data_element_index,
            {
                "size": 40,
                "_source": True,
                "query": {
                    "bool": {
                        "should": [
                            {"term": {"data_element_alias.keyword": {"value": normalized, "boost": 100}}},
                            {"match_phrase": {"data_element_name": {"query": normalized, "boost": 10}}},
                            {"match_phrase": {"data_element_alias": {"query": normalized, "boost": 5}}},
                            {"match": {"data_element_name": {"query": normalized, "fuzziness": "AUTO", "boost": 2}}},
                        ],
                        "minimum_should_match": 1,
                        "filter": [
                            {
                                "bool": {
                                    "should": [
                                        {"terms": {"status.keyword": ["Active", "active", "ACTIVE"]}},
                                        {"terms": {"status": ["Active", "active", "ACTIVE"]}},
                                        {"match_phrase": {"status": "Active"}},
                                    ],
                                    "minimum_should_match": 1,
                                }
                            }
                        ],
                    }
                },
            },
        )
        documents: List[Dict[str, Any]] = []
        for hit in response.get("hits", {}).get("hits", []):
            source = hit.get("_source") or {}
            doc_id = norm(hit.get("_id"))
            data_element_name = norm(source.get("data_element_name"))
            if not doc_id or not data_element_name:
                continue
            aliases = source.get("data_element_alias")
            document = {
                "id": doc_id,
                "data_element_name": data_element_name,
                "definition": norm(source.get("definition")),
                "data_element_alias": aliases if isinstance(aliases, list) else [],
                "status": source.get("status"),
            }
            if _is_active(document["status"]):
                documents.append(document)
            if len(documents) >= 8:
                break
        return documents


# ---------------------------------------------------------------------------
# Postgres cache lookups (exact alias / technical relation)
# ---------------------------------------------------------------------------

def _pg_connect():
    """Open a psycopg2 connection to the KATA cache DB (lazy import).

    Uses the dedicated KATA_PG_* settings and pins the schema via search_path
    so the unqualified table names resolve inside the configured schema.
    """
    import psycopg2  # imported lazily so the module loads without psycopg2

    return psycopg2.connect(
        host=config.KATA_PG_HOST,
        port=config.KATA_PG_PORT,
        dbname=config.KATA_PG_DATABASE,
        user=config.KATA_PG_USER,
        password=config.KATA_PG_PASSWORD,
        options=f"-c search_path={config.KATA_PG_SCHEMA}",
    )


def _aliases_from_row(raw_aliases: Any, matched_alias: str = "") -> List[str]:
    aliases: List[str] = []
    if isinstance(raw_aliases, list):
        aliases = [norm(item) for item in raw_aliases if norm(item)]
    elif isinstance(raw_aliases, str):
        try:
            parsed = json.loads(raw_aliases)
            if isinstance(parsed, list):
                aliases = [norm(item) for item in parsed if norm(item)]
        except json.JSONDecodeError:
            aliases = []
    matched = norm(matched_alias)
    if matched and matched not in aliases:
        aliases = [matched, *aliases]
    return aliases


def search_active_kata_data_elements_by_alias(alias: str, *, limit: int = 8) -> List[Dict[str, Any]]:
    """Find Active KATA data elements by exact normalized alias (Postgres cache)."""
    normalized_alias = normalize_alias_key(alias)
    if not normalized_alias:
        return []
    conn = _pg_connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.data_element_id, c.data_element_name, c.definition,
                       c.data_element_alias, c.status, a.alias
                FROM bribrain_mage_kata_data_element_aliases a
                JOIN bribrain_mage_kata_data_elements_cache c
                  ON c.data_element_id = a.data_element_id
                WHERE a.normalized_alias = %s
                  AND LOWER(COALESCE(c.status, '')) = 'active'
                ORDER BY CASE WHEN LOWER(a.alias) = LOWER(%s) THEN 0 ELSE 1 END,
                         c.data_element_name
                LIMIT %s
                """,
                (normalized_alias, norm(alias), int(limit)),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    return [
        {
            "id": row[0],
            "data_element_name": row[1],
            "definition": row[2],
            "data_element_alias": _aliases_from_row(row[3], row[5]),
            "status": row[4],
        }
        for row in rows
    ]


def search_active_kata_data_elements_by_technical_relation(
    table_name: str, field_name: str, *, limit: int = 8
) -> List[Dict[str, Any]]:
    """Find Active KATA elements linked to an exact technical table+field."""
    normalized_table = normalize_alias_key(table_name)
    normalized_field = normalize_alias_key(field_name)
    if not normalized_table or not normalized_field:
        return []
    conn = _pg_connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.data_element_id, c.data_element_name, c.definition,
                       c.data_element_alias, c.status, r.relation_id, r.table_name,
                       r.field_name, r.resource_name, r.database_name, r.field_edc_id
                FROM bribrain_mage_kata_technical_relations_cache r
                JOIN bribrain_mage_kata_data_elements_cache c
                  ON c.data_element_id = r.data_element_id
                WHERE r.normalized_table_name = %s
                  AND r.normalized_field_name = %s
                  AND LOWER(COALESCE(r.status, '')) = 'active'
                  AND LOWER(COALESCE(c.status, '')) = 'active'
                  AND NULLIF(BTRIM(COALESCE(c.definition, '')), '') IS NOT NULL
                ORDER BY CASE WHEN r.field_edc_id IS NOT NULL THEN 0 ELSE 1 END,
                         c.data_element_name, r.relation_id
                LIMIT %s
                """,
                (normalized_table, normalized_field, int(limit)),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    return [
        {
            "id": row[0],
            "data_element_name": row[1],
            "definition": row[2],
            "data_element_alias": _aliases_from_row(row[3]),
            "status": row[4],
            "technical_relation": {
                "relation_id": row[5],
                "table_name": row[6],
                "field_name": row[7],
                "resource_name": row[8],
                "database_name": row[9],
                "field_edc_id": row[10],
            },
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Generic-column guard + evidence selection (from kata_evidence resolver)
# ---------------------------------------------------------------------------

GENERIC_COLUMN_NAMES = {
    "id", "ids", "key", "code", "kode", "name", "nama", "desc", "description",
    "status", "flag", "type", "jenis", "date", "dt", "ds", "year", "month", "day",
    "created_at", "created_date", "updated_at", "updated_date", "modified_at",
    "timestamp", "insert_date", "load_date",
}


def _iter_aliases(value: Any) -> Iterable[str]:
    if isinstance(value, (list, tuple, set)):
        for item in value:
            text = norm(item)
            if text:
                yield text
        return
    text = norm(value)
    if not text:
        return
    for item in re.split(r"[,;\n]+", text):
        item = item.strip()
        if item:
            yield item


def find_exact_alias(document: Dict[str, Any], column_name: str) -> Optional[str]:
    normalized_column = normalize_alias_key(column_name)
    if not normalized_column:
        return None
    for alias in _iter_aliases(document.get("data_element_alias")):
        if normalize_alias_key(alias) == normalized_column:
            return alias
    return None


def select_kata_evidence(documents: List[Dict[str, Any]], column_name: str) -> Optional[Dict[str, Any]]:
    for document in documents:
        if not isinstance(document, dict) or not _is_active(document.get("status")):
            continue
        definition = norm(document.get("definition"))
        doc_id = norm(document.get("id"))
        data_element_name = norm(document.get("data_element_name"))
        if not definition or not doc_id or not data_element_name:
            continue
        matched_alias = find_exact_alias(document, column_name)
        if matched_alias:
            return {
                "id": doc_id,
                "data_element_name": data_element_name,
                "definition": definition,
                "matched_alias": matched_alias,
            }
    return None


__all__ = [
    "KATA_ELEMENT_BASE_URL",
    "GENERIC_COLUMN_NAMES",
    "KataOpenSearchClient",
    "normalize_alias_key",
    "search_active_kata_data_elements_by_alias",
    "search_active_kata_data_elements_by_technical_relation",
    "select_kata_evidence",
    "find_exact_alias",
]
