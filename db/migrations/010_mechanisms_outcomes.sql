-- Mechanisms of action from ChEMBL, and the standard endpoints and biomarkers named in trial outcomes.
-- Both tables are rebuilt by their loaders (python -m neuroscope.mechanisms load / neuroscope.outcomes classify).
-- Idempotent.
CREATE TABLE IF NOT EXISTS drug_mechanisms (
    drug_id             INTEGER NOT NULL REFERENCES drugs(id) ON DELETE CASCADE,
    chembl_id           TEXT NOT NULL,           -- the molecule matched (its parent, for salts)
    matched_name        TEXT NOT NULL,           -- the ChEMBL name equal to one of the drug's trusted names
    mechanism_of_action TEXT NOT NULL,           -- "Catechol O-methyltransferase inhibitor"
    action_type         TEXT,                    -- INHIBITOR, AGONIST, ...
    target_chembl_id    TEXT,
    target_name         TEXT,                    -- "Catechol O-methyltransferase"
    target_type         TEXT,
    retrieved_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (drug_id, chembl_id, mechanism_of_action)
);

CREATE TABLE IF NOT EXISTS trial_outcome_terms (
    nct_id   TEXT NOT NULL REFERENCES trials(nct_id) ON DELETE CASCADE,
    kind     TEXT NOT NULL CHECK (kind IN ('primary', 'secondary')),
    term     TEXT NOT NULL,                      -- a slug from neuroscope/outcomes.py, e.g. 'alsfrs_r'
    category TEXT NOT NULL CHECK (category IN ('clinical', 'biomarker', 'safety')),
    PRIMARY KEY (nct_id, kind, term)
);
CREATE INDEX IF NOT EXISTS trial_outcome_terms_term_idx ON trial_outcome_terms (term);
