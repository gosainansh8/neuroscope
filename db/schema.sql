-- NeuroScope database schema (Postgres 16 + pgvector)
-- One database holds both the structured trial data and the document embeddings.
-- Safe to re-run: every statement is idempotent.
-- Changes to existing tables go in db/migrations/, applied after this file by `db init`.

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Drugs and aliases
-- A drug is one canonical entity. Every name it goes by (code name, INN,
-- brand name, spelling variant) is a row in drug_aliases pointing at it.
-- alias_key is the normalized lookup key (lowercase, alphanumerics only),
-- so "BAN-2401", "BAN 2401" and "ban2401" all resolve to the same row.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS drugs (
    id              SERIAL PRIMARY KEY,
    canonical_name  TEXT NOT NULL UNIQUE,
    mechanism       TEXT,              -- e.g. 'anti-amyloid antibody'; curated, nullable
    modality        TEXT,              -- e.g. 'monoclonal antibody', 'small molecule'
    is_seeded       BOOLEAN NOT NULL DEFAULT FALSE,  -- came from the curated seed list
    is_combination  BOOLEAN NOT NULL DEFAULT FALSE,  -- e.g. 'donepezil + memantine'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- review_status:
--   verified   = curated (seed) or confirmed by a human; trusted
--   unreviewed = learned from a registry record; links only the trial it came from
--                (first_seen_nct). Elsewhere it is a review candidate: the
--                intervention stays unlinked as 'pending_review' until verified.
--   rejected   = tombstone; never matched and never re-learned
CREATE TABLE IF NOT EXISTS drug_aliases (
    alias_key       TEXT PRIMARY KEY,
    alias_display   TEXT NOT NULL,
    drug_id         INTEGER REFERENCES drugs(id) ON DELETE CASCADE,  -- NULL only when rejected
    alias_type      TEXT NOT NULL CHECK (alias_type IN ('inn', 'code', 'brand', 'combination', 'other')),
    source          TEXT NOT NULL CHECK (source IN ('seed', 'manual', 'ctgov_name', 'ctgov_parenthetical', 'ctgov_other_name')),
    review_status   TEXT NOT NULL DEFAULT 'unreviewed' CHECK (review_status IN ('verified', 'unreviewed', 'rejected')),
    first_seen_nct  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at     TIMESTAMPTZ,
    CHECK (review_status = 'rejected' OR drug_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_drug_aliases_drug ON drug_aliases(drug_id);

-- A combination is its own drug. Components are linked here only when a
-- component name exactly matches an existing non-rejected alias.
CREATE TABLE IF NOT EXISTS drug_components (
    drug_id           INTEGER NOT NULL REFERENCES drugs(id) ON DELETE CASCADE,
    component_drug_id INTEGER NOT NULL REFERENCES drugs(id) ON DELETE CASCADE,
    PRIMARY KEY (drug_id, component_drug_id)
);

-- Every manual correction is recorded, so the alias table's history is auditable.
CREATE TABLE IF NOT EXISTS alias_corrections (
    id            SERIAL PRIMARY KEY,
    action        TEXT NOT NULL CHECK (action IN ('verify', 'reject', 'map', 'resolve_conflict', 'reresolve')),
    alias_key     TEXT,
    old_drug_id   INTEGER,
    new_drug_id   INTEGER,
    note          TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- When one intervention's names point at two different existing drugs, we do
-- NOT silently merge them. The conflict is logged here for human review.
CREATE TABLE IF NOT EXISTS drug_alias_conflicts (
    id                SERIAL PRIMARY KEY,
    nct_id            TEXT NOT NULL,
    intervention_name TEXT NOT NULL,
    alias_key         TEXT NOT NULL,
    assigned_drug_id  INTEGER REFERENCES drugs(id) ON DELETE SET NULL,   -- NULL = left unlinked
    conflicting_drug_id INTEGER REFERENCES drugs(id) ON DELETE SET NULL,
    detected_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved          BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (nct_id, alias_key, conflicting_drug_id)
);

-- ---------------------------------------------------------------------------
-- Sponsors
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sponsors (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    class       TEXT          -- CT.gov class: INDUSTRY, NIH, OTHER_GOV, OTHER, ...
);

-- ---------------------------------------------------------------------------
-- Trials (current state). History lives in trial_snapshots.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trials (
    nct_id                    TEXT PRIMARY KEY,
    brief_title               TEXT NOT NULL,
    official_title            TEXT,
    study_type                TEXT,              -- INTERVENTIONAL, OBSERVATIONAL, EXPANDED_ACCESS
    overall_status            TEXT NOT NULL,     -- raw CT.gov status, e.g. RECRUITING
    status_group              TEXT NOT NULL CHECK (status_group IN ('active', 'completed', 'stopped', 'unknown', 'other')),
    why_stopped               TEXT,
    phases                    TEXT[] NOT NULL DEFAULT '{}',  -- raw, e.g. {PHASE2,PHASE3}
    phase_label               TEXT NOT NULL,     -- display, e.g. 'Phase 2/3'
    highest_phase             NUMERIC(2,1),      -- 0.5 = early phase 1, 1..4; NULL = not applicable
    enrollment                INTEGER,
    enrollment_type           TEXT,              -- ACTUAL or ESTIMATED
    allocation                TEXT,
    masking                   TEXT,
    start_date                DATE,
    start_date_precision      TEXT CHECK (start_date_precision IN ('day', 'month', 'year')),
    primary_completion_date   DATE,
    primary_completion_precision TEXT CHECK (primary_completion_precision IN ('day', 'month', 'year')),
    completion_date           DATE,
    completion_precision      TEXT CHECK (completion_precision IN ('day', 'month', 'year')),
    last_update_posted        DATE,              -- source date: when CT.gov last changed the record
    has_results               BOOLEAN NOT NULL DEFAULT FALSE,
    lead_sponsor_id           INTEGER REFERENCES sponsors(id),
    conditions                TEXT[] NOT NULL DEFAULT '{}',
    keywords                  TEXT[] NOT NULL DEFAULT '{}',
    content_hash              TEXT NOT NULL,
    data_origin               TEXT NOT NULL CHECK (data_origin IN ('live_api', 'replay', 'synthetic')),
    first_ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_retrieved_at         TIMESTAMPTZ NOT NULL DEFAULT now()  -- retrieval date: when we last pulled it
);
CREATE INDEX IF NOT EXISTS idx_trials_status_group ON trials(status_group);
CREATE INDEX IF NOT EXISTS idx_trials_highest_phase ON trials(highest_phase);
CREATE INDEX IF NOT EXISTS idx_trials_sponsor ON trials(lead_sponsor_id);

CREATE TABLE IF NOT EXISTS trial_collaborators (
    nct_id      TEXT NOT NULL REFERENCES trials(nct_id) ON DELETE CASCADE,
    sponsor_id  INTEGER NOT NULL REFERENCES sponsors(id),
    PRIMARY KEY (nct_id, sponsor_id)
);

CREATE TABLE IF NOT EXISTS trial_interventions (
    id                SERIAL PRIMARY KEY,
    nct_id            TEXT NOT NULL REFERENCES trials(nct_id) ON DELETE CASCADE,
    name              TEXT NOT NULL,         -- exactly as registered
    intervention_type TEXT,                  -- DRUG, BIOLOGICAL, BEHAVIORAL, DEVICE, ...
    description       TEXT,
    other_names       TEXT[] NOT NULL DEFAULT '{}',
    arm_group_labels  TEXT[] NOT NULL DEFAULT '{}',
    is_placebo        BOOLEAN NOT NULL DEFAULT FALSE,
    drug_id           INTEGER REFERENCES drugs(id),  -- NULL for placebo and non-drug interventions
    -- How drug_id was decided, so every link can be explained and audited:
    --   matched = exact alias key match; created = new drug; conflict = names pointed
    --   at 2+ drugs (see drug_alias_conflicts); pending_review = only an unreviewed alias
    --   from another trial matched (matched_alias_key names it); ambiguous_name = the
    --   name lists alternatives ("A or B"); placebo / non_drug / no_usable_name = unlinked.
    -- Allowed values are enforced by db/migrations/001_resolution_method_values.sql.
    resolution_method TEXT NOT NULL DEFAULT 'non_drug',
    matched_alias_key TEXT
);
CREATE INDEX IF NOT EXISTS idx_interventions_nct ON trial_interventions(nct_id);
CREATE INDEX IF NOT EXISTS idx_interventions_drug ON trial_interventions(drug_id);

CREATE TABLE IF NOT EXISTS trial_outcomes (
    id          SERIAL PRIMARY KEY,
    nct_id      TEXT NOT NULL REFERENCES trials(nct_id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('primary', 'secondary')),
    measure     TEXT NOT NULL,
    description TEXT,
    time_frame  TEXT
);
CREATE INDEX IF NOT EXISTS idx_outcomes_nct ON trial_outcomes(nct_id);

-- ---------------------------------------------------------------------------
-- History and change tracking
-- A snapshot is written only when a trial's content hash changes, so this
-- table is the full version history of every record we have seen.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id            SERIAL PRIMARY KEY,
    query         TEXT NOT NULL,
    data_origin   TEXT NOT NULL CHECK (data_origin IN ('live_api', 'replay', 'synthetic')),
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    status        TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed')),
    n_fetched     INTEGER NOT NULL DEFAULT 0,
    n_new         INTEGER NOT NULL DEFAULT 0,
    n_changed     INTEGER NOT NULL DEFAULT 0,
    n_unchanged   INTEGER NOT NULL DEFAULT 0,
    n_failed      INTEGER NOT NULL DEFAULT 0,
    error         TEXT
);

CREATE TABLE IF NOT EXISTS trial_snapshots (
    nct_id              TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    run_id              INTEGER REFERENCES ingestion_runs(id),
    last_update_posted  DATE,
    retrieved_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw                 JSONB NOT NULL,
    PRIMARY KEY (nct_id, content_hash)
);

CREATE TABLE IF NOT EXISTS trial_changes (
    id           SERIAL PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES ingestion_runs(id),
    nct_id       TEXT NOT NULL,
    field        TEXT NOT NULL,    -- 'new_trial', 'overall_status', 'phase_label', 'enrollment', ...
    old_value    TEXT,
    new_value    TEXT,
    detected_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_changes_detected ON trial_changes(detected_at);
CREATE INDEX IF NOT EXISTS idx_changes_nct ON trial_changes(nct_id);

-- ---------------------------------------------------------------------------
-- Documents and chunks for RAG (filled in the next build step)
-- 768 dims matches Vertex AI text-embedding-005.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    id              SERIAL PRIMARY KEY,
    source_type     TEXT NOT NULL CHECK (source_type IN ('pubmed', 'press_release', 'fda_label', 'sec_filing', 'other')),
    title           TEXT NOT NULL,
    url             TEXT,
    external_id     TEXT,             -- PMID, accession number, etc.
    published_date  DATE,             -- source date
    retrieved_at    TIMESTAMPTZ NOT NULL DEFAULT now(),  -- retrieval date
    content_hash    TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS document_drugs (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    drug_id     INTEGER NOT NULL REFERENCES drugs(id) ON DELETE CASCADE,
    PRIMARY KEY (document_id, drug_id)
);

CREATE TABLE IF NOT EXISTS chunks (
    id           SERIAL PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,
    text         TEXT NOT NULL,
    token_count  INTEGER,
    embedding    vector(768),
    UNIQUE (document_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- Convenience view: one row per drug with its pipeline position.
-- Agents query this instead of re-deriving it.
--
-- IMPORTANT: these are TRIAL phases, not regulatory status. A completed
-- Phase 3 does not mean approved, and a Phase 4 trial is not proof of
-- approval for this indication. Approval will come only from FDA records
-- in a separate table, never from this view.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_drug_pipeline AS
SELECT
    d.id                                   AS drug_id,
    d.canonical_name,
    d.mechanism,
    d.modality,
    MAX(t.highest_phase)                   AS highest_phase_any,
    MAX(t.highest_phase) FILTER (WHERE t.status_group = 'active') AS highest_phase_active,
    COUNT(DISTINCT t.nct_id)               AS n_trials,
    COUNT(DISTINCT t.nct_id) FILTER (WHERE t.status_group = 'active') AS n_active_trials,
    ARRAY_AGG(DISTINCT s.name) FILTER (WHERE s.name IS NOT NULL) AS lead_sponsors,
    MAX(t.last_update_posted)              AS latest_source_update
FROM drugs d
JOIN trial_interventions ti ON ti.drug_id = d.id
JOIN trials t ON t.nct_id = ti.nct_id
LEFT JOIN sponsors s ON s.id = t.lead_sponsor_id
WHERE t.study_type = 'INTERVENTIONAL'
GROUP BY d.id;
