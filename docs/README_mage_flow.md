# mage_flow — standalone MAGE generation flow

A dependency-free (from `src/`) synchronous port of the production
`src/services/metadata_generation_service.py` flow, so you can run and modify
the metadata-generation logic in isolation.

## Why

The production flow is async and imports deeply from the `ms-bribrain-mage`
package (resolvers, clients, LLM generation, KATA/Confluence services). This
copy pulls all the relevant logic into one small package you can edit freely:
prompts, search params, the resolver chain, and the orchestration.

## The flow

Per column, the first stage that resolves wins. The chain is chosen by
`settings["resolver_chain"]`.

### `"v6"` (default)

Evaluated in `experiments/hierarchy_v4_eval.py`; results and method in
`docs/evaluasi_hierarchy_v3_v6.md` (desc similar 85% in v3 -> 92% in v6).

1. **exact_as400** — exact BM25 lookup, AS400 source only
2. **kata_similar** — candidates from the table's KATA dataset, an LLM picks one
   (rescue: global KATA search). Configured by `settings["kata_similar"]`:
   - `enriched_context` — system context, table terms, abbreviation expansion,
     sibling columns + data types, token/specificity/generic-name rules
   - `type_filter` — drops candidates whose value kind conflicts with the column
     data type before the LLM sees them
   - `use_bm25_context` — AS400 tables: BM25 dictionary hits as evidence
   - `approved_examples` — steward-approved metadata of similarly-named columns
     (default file `MAGE - Human Evaluation for MAGE.xlsx`, sheet `Validate`, or
     `MAGE_APPROVED_EXAMPLES_PATH`); skipped with a warning if the file is missing
3. **kata_evidence** — KATA technical-relation / alias match
4. **bm25_informatica_certified** — BM25 term search, Informatica-certified only
5. **bm25_confluence** — BM25 term search, Confluence source
6. **bm25_as400** — BM25 term search + LLM synthesis, AS400 tables only
7. **pure_llm** — hypothesis + understanding-check gate, then LLM generation

Pass `ColumnDataType` for each column: the type filter and the sibling-column
context depend on it.

### `"legacy"` (original production order)

exact_as400 -> bm25_as400 -> kata_evidence -> bm25_confluence ->
bm25_informatica_certified -> confluence_fallback -> pure_llm. Confluence
discovery only runs with this chain.

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
| `kata_similar.py` | KATA dataset-scoped LLM selection (v6 step 2) |
| `approved_examples.py` | steward-approved few-shot examples for `kata_similar` |
| `confluence.py` | Confluence discovery heuristics (legacy chain only) |
| `resolvers.py` | the resolver chains (`v6` / `legacy`) |
| `flow.py` | orchestrator: `generate_metadata` / `generate_column` |
