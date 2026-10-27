-- Allow resolution_method values added after the first schema:
--   pending_review  an unreviewed alias from another trial matched; waits for review
--   ambiguous_name  the registered name lists alternatives ("A or B")
-- Non-destructive and idempotent: replaces only the CHECK constraint. Existing
-- rows already use a subset of the new values, so validation cannot fail.
ALTER TABLE trial_interventions
    DROP CONSTRAINT IF EXISTS trial_interventions_resolution_method_check;
ALTER TABLE trial_interventions
    ADD CONSTRAINT trial_interventions_resolution_method_check CHECK (resolution_method IN
        ('matched', 'created', 'conflict', 'pending_review', 'ambiguous_name',
         'placebo', 'non_drug', 'no_usable_name'));
