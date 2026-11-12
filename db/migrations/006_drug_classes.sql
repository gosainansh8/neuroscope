-- Drug classes ("cholinesterase inhibitors", "SSRI") are stored as drugs because trials register them
-- as interventions. drugs.is_class (added in schema.sql, before the views that use it) marks them;
-- this migration lets the marking be recorded in alias_corrections. Non-destructive and idempotent.
ALTER TABLE alias_corrections DROP CONSTRAINT IF EXISTS alias_corrections_action_check;
ALTER TABLE alias_corrections ADD CONSTRAINT alias_corrections_action_check CHECK (action IN
    ('verify', 'reject', 'map', 'resolve_conflict', 'reresolve', 'mark_class'));
