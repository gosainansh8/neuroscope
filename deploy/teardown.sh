#!/usr/bin/env bash
# Delete the cloud deployment: the API, the database instance and ALL its data, and the secret.
# Your local database and this repository are not touched. This cannot be undone.
set -euo pipefail
cd "$(dirname "$0")/.." && source deploy/config.sh
read -r -p "Delete $SERVICE and database $SQL_INSTANCE in $PROJECT_ID? Type the instance name to confirm: " answer
[ "$answer" = "$SQL_INSTANCE" ] || { echo "Not confirmed; nothing deleted."; exit 1; }
gcloud run services delete "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --quiet || true
gcloud sql instances delete "$SQL_INSTANCE" --project "$PROJECT_ID" --quiet || true
gcloud secrets delete "$SECRET" --project "$PROJECT_ID" --quiet || true
echo "Deleted. The service account $SERVICE_ACCOUNT_EMAIL is left in place (it costs nothing)."
