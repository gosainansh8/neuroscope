#!/usr/bin/env bash
# Start the cloud database. It is billed by the hour while running.
set -euo pipefail
cd "$(dirname "$0")/.." && source deploy/config.sh
gcloud sql instances patch "$SQL_INSTANCE" --project "$PROJECT_ID" --activation-policy=ALWAYS --quiet
echo "Database started. Stop it when you are done: deploy/stop.sh"
