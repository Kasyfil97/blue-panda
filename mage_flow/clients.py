"""Synchronous HTTP clients — LLM + BM25 service.

Ported (trimmed, sync-only) from:
  - src/clients/llm/llama.py         -> LLMClient
  - src/clients/bm25_service.py      -> BM25Client
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

import requests

from . import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM client (BRI LLM API shape: POST -> {"data": {"llm_answer": "..."}})
# ---------------------------------------------------------------------------

class LLMClient:
    def __init__(
        self,
        url: str = config.LLM_URL,
        api_key: str = config.LLM_API_KEY,
        timeout: float = config.LLM_TIMEOUT,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.timeout = timeout
        self.session = requests.Session()

    def create_response(
        self,
        messages: List[Dict[str, str]],
        sampling_params: Optional[Dict[str, Any]] = None,
        retries: int = 3,
    ) -> str:
        """Return the raw ``llm_answer`` string. Retries on transient failure."""
        headers = {"Content-Type": "application/json", "Authorization": self.api_key}
        body = {"messages": messages, "functions": None, "sampling_params": sampling_params}

        delay = 1.0
        last_error: Optional[str] = None
        for attempt in range(1, retries + 1):
            try:
                resp = self.session.post(self.url, json=body, headers=headers, timeout=self.timeout)
                if resp.status_code == 200:
                    data = resp.json().get("data", {})
                    return (data or {}).get("llm_answer", "")
                last_error = f"status={resp.status_code}"
                print(f"  [LLM] attempt {attempt} status={resp.status_code}", file=sys.stderr)
            except requests.RequestException as exc:
                last_error = str(exc)
                print(f"  [LLM] attempt {attempt} error: {exc}", file=sys.stderr)

            if attempt < retries:
                time.sleep(delay)
                delay = min(delay * 2, 8)

        raise RuntimeError(f"LLM call failed after {retries} attempts ({last_error})")


# ---------------------------------------------------------------------------
# BM25 service client
# ---------------------------------------------------------------------------

class BM25Client:
    def __init__(
        self,
        base_url: str = config.BM25_URL,
        timeout: float = config.BM25_TIMEOUT,
        retries: int = config.BM25_RETRIES,
        index_name: str = config.BM25_INDEX_NAME,
        bearer_token: str = config.BM25_BEARER_TOKEN,
    ) -> None:
        base = base_url.rstrip("/")
        if base.endswith("/api/v1"):
            base = base[: -len("/api/v1")]
        self.base_url = base
        self.timeout = timeout
        self.retries = retries
        self.index_name = index_name
        self.bearer_token = bearer_token
        self.session = requests.Session()

    def _post(self, path: str, payload: Dict[str, Any], request_id: Optional[str] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers: Dict[str, str] = {}
        if request_id:
            headers["X-Request-ID"] = request_id
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"

        last_error: Optional[str] = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.post(url, json=payload, headers=headers, timeout=self.timeout)
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        last_error = f"invalid_json: {exc}"
                        break
                if 400 <= resp.status_code < 500:
                    last_error = f"client_error status={resp.status_code}"
                    print(f"  [BM25] {path} status={resp.status_code}", file=sys.stderr)
                    break
                last_error = f"server_error status={resp.status_code}"
            except requests.RequestException as exc:
                last_error = str(exc)
                print(f"  [BM25] {path} error: {exc}", file=sys.stderr)
            if attempt < self.retries:
                time.sleep(min(2 ** attempt, 5))

        logger.warning("bm25 permanent failure path=%s error=%s", path, last_error)
        return {}

    # -- reads used by the flow ------------------------------------------------

    def get_system_context(
        self,
        table_name: Optional[str] = None,
        system_name: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if table_name:
            payload["table_name"] = table_name
        if system_name:
            payload["system_name"] = system_name
        data = self._post("/api/v1/system/context", payload, request_id or str(uuid.uuid4()))
        return data or {}

    def get_table_knowledge(
        self,
        table_name: str,
        top_k: int = 20,
        threshold: float = 8.0,
        table_name_boost: float = 2.0,
        request_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        payload = {
            "query": table_name,
            "top_k": top_k,
            "threshold": threshold,
            "table_name_boost": table_name_boost,
            "mode": "table_name",
            "index_name": self.index_name,
        }
        data = self._post("/api/v1/search", payload, request_id or str(uuid.uuid4()))
        return data.get("results", []) if data else []

    def get_term_knowledge(
        self,
        term: str,
        docs: Optional[List[Dict[str, Any]]] = None,
        topk: int = 5,
        threshold: float = 1.0,
        table_name_boost: float = 2.0,
        table_scores: Optional[Dict[str, float]] = None,
        table_score_alpha: float = 0.9,
        source_types: Optional[List[str]] = None,
        request_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        table_names: Optional[List[str]] = None
        if docs:
            table_names = [d.get("table_name") for d in docs if isinstance(d, dict) and d.get("table_name")]
            if not table_names:
                return []
        payload: Dict[str, Any] = {
            "query": term,
            "top_k": topk,
            "threshold": threshold,
            "table_name_boost": table_name_boost,
            "mode": "column",
            "index_name": self.index_name,
            "table_names": table_names,
            "table_scores": table_scores,
            "table_score_alpha": table_score_alpha,
        }
        if source_types:
            payload["source_types"] = source_types
        data = self._post("/api/v1/search", payload, request_id or str(uuid.uuid4()))
        return data.get("results", []) if data else []

    def get_exact_lookup(
        self,
        table_name: str,
        column_name: str,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = {"table_name": table_name, "column_name": column_name}
        data = self._post("/api/v1/exact/lookup", payload, request_id or str(uuid.uuid4()))
        return data.get("match", {}) if data else {}

    def get_abbreviation_context(
        self,
        col_name: str,
        request_id: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        data = self._post("/api/v1/terms/context", {"col_name": col_name}, request_id or str(uuid.uuid4()))
        return data.get("context", []) if data else []

    def add_unknown_terms(
        self,
        terms: List[str],
        request_id: Optional[str] = None,
    ) -> List[str]:
        if not terms:
            return []
        data = self._post("/api/v1/terms/unknown", {"terms": terms}, request_id or str(uuid.uuid4()))
        return data.get("added", []) if data else []


__all__ = ["LLMClient", "BM25Client"]
