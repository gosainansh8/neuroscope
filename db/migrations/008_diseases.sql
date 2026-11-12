-- Several diseases. A trial belongs to a disease when that disease's ClinicalTrials.gov condition
-- query returned it (recorded at ingestion), not by guessing from its condition text.
-- Idempotent: init runs every migration every time.
CREATE TABLE IF NOT EXISTS diseases (
    slug                TEXT PRIMARY KEY,            -- alzheimers | parkinsons | als
    name                TEXT NOT NULL,               -- shown to people and the model
    ct_condition        TEXT NOT NULL,               -- ClinicalTrials.gov query.cond
    pubmed_term         TEXT NOT NULL,               -- added to each drug's PubMed search
    indication_pattern  TEXT NOT NULL,               -- case-insensitive regex for label/indication text
    sort_order          INTEGER NOT NULL DEFAULT 0
);
INSERT INTO diseases (slug, name, ct_condition, pubmed_term, indication_pattern, sort_order) VALUES
    ('alzheimers', 'Alzheimer''s disease', 'Alzheimer Disease', 'alzheimer*[Title/Abstract]', 'alzheimer', 1),
    ('parkinsons', 'Parkinson''s disease', 'Parkinson Disease', 'parkinson*[Title/Abstract]', 'parkinson', 2),
    ('als', 'ALS (amyotrophic lateral sclerosis)', 'Amyotrophic Lateral Sclerosis',
     '("amyotrophic lateral sclerosis"[Title/Abstract] OR "motor neuron disease"[Title/Abstract])',
     'amyotrophic lateral sclerosis|lou gehrig', 3)
ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, ct_condition = EXCLUDED.ct_condition,
    pubmed_term = EXCLUDED.pubmed_term, indication_pattern = EXCLUDED.indication_pattern,
    sort_order = EXCLUDED.sort_order;

CREATE TABLE IF NOT EXISTS trial_diseases (
    nct_id       TEXT NOT NULL REFERENCES trials(nct_id) ON DELETE CASCADE,
    disease      TEXT NOT NULL REFERENCES diseases(slug),
    first_run_id INTEGER REFERENCES ingestion_runs(id) ON DELETE SET NULL,
    PRIMARY KEY (nct_id, disease)
);
CREATE INDEX IF NOT EXISTS trial_diseases_disease_idx ON trial_diseases (disease);

-- Before this migration every trial came from the Alzheimer's query. Tag them once: only when
-- no trial has a tag yet, so a later run never re-tags a Parkinson's trial as Alzheimer's.
INSERT INTO trial_diseases (nct_id, disease)
SELECT t.nct_id, 'alzheimers' FROM trials t
WHERE NOT EXISTS (SELECT 1 FROM trial_diseases)
  AND EXISTS (SELECT 1 FROM ingestion_runs r WHERE r.query LIKE 'cond:Alzheimer%' AND r.data_origin <> 'synthetic')
ON CONFLICT DO NOTHING;

-- The pipeline for each disease: v_drug_pipeline restricted to that disease's trials.
CREATE OR REPLACE VIEW v_drug_disease_pipeline AS
SELECT
    d.id                                   AS drug_id,
    d.canonical_name,
    td.disease,
    MAX(t.highest_phase)                   AS highest_phase_any,
    MAX(t.highest_phase) FILTER (WHERE t.status_group = 'active') AS highest_phase_active,
    COUNT(DISTINCT t.nct_id)               AS n_trials,
    COUNT(DISTINCT t.nct_id) FILTER (WHERE t.status_group = 'active') AS n_active_trials,
    MAX(t.last_update_posted)              AS latest_source_update,
    d.is_class
FROM drugs d
JOIN trial_interventions ti ON ti.drug_id = d.id
JOIN trials t ON t.nct_id = ti.nct_id
JOIN trial_diseases td ON td.nct_id = t.nct_id
WHERE t.study_type = 'INTERVENTIONAL'
GROUP BY d.id, td.disease;

-- A registry search also returns off-topic studies through synonyms ("PD" matches PD-1 cancer
-- trials; "AD" and "APP" match app studies). A study counts toward a disease only if its conditions,
-- keywords, or titles match topic_pattern (a Python regular expression, case-insensitive).
ALTER TABLE diseases ADD COLUMN IF NOT EXISTS topic_pattern TEXT;
UPDATE diseases SET topic_pattern = CASE slug
    WHEN 'alzheimers' THEN 'alzheimer|dementia|amyloid|\btau\b|cognitive (impairment|decline)|\bMCI\b'
    WHEN 'parkinsons' THEN 'parkinson'
    WHEN 'als' THEN 'amyotrophic|\bALS\b|motor neuron|lou gehrig|charcot' END
WHERE topic_pattern IS DISTINCT FROM CASE slug
    WHEN 'alzheimers' THEN 'alzheimer|dementia|amyloid|\btau\b|cognitive (impairment|decline)|\bMCI\b'
    WHEN 'parkinsons' THEN 'parkinson'
    WHEN 'als' THEN 'amyotrophic|\bALS\b|motor neuron|lou gehrig|charcot' END;
