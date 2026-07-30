"""Source priority policy for knowledge retrieval.

Ported verbatim from src/services/resolvers/source_priority.py (import of
src.services.common.norm replaced with the local one).

The generation flow intentionally prefers curated, deterministic sources
before lower-confidence generic retrieval.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set

from .common import norm

KNOWLEDGE_PRIORITY_AS400 = 1
KNOWLEDGE_PRIORITY_KATA = 2
KNOWLEDGE_PRIORITY_INFORMATICA_CERTIFIED = 3


def _source_text(item: Dict[str, Any]) -> str:
    parts = [
        item.get("source_type"),
        item.get("source_schema"),
        item.get("database"),
        item.get("system_aplikasi"),
    ]
    return " ".join(norm(part).lower() for part in parts if norm(part))


def _is_as400(item: Dict[str, Any]) -> bool:
    text = _source_text(item)
    return any(token in text for token in ("kamus as400", "as400", "as_400", "as-400", "confluence"))


def _is_kata(item: Dict[str, Any]) -> bool:
    text = _source_text(item)
    return "kata" in text or bool(norm(item.get("data_element_id")))


def _is_informatica_certified(item: Dict[str, Any]) -> bool:
    source_type = norm(item.get("source_type")).lower()
    if source_type != "informatica":
        return False
    for key in (
        "certification_status",
        "certified_status",
        "asset_status",
        "certification",
        "certified",
        "is_certified",
    ):
        value = norm(item.get(key)).lower()
        if not value:
            continue
        return value in {"certified", "true", "yes", "y", "1"}
    return True


def knowledge_source_priority(item: Dict[str, Any]) -> Optional[int]:
    if not isinstance(item, dict):
        return None
    if _is_as400(item):
        return KNOWLEDGE_PRIORITY_AS400
    if _is_kata(item):
        return KNOWLEDGE_PRIORITY_KATA
    if _is_informatica_certified(item):
        return KNOWLEDGE_PRIORITY_INFORMATICA_CERTIFIED
    return None


def filter_by_priority(
    items: Iterable[Dict[str, Any]],
    allowed_priorities: Optional[Set[int]],
) -> List[Dict[str, Any]]:
    if allowed_priorities is None:
        return [item for item in items if isinstance(item, dict)]
    allowed = set(allowed_priorities)
    return [
        item
        for item in items
        if isinstance(item, dict) and knowledge_source_priority(item) in allowed
    ]


__all__ = [
    "KNOWLEDGE_PRIORITY_AS400",
    "KNOWLEDGE_PRIORITY_KATA",
    "KNOWLEDGE_PRIORITY_INFORMATICA_CERTIFIED",
    "knowledge_source_priority",
    "filter_by_priority",
]
