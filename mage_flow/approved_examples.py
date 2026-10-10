"""Steward-approved column metadata, retrieved as few-shot examples.

Source: the human-evaluation workbook ("MAGE - Human Evaluation for MAGE.xlsx",
sheet "Validate"). Each row is a column MAGE described and a data steward
reviewed. The approved name/definition is the steward's MMT reference when
present, otherwise MAGE's own output when the steward rated it Pass/Similar.

For a target column, ``ApprovedExamples.similar()`` returns approved columns
from OTHER tables whose names look alike (same name, shared character
trigrams, or -- for AS400 tables -- the same field suffix). They show the
selector how stewards named similar columns; they are context, not candidates.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set

from .common import is_missing_desc, norm

logger = logging.getLogger(__name__)

DEFAULT_SHEET = "Validate"
ACCEPTED_MAGE_RATINGS = frozenset({"pass", "similar"})
MIN_SCORE = 0.34
AS400_SUFFIX_LEN = 3
AS400_SUFFIX_SCORE = 0.5
MAX_DEFINITION_CHARS = 200


@dataclass(frozen=True)
class ApprovedColumn:
    table_name: str
    column_name: str
    name: str
    definition: str


def _trigrams(text: str) -> Set[str]:
    s = f"  {re.sub(r'[^a-z0-9]', '', text.lower())} "
    return {s[i:i + 3] for i in range(len(s) - 2)}


def _is_as400(table_name: str) -> bool:
    return any(tok in ("as4", "as400") for tok in re.split(r"[^a-z0-9]+", table_name.lower()))


class ApprovedExamples:
    def __init__(self, columns: List[ApprovedColumn]) -> None:
        self.columns = columns
        self._grams = [_trigrams(c.column_name) for c in columns]

    @classmethod
    def from_workbook(cls, path: Path, sheet: str = DEFAULT_SHEET) -> "ApprovedExamples":
        import pandas as pd

        df = pd.read_excel(path, sheet_name=sheet)
        out: List[ApprovedColumn] = []
        seen: Set[tuple] = set()
        for _, row in df.iterrows():
            table, column = norm(row.get("Tabel")), norm(row.get("Kolom"))
            if not table or not column:
                continue
            name, definition = norm(row.get("Nama Data Elemen [MMT]")), norm(row.get("Definisi Data Elemen [MMT]"))
            if not name and norm(row.get("Kemiripan")).lower() in ACCEPTED_MAGE_RATINGS:
                name, definition = norm(row.get("Nama Data Elemen [MAGE]")), norm(row.get("Definisi Data Elemen [MAGE]"))
            if not name or is_missing_desc(name):
                continue
            key = (column.lower(), name.lower())
            if key in seen:  # same column approved in several copies of a table
                continue
            seen.add(key)
            out.append(ApprovedColumn(table, column, name, "" if is_missing_desc(definition) else definition))
        logger.info("[approved_examples] loaded %d approved columns from %s", len(out), path)
        return cls(out)

    def similar(self, col_name: str, table_name: str, k: int = 5,
                exclude_tables: Optional[Set[str]] = None) -> List[ApprovedColumn]:
        target = norm(col_name).lower()
        if not target:
            return []
        grams = _trigrams(target)
        target_as400 = _is_as400(table_name)
        excluded = {t.lower() for t in (exclude_tables or set())} | {norm(table_name).lower()}
        scored: List[tuple] = []
        for col, col_grams in zip(self.columns, self._grams):
            if col.table_name.lower() in excluded:
                continue
            other = col.column_name.lower()
            if other == target:
                score = 1.0
            else:
                score = len(grams & col_grams) / len(grams | col_grams) if grams else 0.0
                if (target_as400 and _is_as400(col.table_name) and len(target) > AS400_SUFFIX_LEN + 1
                        and len(other) > AS400_SUFFIX_LEN + 1 and target[-AS400_SUFFIX_LEN:] == other[-AS400_SUFFIX_LEN:]):
                    score = max(score, AS400_SUFFIX_SCORE)
            if score >= MIN_SCORE:
                scored.append((score, col))
        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored[:k]]


def format_examples(examples: List[ApprovedColumn]) -> str:
    lines = []
    for ex in examples:
        definition = f" -- {ex.definition[:MAX_DEFINITION_CHARS]}" if ex.definition and ex.definition != ex.name else ""
        lines.append(f"- {ex.table_name}.{ex.column_name} => \"{ex.name}\"{definition}")
    return "\n".join(lines)


__all__ = ["ApprovedColumn", "ApprovedExamples", "format_examples"]
