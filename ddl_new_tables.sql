-- ============================================================================
-- New tables introduced in ms-bribrain-mage-rafi-ver (NOT present in base
-- ms-bribrain-mage). These back the two resolver stages the rafi-ver adds:
--   * KataEvidenceResolver        -> Section 1 (REQUIRED if you enable KATA)
--   * ConfluenceFallbackResolver  -> Section 2 (NOT needed by the research port)
--
-- Target: PostgreSQL.
-- DDL extracted verbatim from:
--   src/repositories/kata_data_element_cache.py      (ensure_*_tables)
--   src/services/confluence_discovery_service.py     (ensure_tables)
--
-- How the research port (research/mage_flow) uses these:
--   - Default config: KATA + Confluence are OFF -> NO database required at all.
--   - settings["kata"]["enabled"] = True -> Section 1 tables must EXIST and be
--     POPULATED. The port only runs SELECTs; it never creates or fills them
--     (production fills them via a separate KATA sync job).
--   - settings["confluence_fallback"]["enabled"] = True -> Section 2 is NOT
--     needed: the research port dropped Confluence persistence and runs the
--     discovery in-memory. Section 2 is included only for parity with the
--     production service, in case you run against the real backend.
-- ============================================================================


-- ============================================================================
-- SECTION 1 — KATA data-element cache  (REQUIRED for KataEvidenceResolver)
-- ============================================================================

-- 1a. Data elements (source of definitions).
CREATE TABLE IF NOT EXISTS bribrain_mage_kata_data_elements_cache (
    data_element_id     TEXT PRIMARY KEY,
    data_element_name   TEXT NOT NULL,
    definition          TEXT NULL,
    data_element_alias  JSONB NOT NULL DEFAULT '[]'::jsonb,
    status              TEXT NULL,
    timestamp_created   TEXT NULL,
    timestamp_modified  TEXT NULL,
    row_hash            TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 1b. Alias index -> used by search_active_kata_data_elements_by_alias().
CREATE TABLE IF NOT EXISTS bribrain_mage_kata_data_element_aliases (
    data_element_id  TEXT NOT NULL
        REFERENCES bribrain_mage_kata_data_elements_cache(data_element_id) ON DELETE CASCADE,
    alias            TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT bribrain_mage_kata_data_element_aliases_uniq
        UNIQUE (data_element_id, normalized_alias)
);

CREATE INDEX IF NOT EXISTS bribrain_mage_kata_de_alias_norm_idx
    ON bribrain_mage_kata_data_element_aliases (normalized_alias);

CREATE INDEX IF NOT EXISTS bribrain_mage_kata_de_cache_status_idx
    ON bribrain_mage_kata_data_elements_cache (LOWER(status));

-- 1c. Technical relations -> used by
--     search_active_kata_data_elements_by_technical_relation().
CREATE TABLE IF NOT EXISTS bribrain_mage_kata_technical_relations_cache (
    relation_id            TEXT PRIMARY KEY,
    data_element_id        TEXT NOT NULL,
    data_type              TEXT NULL,
    status                 TEXT NULL,
    resource               TEXT NULL,
    resource_name          TEXT NULL,
    resource_edc_id        TEXT NULL,
    database_name          TEXT NULL,
    database_edc_id        TEXT NULL,
    table_name             TEXT NOT NULL,
    normalized_table_name  TEXT NOT NULL,
    table_edc_id           TEXT NULL,
    field_name             TEXT NOT NULL,
    normalized_field_name  TEXT NOT NULL,
    field_edc_id           TEXT NULL,
    timestamp_created      TEXT NULL,
    timestamp_modified     TEXT NULL,
    row_hash               TEXT NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_sync_id      TEXT NULL
);

CREATE INDEX IF NOT EXISTS bribrain_mage_kata_relation_lookup_idx
    ON bribrain_mage_kata_technical_relations_cache
        (normalized_table_name, normalized_field_name);

CREATE INDEX IF NOT EXISTS bribrain_mage_kata_relation_element_idx
    ON bribrain_mage_kata_technical_relations_cache (data_element_id);


-- ============================================================================
-- SECTION 2 — Confluence discovery persistence
-- (Only for the PRODUCTION ConfluenceDiscoveryService. The research port runs
--  discovery in-memory and does NOT need these tables. Included for parity.)
-- ============================================================================

CREATE TABLE IF NOT EXISTS confluence_discovery_runs (
    id             UUID PRIMARY KEY,
    table_name     TEXT NOT NULL,
    columns        JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_schema  TEXT NULL,
    source_system  TEXT NULL,
    query_terms    JSONB NOT NULL DEFAULT '[]'::jsonb,
    status         TEXT NOT NULL DEFAULT 'completed',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS confluence_discovery_candidates (
    id                   UUID PRIMARY KEY,
    run_id               UUID NULL
        REFERENCES confluence_discovery_runs(id) ON DELETE CASCADE,
    page_id              TEXT NULL,
    title                TEXT NULL,
    url                  TEXT NULL,
    label                TEXT NOT NULL,
    confidence           DOUBLE PRECISION NOT NULL DEFAULT 0,
    score                DOUBLE PRECISION NOT NULL DEFAULT 0,
    reason               TEXT NULL,
    matched_terms        JSONB NOT NULL DEFAULT '[]'::jsonb,
    snippet              TEXT NULL,
    verification_status  TEXT NOT NULL DEFAULT 'heuristic_only',
    extracted_fields     JSONB NOT NULL DEFAULT '[]'::jsonb,
    filtered_fields      JSONB NOT NULL DEFAULT '[]'::jsonb,
    body_hash            TEXT NULL,
    source_schema        TEXT NULL,
    source_system        TEXT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Runtime migration keeps this column additive on pre-existing installs.
ALTER TABLE confluence_discovery_candidates
    ADD COLUMN IF NOT EXISTS filtered_fields JSONB NOT NULL DEFAULT '[]'::jsonb;

CREATE INDEX IF NOT EXISTS idx_confluence_discovery_candidates_page_id
    ON confluence_discovery_candidates(page_id);


-- ============================================================================
-- NOTE — bribrain_mage_knowledges_main is NOT created here.
-- The Confluence curation step (ConfluenceDiscoveryRepository.upsert_knowledge)
-- writes into bribrain_mage_knowledges_main, but that is the PRE-EXISTING main
-- knowledge table (present in base ms-bribrain-mage as well), not a new rafi-ver
-- table. The research port never writes to it. Create/manage it via its own
-- existing migration, not this file.
-- ============================================================================
