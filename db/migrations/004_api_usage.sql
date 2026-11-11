-- Usage caps and an answer cache for the public API.
-- Non-destructive and idempotent: creates two small tables.

-- One row per day: how many questions reached the model. Enforces the daily and monthly caps.
CREATE TABLE IF NOT EXISTS api_usage (
    day        DATE PRIMARY KEY,
    questions  INTEGER NOT NULL DEFAULT 0
);

-- Answers to recently asked questions, so a repeated question costs nothing.
CREATE TABLE IF NOT EXISTS api_answer_cache (
    question_key TEXT PRIMARY KEY,           -- sha256 of the normalized question
    question     TEXT NOT NULL,
    response     JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
