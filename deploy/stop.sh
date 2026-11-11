#!/usr/bin/env bash
# Stop the cloud database. Data is kept; only storage is billed while stopped.
# The API stays deployed (it costs nothing idle) and answers 503 until the database is started.
set -euo pipefail
cd "$(dirname "$0")/.." && source deploy/config.sh
gcloud sql instances patch "$SQL_INSTANCE" --project "$PROJECT_ID" --activation-policy=NEVER --quiet
echo "Database stopped. Start it again with deploy/start.sh"
