-- Keyword search over document chunks, and date precision for documents.
-- Non-destructive and idempotent: adds columns and an index only.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS journal TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS published_date_precision TEXT
    CHECK (published_date_precision IN ('day', 'month', 'year'));
-- external_id is the PMID for PubMed records; one row per source record.
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_source_external
    ON documents (source_type, external_id) WHERE external_id IS NOT NULL;
-- How a document was linked to a drug: the trusted name found in its text.
ALTER TABLE document_drugs ADD COLUMN IF NOT EXISTS matched_name TEXT;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', text)) STORED;
CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING gin (tsv);
