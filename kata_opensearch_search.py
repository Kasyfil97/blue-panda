"""Standalone script for searching data in KATA OpenSearch.

Usage:
    python kata_opensearch_search.py --type data-element --query "nama nasabah"
    python kata_opensearch_search.py --type dataset --query "tabungan"
    python kata_opensearch_search.py --type dataset --query "tabungan" --table-name "TB_TABUNGAN"
    python kata_opensearch_search.py --type data-element --id "some-doc-id"
    python kata_opensearch_search.py --type dataset --id "some-doc-id"
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
import unicodedata
from typing import Any, Dict, Iterable, List, Optional

import requests
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _norm(value: Any) -> str:
    """Normalize unicode text to plain ASCII-like form (mirrors src.services.common.norm)."""
    text = str(value or "").strip()
    return unicodedata.normalize("NFKC", text)


def _normalize_identifier(value: Any) -> str:
    """Strip everything except lowercase letters and digits (mirrors kata_alias_search logic)."""
    return re.sub(r"[^a-z0-9]+", "", _norm(value).lower())


def _iter_aliases(value: Any) -> Iterable[str]:
    """Yield each alias string from a list or comma/semicolon-separated string."""
    if isinstance(value, (list, tuple, set)):
        for item in value:
            text = _norm(item)
            if text:
                yield text
        return
    text = _norm(value)
    if not text:
        return
    for item in re.split(r"[,;\n]+", text):
        item = item.strip()
        if item:
            yield item


def _find_exact_alias(document: Dict[str, Any], column_name: str) -> Optional[str]:
    """Return the matched alias if any alias normalizes to the same identifier as column_name."""
    normalized_column = _normalize_identifier(column_name)
    if not normalized_column:
        return None
    for alias in _iter_aliases(document.get("data_element_alias")):
        if _normalize_identifier(alias) == normalized_column:
            return alias
    return None


def _is_active_status(value: Any) -> bool:
    return _norm(value).lower() == "active"


class KataOpenSearchClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float = 30.0,
        retries: int = 3,
        data_element_index: str = "data-element",
        dataset_index: str = "dataset",
        draft_index: str = "draft",
    ) -> None:
        self.base_url = self._normalize_base_url(base_url)
        self.timeout = timeout
        self.retries = retries
        self.data_element_index = data_element_index
        self.dataset_index = dataset_index
        self.draft_index = draft_index

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": self._build_auth_header(username, password),
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    @classmethod
    def from_env(cls) -> "KataOpenSearchClient":
        load_dotenv()
        base_url = os.getenv("KATA_OPENSEARCH_URL", "")
        username = os.getenv("KATA_OPENSEARCH_USERNAME", "")
        password = os.getenv("KATA_OPENSEARCH_PASSWORD", "")
        timeout = float(os.getenv("KATA_OPENSEARCH_TIMEOUT", "30"))
        retries = int(os.getenv("KATA_OPENSEARCH_RETRIES", "3"))
        data_element_index = os.getenv(
            "KATA_DATA_ELEMENT_OPENSEARCH_INDEX",
            os.getenv("KATA_OPENSEARCH_INDEX", "data-element"),
        )
        dataset_index = os.getenv("KATA_DATASET_OPENSEARCH_INDEX", "dataset")
        draft_index = os.getenv("KATA_DRAFT_OPENSEARCH_INDEX", "draft")
        return cls(
            base_url,
            username,
            password,
            timeout=timeout,
            retries=retries,
            data_element_index=data_element_index,
            dataset_index=dataset_index,
            draft_index=draft_index,
        )

    @staticmethod
    def _normalize_base_url(raw: str) -> str:
        value = (raw or "").strip()
        if not value:
            raise ValueError("KATA_OPENSEARCH_URL is not configured")
        if not value.startswith(("http://", "https://")):
            value = f"http://{value}"
        return value.rstrip("/")

    @staticmethod
    def _build_auth_header(username: str, password: str) -> str:
        if not username or not password:
            raise ValueError("KATA_OPENSEARCH_USERNAME or KATA_OPENSEARCH_PASSWORD is not configured")
        token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        return f"Basic {token}"

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        method_upper = method.upper()
        last_error = "unknown request error"

        for attempt in range(self.retries + 1):
            t0 = time.perf_counter()
            try:
                response = self.session.request(
                    method_upper, url, json=payload, timeout=self.timeout
                )
                elapsed_ms = (time.perf_counter() - t0) * 1000

                if response.ok:
                    logger.debug(
                        "%s %s -> %d (%.1f ms)", method_upper, url, response.status_code, elapsed_ms
                    )
                    return response.json() if response.text else {}

                preview = (response.text or "").strip().replace("\n", " ")[:240]
                last_error = f"status={response.status_code} url={url} response={preview}"
                logger.warning("Request failed (attempt %d/%d): %s", attempt + 1, self.retries + 1, last_error)

            except requests.RequestException as exc:
                elapsed_ms = (time.perf_counter() - t0) * 1000
                last_error = str(exc)
                logger.warning("Request exception (attempt %d/%d): %s", attempt + 1, self.retries + 1, exc)

            if attempt < self.retries:
                time.sleep(min(2**attempt, 5))

        raise RuntimeError(f"KATA OpenSearch request failed: {last_error}")

    # -------------------------------------------------------------------------
    # Data Element methods
    # -------------------------------------------------------------------------

    def get_data_element_by_id(self, doc_id: str) -> Optional[Dict[str, Any]]:
        normalized = doc_id.strip()
        if not normalized:
            return None
        try:
            response = self._request_json("GET", f"{self.data_element_index}/_doc/{normalized}")
        except RuntimeError as exc:
            if "status=404" in str(exc):
                return None
            raise
        if not response.get("found"):
            return None
        return self._to_data_element_document(str(response.get("_id") or ""), response.get("_source") or {})

    def search_data_elements(self, query: str) -> List[Dict[str, Any]]:
        normalized = query.strip()
        if not normalized:
            return []

        response = self._request_json(
            "POST",
            f"{self.data_element_index}/_search",
            payload={
                "size": 40,
                "_source": True,
                "query": {
                    "bool": {
                        "should": [
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
            doc = self._to_data_element_document(str(hit.get("_id") or ""), hit.get("_source") or {})
            if doc and _is_active_status(doc.get("status")):
                documents.append(doc)
            if len(documents) >= 8:
                break
        return documents

    def search_data_elements_by_alias(self, alias: str) -> List[Dict[str, Any]]:
        """Search data elements by exact alias match.

        Uses a dedicated alias-focused OpenSearch query (not the name-biased
        general search) so alias-only terms score high enough to appear in
        results. Then post-filters client-side with _find_exact_alias, which
        normalizes both sides (_normalize_identifier: strip non-alphanumeric,
        lowercase) before comparing — matching the KataEvidenceResolver logic.
        """
        normalized = _norm(alias)
        if not normalized:
            return []

        response = self._request_json(
            "POST",
            f"{self.data_element_index}/_search",
            payload={
                "size": 40,
                "_source": True,
                "query": {
                    "bool": {
                        "should": [
                            {"term": {"data_element_alias.keyword": {"value": normalized, "boost": 15}}},
                            {"match_phrase": {"data_element_alias": {"query": normalized, "boost": 8}}},
                            {"match": {"data_element_alias": {"query": normalized, "fuzziness": "AUTO", "boost": 3}}},
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

        matched: List[Dict[str, Any]] = []
        for hit in response.get("hits", {}).get("hits", []):
            doc = self._to_data_element_document(str(hit.get("_id") or ""), hit.get("_source") or {})
            if not doc or not _is_active_status(doc.get("status")):
                continue
            matched_alias = _find_exact_alias(doc, normalized)
            if matched_alias:
                doc["matched_alias"] = matched_alias
                matched.append(doc)
            if len(matched) >= 8:
                break
        return matched

    # -------------------------------------------------------------------------
    # Dataset methods
    # -------------------------------------------------------------------------

    def get_dataset_by_id(self, doc_id: str) -> Optional[Dict[str, Any]]:
        normalized = doc_id.strip()
        if not normalized:
            return None
        for index_name in (self.dataset_index, self.draft_index):
            try:
                response = self._request_json("GET", f"{index_name}/_doc/{normalized}")
            except RuntimeError as exc:
                if "status=404" in str(exc):
                    continue
                raise
            if not response.get("found"):
                continue
            doc = self._to_dataset_document(str(response.get("_id") or ""), response.get("_source") or {})
            if doc:
                return doc
        return None

    def search_datasets(
        self,
        query: str,
        *,
        table_name: Optional[str] = None,
        document_link: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        normalized = query.strip()
        normalized_table_name = (table_name or "").strip()
        normalized_document_link = (document_link or "").strip()
        if not normalized and not normalized_table_name and not normalized_document_link:
            return []

        documents: List[Dict[str, Any]] = []
        seen_ids: set = set()

        for index_name in (self.dataset_index, self.draft_index):
            should: List[Dict[str, Any]] = []
            if normalized:
                should.extend([
                    {"match_phrase": {"data.data_name": {"query": normalized, "boost": 12}}},
                    {"match_phrase": {"data_name": {"query": normalized, "boost": 12}}},
                    {"match_phrase": {"dataset_name": {"query": normalized, "boost": 12}}},
                    {"match_phrase": {"data.data_alias": {"query": normalized, "boost": 6}}},
                    {"match_phrase": {"data_alias": {"query": normalized, "boost": 6}}},
                    {"match": {"data.data_name": {"query": normalized, "fuzziness": "AUTO", "boost": 2}}},
                    {"match": {"data_name": {"query": normalized, "fuzziness": "AUTO", "boost": 2}}},
                ])
            if normalized_table_name:
                should.extend([
                    {"term": {"data_alias.keyword": {"value": normalized_table_name, "boost": 12}}},
                    {"term": {"data.data_alias.keyword": {"value": normalized_table_name, "boost": 12}}},
                ])
            if normalized_document_link:
                should.extend([
                    {"term": {"document_link.keyword": {"value": normalized_document_link, "boost": 8}}},
                    {"term": {"data.document_link.keyword": {"value": normalized_document_link, "boost": 8}}},
                ])

            payload: Dict[str, Any] = {
                "size": 8,
                "_source": True,
                "query": {"bool": {"should": should, "minimum_should_match": 1}},
            }
            if index_name == self.draft_index:
                payload["query"]["bool"]["filter"] = [{"term": {"draft_type.keyword": "dataset"}}]

            response = self._request_json("POST", f"{index_name}/_search", payload=payload)
            for hit in response.get("hits", {}).get("hits", []):
                doc = self._to_dataset_document(str(hit.get("_id") or ""), hit.get("_source") or {})
                if doc is None or doc["id"] in seen_ids:
                    continue
                seen_ids.add(doc["id"])
                documents.append(doc)

        return documents

    # -------------------------------------------------------------------------
    # Document parsers
    # -------------------------------------------------------------------------

    @staticmethod
    def _to_data_element_document(doc_id: str, source: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        name = str(source.get("data_element_name") or "").strip()
        if not doc_id or not name:
            return None
        aliases = source.get("data_element_alias")
        category = source.get("category")
        domain_data = source.get("domain_data")
        domain_data_name = source.get("domain_data_name")
        return {
            "id": doc_id,
            "data_element_name": name,
            "definition": str(source.get("definition") or "").strip(),
            "example": source.get("example"),
            "business_logic": source.get("business_logic"),
            "formulation": source.get("formulation"),
            "category": category if isinstance(category, list) else [],
            "format_type": source.get("format_type"),
            "confidentiality_level": source.get("confidentiality_level"),
            "domain_data": domain_data if isinstance(domain_data, list) else [],
            "domain_data_name": domain_data_name if isinstance(domain_data_name, list) else [],
            "data_element_alias": aliases if isinstance(aliases, list) else [],
            "length": source.get("length"),
            "status": source.get("status"),
            "pii_information": source.get("pii_information"),
            "timestamp_created": source.get("timestamp_created"),
            "timestamp_modified": source.get("timestamp_modified"),
        }

    @staticmethod
    def _to_dataset_document(doc_id: str, source: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not doc_id:
            return None
        draft_type = str(source.get("draft_type") or "").strip().lower()
        if draft_type and draft_type != "dataset":
            return None

        dataset_payload = source.get("data")
        if not isinstance(dataset_payload, dict):
            dataset_payload = source

        data_name = str(
            dataset_payload.get("data_name")
            or source.get("data_name")
            or source.get("dataset_name")
            or ""
        ).strip()
        if not data_name:
            return None

        raw_aliases = (
            dataset_payload.get("data_alias")
            or source.get("data_alias")
            or source.get("dataset_alias")
            or []
        )
        data_alias = raw_aliases if isinstance(raw_aliases, list) else []

        raw_elements = source.get("data_element_used") or dataset_payload.get("data_element_used")
        data_elements: List[Dict[str, Any]] = []
        if isinstance(raw_elements, list):
            for item in raw_elements:
                if not isinstance(item, dict):
                    continue
                element_name = str(item.get("data_element_name") or "").strip()
                if not element_name:
                    continue
                data_elements.append({
                    "id": str(item.get("doc_id") or item.get("id") or "").strip() or None,
                    "data_element_name": element_name,
                    "definition": str(item.get("definition") or "").strip(),
                    "data_element_alias": item.get("data_element_alias")
                    if isinstance(item.get("data_element_alias"), list)
                    else [],
                    "format_type": item.get("format_type"),
                    "status": item.get("status") or item.get("data_element_state"),
                })

        return {
            "id": doc_id,
            "data_name": data_name,
            "definition": str(
                dataset_payload.get("definition") or source.get("definition") or ""
            ).strip(),
            "data_alias": data_alias,
            "document_link": str(
                dataset_payload.get("document_link") or source.get("document_link") or ""
            ).strip() or None,
            "status": source.get("status"),
            "draft_type": source.get("draft_type"),
            "data_elements": data_elements,
            "data_element_count": len(data_elements),
        }


def _print_results(results: Any) -> None:
    print(json.dumps(results, indent=2, ensure_ascii=False, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description="Search KATA OpenSearch")
    parser.add_argument(
        "--type", "-t",
        choices=["data-element", "dataset"],
        required=True,
        help="Index type to search",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--query", "-q", help="Search query string (searches name + alias fields)")
    group.add_argument("--alias", "-a", help="Search data elements by alias only (data-element type only)")
    group.add_argument("--id", help="Fetch a document by its ID")
    parser.add_argument("--table-name", help="Filter datasets by table name / data alias (dataset search only)")
    parser.add_argument("--document-link", help="Filter datasets by document link (dataset search only)")
    parser.add_argument("--size", type=int, default=8, help="Max number of results to display (default: 8)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.alias and args.type != "data-element":
        parser.error("--alias is only supported for --type data-element")

    client = KataOpenSearchClient.from_env()
    logger.info("Connected to %s", client.base_url)

    if args.type == "data-element":
        if args.id:
            result = client.get_data_element_by_id(args.id)
            if result is None:
                print(f"No data element found with id={args.id!r}", file=sys.stderr)
                sys.exit(1)
            _print_results(result)
        elif args.alias:
            results = client.search_data_elements_by_alias(args.alias)
            print(f"Found {len(results)} data element(s) for alias={args.alias!r}")
            _print_results(results[: args.size])
        else:
            results = client.search_data_elements(args.query)
            print(f"Found {len(results)} data element(s) for query={args.query!r}")
            _print_results(results[: args.size])

    else:  # dataset
        if args.id:
            result = client.get_dataset_by_id(args.id)
            if result is None:
                print(f"No dataset found with id={args.id!r}", file=sys.stderr)
                sys.exit(1)
            _print_results(result)
        else:
            results = client.search_datasets(
                args.query,
                table_name=args.table_name,
                document_link=args.document_link,
            )
            print(f"Found {len(results)} dataset(s) for query={args.query!r}")
            _print_results(results[: args.size])


if __name__ == "__main__":
    main()
