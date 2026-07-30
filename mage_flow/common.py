"""Shared helpers — normalization, JSON parsing, [AI] prefixing.

Ported (standalone) from:
  - src/services/common.py
  - src/core/llm/generation_async.py  (JSON extraction)
  - src/services/metadata_generation_service.py  (_ai_prefix)
"""

from __future__ import annotations

import json
import re
from json import JSONDecodeError, JSONDecoder
from typing import Any


def norm(v: Any) -> str:
    """Trim to a clean string; None/whitespace collapse to ''."""
    return str(v or "").strip()


def is_missing_desc(v: Any) -> bool:
    """True when a description value is effectively empty."""
    return norm(v).lower() in ("", "none", "-", "nan")


def ai_prefix(text: Any) -> str:
    """Prefix generated text with '[AI] ' (idempotent). Empty stays empty."""
    value = norm(text)
    if not value:
        return value
    if value.startswith("[AI]"):
        return value
    return f"[AI] {value}"


def extract_json(text: str) -> str:
    """Pull the first JSON object/array out of a (possibly fenced) LLM answer."""
    stripped = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", stripped, re.IGNORECASE)
    if fence:
        inner = (fence.group(1) or "").strip()
        if inner:
            return inner
    start = min((i for i in (stripped.find("{"), stripped.find("[")) if i != -1), default=-1)
    if start == -1:
        return stripped
    candidate = stripped[start:]
    try:
        _, end = JSONDecoder().raw_decode(candidate)
        return candidate[:end]
    except JSONDecodeError:
        return candidate


def parse_llm_output(raw: str) -> Any:
    """Parse an LLM answer into a Python object, tolerating fences/preamble."""
    if not raw:
        raise ValueError("Empty LLM output")
    candidate = extract_json(raw)
    for attempt in (candidate, raw):
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Cannot parse LLM JSON. Raw: {raw[:200]!r}")


__all__ = ["norm", "is_missing_desc", "ai_prefix", "extract_json", "parse_llm_output"]
