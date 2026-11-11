#!/usr/bin/env bash
# Build the container and deploy the website and API to Cloud Run. Re-run to ship code changes.
#
#   deploy/3_deploy_service.sh            private: only you, through deploy/open.sh or deploy/ask.sh
#   deploy/3_deploy_service.sh --public   anyone with the link can open the page and ask questions
#
# A public service is protected by question caps (set below); see deploy/README.md.
set -euo pipefail
cd "$(dirname "$0")/.." && source deploy/config.sh

ACCESS="--no-allow-unauthenticated"
[ "${1:-}" = "--public" ] && ACCESS="--allow-unauthenticated"
DAILY="${NEUROSCOPE_DAILY_QUESTION_LIMIT:-50}"
MONTHLY="${NEUROSCOPE_MONTHLY_QUESTION_LIMIT:-500}"
VISITOR="${NEUROSCOPE_VISITOR_LIMIT:-5}"
echo "Access: ${ACCESS#--}   Caps: $DAILY questions/day, $MONTHLY/month, $VISITOR per visitor per 10 minutes"

gcloud run deploy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --source . \
    --service-account "$SERVICE_ACCOUNT_EMAIL" \
    --add-cloudsql-instances "$CONNECTION_NAME" \
    --set-secrets "DATABASE_URL=${SECRET}:latest" \
    --set-env-vars "GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},NEUROSCOPE_DAILY_QUESTION_LIMIT=${DAILY},NEUROSCOPE_MONTHLY_QUESTION_LIMIT=${MONTHLY},NEUROSCOPE_VISITOR_LIMIT=${VISITOR}" \
    $ACCESS \
    --memory 512Mi --cpu 1 --min-instances 0 --max-instances 1 --timeout 180 --quiet

echo "Deployed: $(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"
if [ "$ACCESS" = "--allow-unauthenticated" ]; then
    echo "Public: anyone can open that address."
else
    echo "Private: open it with deploy/open.sh, or ask with deploy/ask.sh \"your question\""
fi
