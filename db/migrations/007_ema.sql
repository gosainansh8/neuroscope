-- European Medicines Agency: medicines evaluated centrally for the whole EU (EPAR list).
-- One row per EMA medicine matched to a drug. `python -m neuroscope.ema load` replaces all rows.
-- Idempotent: init runs every migration every time.
CREATE TABLE IF NOT EXISTS ema_medicines (
    ema_product_number          TEXT PRIMARY KEY,            -- EMEA/H/C/005966
    name                        TEXT NOT NULL,               -- Leqembi
    medicine_status             TEXT NOT NULL,               -- Authorised, Withdrawn, Refused, Application withdrawn, ...
    inn                         TEXT,                        -- international non-proprietary name, as published
    active_substance            TEXT,
    therapeutic_area            TEXT,                        -- MeSH terms joined with ';'
    therapeutic_indication      TEXT,                        -- verbatim, HTML entities decoded
    indication_mentions_alzheimers BOOLEAN NOT NULL DEFAULT FALSE,   -- text search, a pointer only
    marketing_authorisation_holder TEXT,
    authorisation_date          DATE,                        -- marketing authorisation date
    refusal_date                DATE,
    application_withdrawal_date DATE,
    withdrawal_date             DATE,                        -- withdrawal, expiry, revocation or lapse after authorisation
    conditional_approval        BOOLEAN NOT NULL DEFAULT FALSE,
    generic                     BOOLEAN NOT NULL DEFAULT FALSE,
    biosimilar                  BOOLEAN NOT NULL DEFAULT FALSE,
    url                         TEXT,
    drug_id                     INTEGER NOT NULL REFERENCES drugs(id) ON DELETE CASCADE,
    matched_key                 TEXT NOT NULL,
    match_method                TEXT NOT NULL CHECK (match_method IN ('exact', 'normalized')),
    source_timestamp            TIMESTAMPTZ NOT NULL,        -- meta.timestamp of the EMA file
    raw                         JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ema_medicines_drug_idx ON ema_medicines (drug_id);
