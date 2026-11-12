-- A drug program = one drug in one disease, judged from its interventional trials.
--   active        at least one trial is recruiting, enrolling, or ongoing
--   discontinued  no active trial, and the most recently updated trial was terminated, withdrawn, or suspended
--   dormant       no active trial, and no trial updated in the last 3 years
--   completed     no active trial; the latest trial completed (or its status is unknown) within 3 years
-- This describes the registry record, not a company decision: a stopped trial can be replaced by one not
-- yet registered, and a company can end a program without terminating a trial. Idempotent.
CREATE OR REPLACE VIEW v_program_status AS
WITH t AS (
    SELECT DISTINCT ti.drug_id, td.disease, tr.nct_id, tr.status_group, tr.overall_status, tr.highest_phase,
           tr.why_stopped, tr.last_update_posted
    FROM trial_interventions ti
    JOIN trials tr ON tr.nct_id = ti.nct_id
    JOIN trial_diseases td ON td.nct_id = tr.nct_id
    WHERE tr.study_type = 'INTERVENTIONAL' AND ti.drug_id IS NOT NULL
),
latest AS (
    SELECT DISTINCT ON (drug_id, disease) drug_id, disease, nct_id, status_group, overall_status, why_stopped,
           last_update_posted, highest_phase
    FROM t WHERE status_group <> 'active'
    ORDER BY drug_id, disease, last_update_posted DESC NULLS LAST, nct_id
),
agg AS (
    SELECT drug_id, disease,
           COUNT(DISTINCT nct_id) AS n_trials,
           COUNT(DISTINCT nct_id) FILTER (WHERE status_group = 'active') AS n_active,
           COUNT(DISTINCT nct_id) FILTER (WHERE status_group = 'stopped') AS n_stopped,
           MAX(highest_phase) AS highest_phase,
           MAX(last_update_posted) AS last_update
    FROM t GROUP BY drug_id, disease
)
SELECT d.id AS drug_id, d.canonical_name, a.disease, a.n_trials, a.n_active, a.n_stopped, a.highest_phase,
       a.last_update,
       l.nct_id AS latest_closed_nct, l.overall_status AS latest_closed_status, l.why_stopped AS latest_why_stopped,
       l.highest_phase AS latest_closed_phase,
       CASE WHEN a.n_active > 0 THEN 'active'
            WHEN l.status_group = 'stopped' THEN 'discontinued'
            WHEN a.last_update < current_date - INTERVAL '3 years' THEN 'dormant'
            ELSE 'completed' END AS program_status,
       EXISTS (SELECT 1 FROM fda_products p JOIN fda_applications fa ON fa.application_number = p.application_number
               WHERE p.drug_id = d.id AND fa.approval_status = 'approved') AS fda_approved,
       EXISTS (SELECT 1 FROM ema_medicines e WHERE e.drug_id = d.id AND e.medicine_status = 'Authorised')
           AS ema_authorised
FROM agg a JOIN drugs d ON d.id = a.drug_id
LEFT JOIN latest l ON l.drug_id = a.drug_id AND l.disease = a.disease
WHERE NOT d.is_class;
