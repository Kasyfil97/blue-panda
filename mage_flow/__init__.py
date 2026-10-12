"""Standalone port of the ms-bribrain-mage metadata generation flow.

Dependency-free from the production `src/` package — everything needed to run
and tweak the resolver flow lives here (chain "v6" by default, "legacy" via
settings["resolver_chain"]). Synchronous throughout, so it is
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
    kata_similar.py    KATA dataset-scoped LLM selection (v6 chain, step 2)
    approved_examples.py steward-approved few-shot examples for kata_similar
    confluence.py      Confluence discovery heuristics (legacy chain only)
    resolvers.py       the resolver chains (v6 / legacy)
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
