#!/usr/bin/env bash
# Build the container and deploy the API to Cloud Run. Re-run to ship code changes.
# The service is private: only callers allowed by the project's IAM can reach it.
set -euo pipefail
cd "$(dirname "$0")/.." && source deploy/config.sh

gcloud run deploy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --source . \
    --service-account "$SERVICE_ACCOUNT_EMAIL" \
    --add-cloudsql-instances "$CONNECTION_NAME" \
    --set-secrets "DATABASE_URL=${SECRET}:latest" \
    --set-env-vars "GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION}" \
    --no-allow-unauthenticated \
    --memory 512Mi --cpu 1 --min-instances 0 --max-instances 1 --timeout 180 --quiet

echo "Deployed: $(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"
echo "Try it:   deploy/ask.sh \"Is semaglutide approved for Alzheimer's disease?\""
