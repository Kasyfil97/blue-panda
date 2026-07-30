"""Standalone port of the ms-bribrain-mage metadata generation flow.

Dependency-free from the production `src/` package — everything needed to run
and tweak the 6-stage resolver flow lives here. Synchronous throughout, so it is
easy to step through and modify.

Quick start:
    from mage_flow import generate_metadata, generate_column, default_settings

    # single column
    print(generate_column("MY_TABLE", "cust_bal_amt"))

    # whole table
    settings = default_settings()
    settings["business_title"]["enabled"] = True
    result = generate_metadata(my_table_dict, force_generate=True, settings=settings)

Module map:
    config.py          connection env + SETTINGS (tunables + feature flags)
    prompts.py         all LLM prompts (edit to experiment)
    clients.py         sync LLM + BM25 HTTP clients
    llm_generation.py  prompt formatting + parsing per task
    source_priority.py AS400 / KATA / Informatica priority policy
    kata.py            KATA OpenSearch + Postgres cache (gated off)
    confluence.py      Confluence discovery heuristics (gated off)
    resolvers.py       the 6-stage resolver chain
    flow.py            orchestrator (generate_metadata / generate_column)
"""

from .config import default_settings
from .flow import (
    NoGenerationNeeded,
    generate_column,
    generate_metadata,
    metadata_validation,
    process_column,
    validate_table,
)

__all__ = [
    "default_settings",
    "generate_metadata",
    "generate_column",
    "process_column",
    "validate_table",
    "metadata_validation",
    "NoGenerationNeeded",
]
