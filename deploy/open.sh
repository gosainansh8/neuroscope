#!/usr/bin/env bash
# Open the deployed web page in your browser.
# The service is private, so this runs a local proxy that signs each request as you.
# Leave it running while you use the page; press Ctrl-C to stop.
set -euo pipefail
cd "$(dirname "$0")/.." && source deploy/config.sh
PORT="${1:-8081}"
echo "Open http://localhost:${PORT}/ in your browser. Press Ctrl-C here to stop."
(sleep 3 && command -v open >/dev/null && open "http://localhost:${PORT}/") &
gcloud run services proxy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --port "$PORT"
