"""Synchronous HTTP clients — LLM + BM25 service.

Ported (trimmed, sync-only) from:
  - src/clients/llm/llama.py         -> LLMClient
  - src/clients/bm25_service.py      -> BM25Client
"""

from __future__ import annotations

import json
import logging
import re
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
# AWS Bedrock client (OIDC federation — mirrors bedrock_session.py)
# ---------------------------------------------------------------------------

_BEDROCK_SESSION_REFRESH_SECONDS = 3000  # re-auth ~50 min before 1 h expiry


class BedrockLLMClient:
    """Drop-in replacement for LLMClient using AWS Bedrock via Entra ID OIDC federation.

    Auth flow (mirrors bedrock_session.py):
      1. Entra ID client-credentials → OIDC JWT
      2. STS AssumeRoleWithWebIdentity (JWT + bridge role) → temp AWS creds
      3. STS AssumeRole (bridge creds + target role)     → final AWS creds
    Primary invoke : boto3 bedrock-runtime invoke_model
    Fallback invoke: SigV4-signed POST to Bedrock Mantle OpenAI-compatible endpoint
    """

    def __init__(
        self,
        region: str = config.AWS_REGION,
        model: str = config.BEDROCK_MODEL_ID,
        timeout: float = config.BEDROCK_TIMEOUT,
        azure_tenant_id: str = config.AZURE_TENANT_ID,
        azure_client_id: str = config.AZURE_CLIENT_ID,
        azure_client_secret: str = config.AZURE_CLIENT_SECRET,
        role_arn_bridge: str = config.AWS_ROLE_ARN_BRIDGE,
        role_arn_target: str = config.AWS_ROLE_ARN_TARGET,
    ) -> None:
        self.region = region
        self.model = model
        self._timeout = timeout
        self._azure_tenant_id = azure_tenant_id
        self._azure_client_id = azure_client_id
        self._azure_client_secret = azure_client_secret
        self._role_arn_bridge = role_arn_bridge
        self._role_arn_target = role_arn_target
        self.session = requests.Session()  # for Entra token + Mantle requests

        self._boto_session: Any = None
        self._target_creds: Optional[Dict[str, Any]] = None
        self._created_at: float = 0.0

        self._setup()

    # -- OIDC federation -----------------------------------------------------

    def _get_entra_token(self) -> str:
        resp = self.session.post(
            f"https://login.microsoftonline.com/{self._azure_tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._azure_client_id,
                "client_secret": self._azure_client_secret,
                "scope": f"{self._azure_client_id}/.default",
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    def _assume_bridge_role(self, access_token: str) -> Dict[str, Any]:
        import boto3
        sts = boto3.client("sts", region_name=self.region)
        return sts.assume_role_with_web_identity(
            RoleArn=self._role_arn_bridge,
            RoleSessionName="mage-bedrock-bridge",
            WebIdentityToken=access_token,
            DurationSeconds=3600,
        )["Credentials"]

    def _assume_target_role(self, bridge_creds: Dict[str, Any]) -> Dict[str, Any]:
        import boto3
        sts = boto3.client(
            "sts",
            region_name=self.region,
            aws_access_key_id=bridge_creds["AccessKeyId"],
            aws_secret_access_key=bridge_creds["SecretAccessKey"],
            aws_session_token=bridge_creds["SessionToken"],
        )
        return sts.assume_role(
            RoleArn=self._role_arn_target,
            RoleSessionName="mage-bedrock-target",
            DurationSeconds=3600,
        )["Credentials"]

    def _setup(self) -> None:
        import boto3
        logger.info("[Bedrock] Setting up OIDC session...")
        access_token = self._get_entra_token()
        bridge_creds = self._assume_bridge_role(access_token)
        self._target_creds = self._assume_target_role(bridge_creds)
        self._boto_session = boto3.Session(
            aws_access_key_id=self._target_creds["AccessKeyId"],
            aws_secret_access_key=self._target_creds["SecretAccessKey"],
            aws_session_token=self._target_creds["SessionToken"],
            region_name=self.region,
        )
        self._created_at = time.time()
        logger.info("[Bedrock] OIDC session ready")

    def _refresh_if_needed(self) -> None:
        if time.time() - self._created_at >= _BEDROCK_SESSION_REFRESH_SECONDS:
            logger.info("[Bedrock] Refreshing OIDC session (age >= %ds)", _BEDROCK_SESSION_REFRESH_SECONDS)
            self._setup()

    # -- invoke helpers ------------------------------------------------------

    @staticmethod
    def _is_expired(exc: Exception) -> bool:
        text = f"{type(exc).__name__}: {exc}"
        markers = (
            "ExpiredToken", "ExpiredTokenException", "InvalidSignatureException",
            "security token included in the request is expired",
            "The provided token has expired", "UnrecognizedClientException",
        )
        return any(m.lower() in text.lower() for m in markers)

    @staticmethod
    def _strip_reasoning(text: str) -> str:
        cleaned = re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=re.DOTALL | re.IGNORECASE)
        return cleaned.strip()

    def _invoke_standard(self, body: str) -> str:
        """Invoke via boto3 bedrock-runtime. Raises on failure."""
        runtime = self._boto_session.client("bedrock-runtime", region_name=self.region)
        resp = runtime.invoke_model(
            modelId=self.model,
            contentType="application/json",
            accept="application/json",
            body=body,
        )
        rb = json.loads(resp["body"].read())
        if "choices" in rb and rb["choices"]:
            return rb["choices"][0]["message"]["content"]
        if "content" in rb:
            return rb["content"][0]["text"]
        if "output" in rb:
            return rb["output"]
        return json.dumps(rb)[:300]

    def _invoke_mantle(self, body: str) -> Optional[str]:
        """SigV4-signed POST to Bedrock Mantle OpenAI-compatible endpoint."""
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials

        creds = self._target_creds
        mantle_url = f"https://bedrock-mantle.{self.region}.api.aws/v1/chat/completions"
        aws_req = AWSRequest(
            method="POST",
            url=mantle_url,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        SigV4Auth(
            Credentials(creds["AccessKeyId"], creds["SecretAccessKey"], creds["SessionToken"]),
            "bedrock",
            self.region,
        ).add_auth(aws_req)
        try:
            resp = self.session.post(
                mantle_url, headers=dict(aws_req.headers), data=body, timeout=self._timeout,
            )
            resp.raise_for_status()
            rb = resp.json()
            if "choices" in rb and rb["choices"]:
                return rb["choices"][0]["message"]["content"]
            logger.warning("[Bedrock] Unexpected Mantle response shape: %s", json.dumps(rb)[:200])
        except Exception as exc:
            logger.warning("[Bedrock] Mantle invoke failed: %s", exc)
        return None

    # -- public interface (same as LLMClient) --------------------------------

    def create_response(
        self,
        messages: List[Dict[str, str]],
        sampling_params: Optional[Dict[str, Any]] = None,
        retries: int = 3,
    ) -> str:
        """Return the assistant text. Same interface as LLMClient."""
        params = sampling_params or {}
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "max_tokens": int(params.get("max_tokens", 1200)),
            "temperature": float(params.get("temperature", 0.1)),
        })

        self._refresh_if_needed()

        last_error: Optional[str] = None
        for attempt in range(1, retries + 1):
            try:
                text = self._invoke_standard(body)
                return self._strip_reasoning(text)
            except Exception as exc:
                if self._is_expired(exc) and attempt < retries:
                    logger.info("[Bedrock] Token expired mid-call — re-authenticating...")
                    self._setup()
                    continue
                logger.warning("[Bedrock] invoke_model failed (%s), trying Mantle...", exc)
                text = self._invoke_mantle(body)
                if text is not None:
                    return self._strip_reasoning(text)
                last_error = str(exc)
                if attempt < retries:
                    time.sleep(min(2 ** (attempt - 1), 8))

        raise RuntimeError(f"Bedrock call failed after {retries} attempts ({last_error})")


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

    def get_table_term_context(
        self,
        table_name: str,
        request_id: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        data = self._post("/api/v1/tables/term-context", {"table_name": table_name}, request_id or str(uuid.uuid4()))
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


__all__ = ["LLMClient", "BedrockLLMClient", "BM25Client"]
