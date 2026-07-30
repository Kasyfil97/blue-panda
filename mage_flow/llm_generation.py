"""LLM generation methods — prompt formatting + parsing per task.

Ported (sync) from src/core/llm/generation_async.py. Each method formats a
prompt from ``prompts.py``, calls the LLM, and returns the parsed JSON dict.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from . import prompts
from .clients import LLMClient
from .common import parse_llm_output


def _json_or_empty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) if value else ""


class LLMGeneration:
    def __init__(self, client: Optional[LLMClient] = None) -> None:
        self.client = client or LLMClient()

    def _run(self, messages, sampling_params) -> Dict[str, Any]:
        raw = self.client.create_response(messages=messages, sampling_params=sampling_params)
        parsed = parse_llm_output(raw)
        if not isinstance(parsed, dict):
            raise ValueError("Expected JSON object from LLM")
        return parsed

    def col_desc_hypothesis(
        self,
        table_name: str,
        col_name: str,
        system_context: str,
        term_knowledge: Any = None,
        sampling_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        prompt = prompts.PROMPT_HYPOTHESIS.format(
            system_context=system_context or "",
            term_knowledge=_json_or_empty(term_knowledge),
        )
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"table name: {table_name}\ncolumn name: {col_name}"},
        ]
        return self._run(messages, sampling_params)

    def col_understanding_check(
        self,
        table_name: str,
        col_name: str,
        system_context: str,
        abbr_context: Any,
        sampling_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        prompt = prompts.PROMPT_UNDERSTANDING_CHECK.format(
            system_context=system_context or "",
            abbr_context=json.dumps(abbr_context, ensure_ascii=False, indent=2),
        )
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"table name: {table_name}, column name: {col_name}"},
        ]
        return self._run(messages, sampling_params)

    def col_desc_generate(
        self,
        table_name: str,
        col_name: str,
        system_context: str,
        col_knowledge: Any,
        term_knowledge: Any,
        sampling_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        prompt = prompts.PROMPT_COL_DESC_GENERATE.format(
            table_name=table_name,
            system_context=system_context or "",
            col_knowledge=_json_or_empty(col_knowledge),
            term_knowledge=_json_or_empty(term_knowledge),
        )
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": col_name},
        ]
        return self._run(messages, sampling_params)

    def table_desc_generate(
        self,
        table_name: str,
        system_context: str,
        table_knowledge: Any,
        columns: Any,
        sampling_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        prompt = prompts.PROMPT_TABLE_DESC_GENERATE.format(
            table_name=table_name,
            system_context=system_context or "",
            table_knowledge=_json_or_empty(table_knowledge),
            columns=_json_or_empty(columns),
        )
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"table name: {table_name}"},
        ]
        return self._run(messages, sampling_params)

    def col_business_title_generate(
        self,
        table_name: str,
        col_name: str,
        col_description: str,
        system_context: str = "",
        sampling_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        prompt = prompts.PROMPT_COL_BUSINESS_TITLE.format(
            table_name=table_name,
            col_name=col_name,
            col_description=col_description,
            system_context=system_context or "",
        )
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"table name: {table_name}, column name: {col_name}"},
        ]
        return self._run(messages, sampling_params)


__all__ = ["LLMGeneration"]
