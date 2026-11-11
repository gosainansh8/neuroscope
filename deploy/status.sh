#!/usr/bin/env bash
# Show whether the database is running (and so costing money) and where the API is.
set -euo pipefail
cd "$(dirname "$0")/.." && source deploy/config.sh
STATE="$(gcloud sql instances describe "$SQL_INSTANCE" --project "$PROJECT_ID" \
    --format='value(state,settings.activationPolicy,settings.tier)' 2>/dev/null || echo "not created")"
echo "Database $SQL_INSTANCE: $STATE"
case "$STATE" in *ALWAYS*) echo "  -> RUNNING and billed by the hour. Stop with deploy/stop.sh" ;;
                 *NEVER*)  echo "  -> stopped; only storage is billed" ;; esac
URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
    --format='value(status.url)' 2>/dev/null || true)"
echo "API: ${URL:-not deployed}"
