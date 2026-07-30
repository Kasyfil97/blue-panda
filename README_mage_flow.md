# mage_flow — standalone MAGE generation flow

A dependency-free (from `src/`) synchronous port of the production
`src/services/metadata_generation_service.py` flow, so you can run and modify
the metadata-generation logic in isolation.

## Why

The production flow is async and imports deeply from the `ms-bribrain-mage`
package (resolvers, clients, LLM generation, KATA/Confluence services). This
copy pulls all the relevant logic into one small package you can edit freely:
prompts, search params, the resolver chain, and the orchestration.

## The flow (identical order to production `_RESOLVER_CHAIN`)

Per column, first stage that resolves wins:

1. **exact_as400** — exact BM25 lookup, AS400 source only
2. **bm25_as400** — BM25 term search + LLM synthesis, AS400 source only
3. **kata_evidence** — KATA technical-relation / alias match *(gated off)*
4. **bm25_informatica_certified** — BM25 term search, Informatica-certified only
5. **confluence_fallback** — evidence discovered up front from Confluence *(gated off)*
6. **pure_llm** — hypothesis + understanding-check gate, then LLM generation

Table-level extras: **table description** generation, **business title**
generation, **auto-approve** eligibility, and a **GenerationSummary**.

## Run

```bash
# single column
python generate_column_description.py AS400_TABUNGAN_NASABAH cust_bal_amt

# whole table (from JSON)
python generate_metadata.py sample_table.json --force
python generate_metadata.py sample_table.json --force --business-title --out result.json
```

## Configure

- **`mage_flow/config.py`** — connection env (LLM/BM25/KATA/Confluence/Postgres)
  and `SETTINGS` (sampling, BM25 params, and feature flags).
- **`mage_flow/prompts.py`** — every LLM prompt, editable.

Set env in a `.env` file (loaded automatically):

```
LLM_URL=...
LLM_API_KEY=...
BM25_SERVICE_URL=http://localhost:8001
BM25_INDEX_NAME=default
```

## Heavy stages (off by default)

`kata` and `confluence_fallback` need OpenSearch / Postgres / Confluence. They
are disabled in `SETTINGS`. Enable per run:

```python
settings = default_settings()
settings["kata"]["enabled"] = True            # needs KATA_OPENSEARCH_* / PG* env
settings["confluence_fallback"]["enabled"] = True  # needs CONFLUENCE_* env
```

KATA Postgres lookups require `psycopg2` (imported lazily; not needed when KATA
is off).

## Module map

| File | Responsibility |
|------|----------------|
| `config.py` | env + `SETTINGS` (tunables + feature flags) |
| `prompts.py` | all LLM prompts |
| `clients.py` | sync LLM + BM25 HTTP clients |
| `llm_generation.py` | prompt formatting + JSON parsing per task |
| `source_priority.py` | AS400 / KATA / Informatica priority policy |
| `kata.py` | KATA OpenSearch + Postgres cache (gated) |
| `confluence.py` | Confluence discovery heuristics (gated) |
| `resolvers.py` | the 6-stage resolver chain |
| `flow.py` | orchestrator: `generate_metadata` / `generate_column` |
