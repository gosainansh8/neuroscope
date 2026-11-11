-- Record which model produced each chunk embedding, and when.
-- Non-destructive and idempotent: adds two nullable columns.
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding_model TEXT;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedded_at TIMESTAMPTZ;
