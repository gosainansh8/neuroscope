#!/usr/bin/env bash
# Schedule the weekly refresh: a Cloud Run job that runs `python -m neuroscope.refresh`
# against the cloud database, triggered every Monday at 06:00 UTC by Cloud Scheduler.
#
#   deploy/4_schedule_refresh.sh            create or update the job and the schedule
#   deploy/4_schedule_refresh.sh --run-now  ...and run it once now, waiting for the result
#
# Re-run after deploy/3_deploy_service.sh so the job uses the newly deployed code.
# COST: a few minutes of compute a week, plus Vertex AI embeddings for new abstracts (cents).
# Cloud Scheduler includes 3 free jobs per billing account.
set -euo pipefail
cd "$(dirname "$0")/.." && source deploy/config.sh
JOB="neuroscope-refresh"
SCHEDULE_NAME="neuroscope-weekly-refresh"

gcloud services enable cloudscheduler.googleapis.com --project "$PROJECT_ID"

# The job runs the same image as the website, so it always has the same code.
IMAGE="$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
    --format='value(spec.template.spec.containers[0].image)')"
[ -n "$IMAGE" ] || { echo "Deploy the service first (deploy/3_deploy_service.sh)."; exit 1; }

gcloud run jobs deploy "$JOB" --project "$PROJECT_ID" --region "$REGION" --image "$IMAGE" \
    --service-account "$SERVICE_ACCOUNT_EMAIL" \
    --set-cloudsql-instances "$CONNECTION_NAME" \
    --set-secrets "DATABASE_URL=${SECRET}:latest" \
    --set-env-vars "GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},SEC_USER_AGENT=${SEC_USER_AGENT:-}" \
    --command python --args=-m,neuroscope.refresh \
    --memory 1Gi --cpu 1 --task-timeout 3600 --max-retries 0 --quiet

# The scheduler calls the job as the service account, which may run only this job.
gcloud run jobs add-iam-policy-binding "$JOB" --project "$PROJECT_ID" --region "$REGION" \
    --member "serviceAccount:$SERVICE_ACCOUNT_EMAIL" --role roles/run.invoker --quiet >/dev/null

URI="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB}:run"
ACTION=create
gcloud scheduler jobs describe "$SCHEDULE_NAME" --project "$PROJECT_ID" --location "$REGION" >/dev/null 2>&1 && ACTION=update
gcloud scheduler jobs "$ACTION" http "$SCHEDULE_NAME" --project "$PROJECT_ID" --location "$REGION" \
    --schedule "0 6 * * 1" --time-zone "Etc/UTC" --uri "$URI" --http-method POST \
    --oauth-service-account-email "$SERVICE_ACCOUNT_EMAIL" \
    --oauth-token-scope "https://www.googleapis.com/auth/cloud-platform" --quiet >/dev/null
echo "Scheduled: every Monday 06:00 UTC ($ACTION)."

if [ "${1:-}" = "--run-now" ]; then
    echo "Running the refresh now (several minutes)..."
    gcloud run jobs execute "$JOB" --project "$PROJECT_ID" --region "$REGION" --wait
fi
echo "Logs: Cloud console > Cloud Run > Jobs > $JOB. Past runs: deploy/cloud.sh python -m neuroscope.report"
