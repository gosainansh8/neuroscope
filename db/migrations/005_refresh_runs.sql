-- One row per scheduled refresh (python -m neuroscope.refresh).
-- Non-destructive and idempotent: creates one small table.
CREATE TABLE IF NOT EXISTS refresh_runs (
    id           SERIAL PRIMARY KEY,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    status       TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'partial', 'failed')),
    steps        JSONB NOT NULL DEFAULT '[]'      -- [{"step": "trials", "ok": true, "summary": {...}}, ...]
);
