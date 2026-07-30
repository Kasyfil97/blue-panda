"""Resolver that reuses approved KATA data elements as column evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import unicodedata
from typing import Any, Dict, Iterable, List, Optional

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity as _cosine_similarity
    _SKLEARN_AVAILABLE = True
except ImportError:
    _SKLEARN_AVAILABLE = False

# Allow running standalone from research/ directory
_MAGE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "ms-bribrain-mage"))
if _MAGE_ROOT not in sys.path:
    sys.path.insert(0, _MAGE_ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from src.clients.kata_opensearch import KataOpenSearchClient
    from src.repositories.kata_data_element_cache import (
        search_active_kata_data_elements_by_alias,
        search_active_kata_data_elements_by_technical_relation,
        upsert_kata_data_elements,
    )
    from src.services.common import norm
    _SRC_AVAILABLE = True
except ImportError:
    _SRC_AVAILABLE = False
    search_active_kata_data_elements_by_alias = None  # type: ignore
    search_active_kata_data_elements_by_technical_relation = None  # type: ignore
    upsert_kata_data_elements = None  # type: ignore

    def norm(value: Any) -> str:  # type: ignore
        return unicodedata.normalize("NFKC", str(value or "")).strip()

    try:
        _research_dir = os.path.dirname(os.path.abspath(__file__))
        if _research_dir not in sys.path:
            sys.path.insert(0, _research_dir)
        from kata_opensearch_search import KataOpenSearchClient  # type: ignore
    except ImportError:
        KataOpenSearchClient = None  # type: ignore

try:
    from .base import BaseResolver, ResolverContext, ResolverResult
except ImportError:
    class BaseResolver:  # type: ignore
        pass
    class ResolverContext:  # type: ignore
        pass
    class ResolverResult:  # type: ignore
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

logger = logging.getLogger(__name__)

KATA_ELEMENT_BASE_URL = "https://kata.bri.co.id/metadata-directory/element-detail"
SIMILARITY_THRESHOLD = 0.3

_GENERIC_COLUMN_NAMES = {
    "id",
    "ids",
    "key",
    "code",
    "kode",
    "name",
    "nama",
    "desc",
    "description",
    "status",
    "flag",
    "type",
    "jenis",
    "date",
    "dt",
    "ds",
    "year",
    "month",
    "day",
    "created_at",
    "created_date",
    "updated_at",
    "updated_date",
    "modified_at",
    "timestamp",
    "insert_date",
    "load_date",
}


def _normalize_identifier(value: Any) -> str:
    """Normalize technical names while preserving exact-token intent."""
    return re.sub(r"[^a-z0-9]+", "", norm(value).lower())


def _is_active(value: Any) -> bool:
    return norm(value).lower() == "active"


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


def _tokenize_name(value: Any) -> str:
    """Split technical identifiers and display names into comparable token strings."""
    text = norm(value).lower()
    tokens = re.split(r"[_\-\s]+", text)
    return " ".join(t for t in tokens if t)


def _compute_tfidf_similarities(query: str, candidate_names: List[str]) -> List[float]:
    """Compute TF-IDF cosine similarity between query and each candidate name."""
    if not candidate_names:
        return []
    if not _SKLEARN_AVAILABLE:
        logger.warning("sklearn not available; similarity scores will be 0.0. Install with: pip install scikit-learn")
        return [0.0] * len(candidate_names)

    query_tok = _tokenize_name(query)
    candidate_toks = [_tokenize_name(c) for c in candidate_names]
    texts = [query_tok] + candidate_toks

    try:
        vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), min_df=1)
        matrix = vectorizer.fit_transform(texts)
        sims = _cosine_similarity(matrix[0:1], matrix[1:])[0]
        return sims.tolist()
    except Exception as exc:
        logger.warning("TF-IDF similarity computation failed: %s", exc)
        return [0.0] * len(candidate_names)


def _select_best_by_similarity(
    documents: List[Dict[str, Any]],
    column_name: str,
    threshold: float = SIMILARITY_THRESHOLD,
) -> Optional[Dict[str, Any]]:
    """Return the best matching document by TF-IDF similarity on data_element_name."""
    candidates = []
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        if not _is_active(doc.get("status")):
            continue
        if not norm(doc.get("definition")) or not norm(doc.get("id")) or not norm(doc.get("data_element_name")):
            continue
        candidates.append(doc)

    if not candidates:
        return None

    best_score = -1.0
    best_doc: Optional[Dict[str, Any]] = None
    for doc in candidates:
        aliases = list(_iter_aliases(doc.get("data_element_alias")))
        if not aliases:
            continue
        scores = _compute_tfidf_similarities(column_name, aliases)
        doc_best = max(scores) if scores else 0.0
        if doc_best >= threshold and doc_best > best_score:
            best_score = doc_best
            best_doc = doc

    if best_doc is None:
        return None

    return {
        "id": norm(best_doc["id"]),
        "data_element_name": norm(best_doc["data_element_name"]),
        "definition": norm(best_doc["definition"]),
        "data_element_alias": best_doc.get("data_element_alias", []),
        "similarity_score": round(best_score, 4),
    }


def _live_fallback_enabled() -> bool:
    return os.getenv("ENABLE_LIVE_KATA_DATA_ELEMENT_FALLBACK", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }


class KataEvidenceResolver(BaseResolver):
    """Resolve a column using TF-IDF similarity against KATA data element names."""

    @property
    def name(self) -> str:
        return "kata_evidence"

    async def resolve(self, ctx: ResolverContext) -> ResolverResult:
        normalized_column = norm(ctx.col_name)
        if not normalized_column:
            return ResolverResult(False, self.name)

        # --- Technical relation search (exact match, score 100) ---
        custom_alias_search = ctx.kata_data_element_search is not None
        relation_search = ctx.kata_technical_relation_search
        if relation_search is None and not custom_alias_search:
            relation_search = search_active_kata_data_elements_by_technical_relation

        if relation_search is not None:
            try:
                loop = asyncio.get_running_loop()
                relation_documents = await loop.run_in_executor(
                    ctx.executor,
                    lambda: relation_search(ctx.table_name, normalized_column),
                )
            except Exception as exc:
                logger.warning(
                    "kata technical relation lookup failed table=%s column=%s error=%s",
                    ctx.table_name,
                    normalized_column,
                    exc,
                )
            else:
                relation_evidence = next(
                    (
                        document
                        for document in relation_documents or []
                        if isinstance(document, dict)
                        and _is_active(document.get("status"))
                        and norm(document.get("id"))
                        and norm(document.get("data_element_name"))
                        and norm(document.get("definition"))
                    ),
                    None,
                )
                if relation_evidence is not None:
                    evidence_id = norm(relation_evidence.get("id"))
                    definition = norm(relation_evidence.get("definition"))
                    evidence_url = f"{KATA_ELEMENT_BASE_URL}/{evidence_id}"
                    knowledge = [
                        {
                            "table_name": ctx.table_name,
                            "column_name": normalized_column,
                            "column_description": definition,
                            "source_schema": "KATA",
                            "source_type": "kata",
                            "data_element_id": evidence_id,
                            "data_element_name": norm(
                                relation_evidence.get("data_element_name")
                            ),
                            "data_element_alias": relation_evidence.get("data_element_alias", []),
                            "evidence_url": evidence_url,
                            "match_reason": (
                                "Exact KATA technical relation: "
                                f"{ctx.table_name}.{normalized_column}"
                            ),
                            "similarity_score": 1.0,
                            "final_score": 100.0,
                        }
                    ]
                    return ResolverResult(
                        True,
                        "kata_technical_relation",
                        description=definition,
                        knowledge=knowledge,
                    )

        if normalized_column.lower() in _GENERIC_COLUMN_NAMES:
            return ResolverResult(False, self.name)

        # --- Alias search: get all candidates, rank by TF-IDF similarity ---
        search = ctx.kata_data_element_search
        use_cache_default = search is None
        if search is None:
            search = search_active_kata_data_elements_by_alias

        documents: List[Dict[str, Any]] = []
        try:
            loop = asyncio.get_running_loop()
            documents = await loop.run_in_executor(
                ctx.executor, lambda: search(normalized_column)
            )
        except Exception as exc:  # KATA evidence must never block generation.
            logger.warning(
                "kata evidence lookup failed table=%s column=%s error=%s",
                ctx.table_name,
                normalized_column,
                exc,
            )
            if not use_cache_default or not _live_fallback_enabled():
                return ResolverResult(False, self.name)

        if not documents and use_cache_default and _live_fallback_enabled():
            try:
                loop = asyncio.get_running_loop()
                live_documents = await loop.run_in_executor(
                    ctx.executor,
                    lambda: KataOpenSearchClient().search_data_elements(normalized_column),
                )
                documents = live_documents or []
                if documents:
                    await loop.run_in_executor(
                        ctx.executor,
                        lambda: upsert_kata_data_elements(documents),
                    )
            except Exception as exc:
                logger.warning(
                    "kata live fallback lookup failed table=%s column=%s error=%s",
                    ctx.table_name,
                    normalized_column,
                    exc,
                )
                return ResolverResult(False, self.name)

        evidence = _select_best_by_similarity(documents or [], normalized_column)
        if evidence is None:
            return ResolverResult(False, self.name)

        similarity_score = evidence["similarity_score"]
        evidence_url = f"{KATA_ELEMENT_BASE_URL}/{evidence['id']}"
        knowledge = [
            {
                "table_name": ctx.table_name,
                "column_name": normalized_column,
                "column_description": evidence["definition"],
                "source_schema": "KATA",
                "source_type": "kata",
                "data_element_id": evidence["id"],
                "data_element_name": evidence["data_element_name"],
                "data_element_alias": evidence["data_element_alias"],
                "evidence_url": evidence_url,
                "match_reason": f"TF-IDF similarity match: {normalized_column} (score={similarity_score})",
                "similarity_score": similarity_score,
                "final_score": round(similarity_score * 95.0, 2),
            }
        ]
        return ResolverResult(
            True,
            "kata_alias",
            description=evidence["definition"],
            knowledge=knowledge,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Search KATA data elements by alias with TF-IDF similarity")
    parser.add_argument("--alias", "-a", required=True, help="Column alias to search (e.g. NM_NASABAH)")
    parser.add_argument("--size", type=int, default=8, help="Max results to display (default: 8)")
    parser.add_argument(
        "--threshold", "-t",
        type=float,
        default=SIMILARITY_THRESHOLD,
        help=f"Minimum similarity score to include in results (default: {SIMILARITY_THRESHOLD})",
    )
    args = parser.parse_args()

    if KataOpenSearchClient is None:
        print("ERROR: Could not import KataOpenSearchClient.", file=sys.stderr)
        sys.exit(1)

    if not _SKLEARN_AVAILABLE:
        print("WARNING: sklearn not installed. Install with: pip install scikit-learn", file=sys.stderr)

    normalized = norm(args.alias)
    client = KataOpenSearchClient.from_env()

    print(f"Searching alias: {normalized!r} ...")
    documents = client.search_data_elements(normalized)
    print(f"Found {len(documents)} candidate(s) from OpenSearch.")

    if not documents:
        print("No candidates found.")
        return

    results = []
    for doc in documents:
        aliases = list(_iter_aliases(doc.get("data_element_alias", [])))
        if not aliases:
            continue
        scores = _compute_tfidf_similarities(normalized, aliases)
        best = max(scores) if scores else 0.0
        if best >= args.threshold:
            results.append({**doc, "similarity_score": round(best, 4)})

    results.sort(key=lambda d: d["similarity_score"], reverse=True)

    if not results:
        print(f"No results above similarity threshold {args.threshold}.")
        print("Tip: lower --threshold to see more results.")
        return

    print(f"Found {len(results)} result(s) above threshold {args.threshold}:")
    print(json.dumps(results[: args.size], indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
