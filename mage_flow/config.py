"""Configuration for the standalone MAGE flow.

Everything you tune lives here:
  - Client connection settings (read from environment / .env)
  - SETTINGS: the runtime knobs that mirror the production `settings` dict
    passed into generate_metadata_async, plus feature flags for the heavy
    KATA / Confluence stages (both OFF by default so the flow runs without
    OpenSearch / Postgres / Confluence).

Edit SETTINGS freely — it is a plain dict.
"""

from __future__ import annotations

import copy
import os
from typing import Any, Dict

from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Client connection config (env-driven)
# ---------------------------------------------------------------------------

LLM_URL = os.getenv("LLM_URL", os.getenv("LLM_API_URL", ""))
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "120"))

BM25_URL = os.getenv("BM25_SERVICE_URL", "http://localhost:8001").rstrip("/")
BM25_TIMEOUT = float(os.getenv("BM25_SERVICE_TIMEOUT", "60"))
BM25_RETRIES = int(os.getenv("BM25_SERVICE_RETRY", "2"))
BM25_INDEX_NAME = os.getenv("BM25_INDEX_NAME", "default")
BM25_BEARER_TOKEN = os.getenv("BM25_SERVICE_BEARER_TOKEN", os.getenv("API_BEARER_TOKEN", ""))

# KATA OpenSearch (only used when SETTINGS["kata"]["enabled"] is True)
KATA_OPENSEARCH_URL = os.getenv("KATA_OPENSEARCH_URL", "")
KATA_OPENSEARCH_USERNAME = os.getenv("KATA_OPENSEARCH_USERNAME", "")
KATA_OPENSEARCH_PASSWORD = os.getenv("KATA_OPENSEARCH_PASSWORD", "")
KATA_OPENSEARCH_TIMEOUT = float(os.getenv("KATA_OPENSEARCH_TIMEOUT", "30"))
KATA_OPENSEARCH_RETRIES = int(os.getenv("KATA_OPENSEARCH_RETRIES", os.getenv("KATA_OPENSEARCH_RETRY", "3")))
# Faithful to production KataDraftConfig: index defaults to "data-element".
KATA_DATA_ELEMENT_INDEX = os.getenv(
    "KATA_DATA_ELEMENT_OPENSEARCH_INDEX",
    os.getenv("KATA_OPENSEARCH_INDEX", "data-element"),
)
KATA_USE_DASHBOARD_PROXY = os.getenv("KATA_OPENSEARCH_USE_DASHBOARD_PROXY", "false").strip().lower() in {
    "1", "true", "yes", "y",
}

# Postgres for the KATA cache lookups (technical-relation / alias).
# NOTE: dedicated KATA_PG_* namespace so it does NOT collide with the main
# app PG* env (which may point at a different production database). Defaults
# below target the local KATA cache DB.
KATA_PG_HOST = os.getenv("KATA_PGHOST", "localhost")
KATA_PG_PORT = int(os.getenv("KATA_PGPORT", "5432"))
KATA_PG_DATABASE = os.getenv("KATA_PGDATABASE", "mydb")
KATA_PG_SCHEMA = os.getenv("KATA_PGSCHEMA", "public")
KATA_PG_USER = os.getenv("KATA_PGUSER", "admin")
KATA_PG_PASSWORD = os.getenv("KATA_PGPASSWORD", "postgres")

# AWS Bedrock (OIDC federation — mirrors bedrock_session.py)
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "openai.gpt-oss-120b-1:0")
AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-3")
BEDROCK_TIMEOUT = float(os.getenv("BEDROCK_TIMEOUT", "120"))

# Azure AD / Entra ID (IdP for STS AssumeRoleWithWebIdentity)
AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID", "")
AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET", "")

# AWS role chaining: Entra token → bridge role → target role (Bedrock access)
AWS_ROLE_ARN_BRIDGE = os.getenv("AWS_ROLE_ARN_BRIDGE", "")
AWS_ROLE_ARN_TARGET = os.getenv("AWS_ROLE_ARN_TARGET", "")

# Confluence (only used when SETTINGS["confluence_fallback"]["enabled"] is True)
CONFLUENCE_BASE_URL = os.getenv("CONFLUENCE_BASE_URL", "https://confluence.bri.co.id")
# Faithful to production ConfluenceConfig: prefer PAT, then CONFLUENCE_PAT.
CONFLUENCE_PAT = os.getenv("PAT", os.getenv("CONFLUENCE_PAT", ""))
CONFLUENCE_TIMEOUT_CONNECT = float(os.getenv("CONFLUENCE_TIMEOUT_CONNECT", "5"))
CONFLUENCE_TIMEOUT_READ = float(os.getenv("CONFLUENCE_TIMEOUT_READ", "20"))


# ---------------------------------------------------------------------------
# Runtime settings (mirrors production `settings` + feature flags)
# ---------------------------------------------------------------------------

_DEFAULT_SETTINGS: Dict[str, Any] = {
    "llm": {
        "sampling": {"temperature": 0.1, "top_p": 0.9, "max_tokens": 1200},
    },
    "bm25": {
        "table_search": {"top_k": 20, "threshold": 8.0, "table_name_boost": 2.0},
        "term_search": {
            "top_k": 5,
            "threshold": 1.0,
            "table_name_boost": 2.0,
            "table_score_alpha": 0.9,
        },
        "global_search": {"top_k": 5, "threshold": 1.0, "table_name_boost": 2.0},
    },
    "business_title": {
        "enabled": False,
        "sampling": {"temperature": 0.1, "top_p": 0.9, "max_tokens": 400},
    },
    "table_description": {
        "enabled": True,
        "sampling": {"temperature": 0.1, "top_p": 0.9, "max_tokens": 500},
    },
    # ---- Heavy optional stages (OFF by default) ----
    "kata": {
        "enabled": True,                  # master switch for the KATA resolver
        "technical_relation_enabled": True,  # Postgres cache lookup by (table, field)
        "alias_enabled": True,               # Postgres cache lookup by column alias
        "live_opensearch_fallback": True,    # hit KATA OpenSearch when the cache misses
                                             # (required here: the local cache is empty)
    },
    "confluence_fallback": {
        "enabled": True,
        "min_table_score": 8.0,   # only try Confluence when best table hit is weaker
        "limit": 10,
        "min_confidence": 0.8,
        "allowed_labels": ["usable_metadata"],
        "timeout_seconds": 12.0,  # kept for parity; sync port applies it loosely
    },
    "auto_approve": {"required_source_type": "confluence"},
}


def default_settings() -> Dict[str, Any]:
    """Return a deep copy of the default settings so callers can mutate freely."""
    return copy.deepcopy(_DEFAULT_SETTINGS)


__all__ = ["default_settings"]
