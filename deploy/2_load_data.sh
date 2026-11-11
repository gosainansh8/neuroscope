#!/usr/bin/env bash
# Copy the local database (Docker) into the cloud database. Replaces what is there.
# Needs the local database running (docker compose up -d) and the cloud instance started.
set -euo pipefail
cd "$(dirname "$0")/.." && source deploy/config.sh

mkdir -p deploy/.bin
PROXY="deploy/.bin/cloud-sql-proxy"
if [ ! -x "$PROXY" ]; then
    case "$(uname -s)-$(uname -m)" in
        Darwin-arm64) asset="cloud-sql-proxy.darwin.arm64" ;;
        Darwin-x86_64) asset="cloud-sql-proxy.darwin.amd64" ;;
        Linux-x86_64) asset="cloud-sql-proxy.linux.amd64" ;;
        *) echo "Unsupported platform for the Cloud SQL proxy download"; exit 1 ;;
    esac
    curl -fsSL -o "$PROXY" "https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/${PROXY_VERSION}/${asset}"
    chmod +x "$PROXY"
fi

# The proxy makes the cloud database reachable on a local port, over an encrypted, authenticated tunnel.
"$PROXY" --address 0.0.0.0 --port "$PROXY_PORT" "$CONNECTION_NAME" >/tmp/neuroscope-proxy.log 2>&1 &
PROXY_PID=$!
trap 'kill $PROXY_PID 2>/dev/null || true' EXIT
sleep 5
kill -0 $PROXY_PID 2>/dev/null || { echo "The proxy did not start:"; cat /tmp/neuroscope-proxy.log; exit 1; }

# Cloud URL for use from inside the local database container (which has pg_dump and psql).
SECRET_URL="$(gcloud secrets versions access latest --secret "$SECRET" --project "$PROJECT_ID")"
PASSWORD="$(printf '%s' "$SECRET_URL" | sed -E 's#^postgresql://[^:]+:([^@]+)@.*#\1#')"
TARGET="postgresql://${DB_USER}:${PASSWORD}@host.docker.internal:${PROXY_PORT}/${DB_NAME}"

echo "Resetting the cloud database and copying local data (a few minutes)..."
docker compose exec -T db psql -q "$TARGET" -v ON_ERROR_STOP=1 \
    -c "DROP SCHEMA IF EXISTS public CASCADE" -c "CREATE SCHEMA public"
docker compose exec -T db sh -c \
    "pg_dump -U postgres --no-owner --no-privileges neuroscope | psql -q -v ON_ERROR_STOP=1 '$TARGET'" >/dev/null
unset PASSWORD SECRET_URL

echo "Row counts, local vs cloud:"
COUNTS="SELECT 'trials', count(*) FROM trials UNION ALL SELECT 'drugs', count(*) FROM drugs UNION ALL SELECT 'interventions', count(*) FROM trial_interventions UNION ALL SELECT 'fda_applications', count(*) FROM fda_applications UNION ALL SELECT 'fda_labels', count(*) FROM fda_labels UNION ALL SELECT 'documents', count(*) FROM documents UNION ALL SELECT 'chunks_with_embeddings', count(*) FROM chunks WHERE embedding IS NOT NULL ORDER BY 1"
LOCAL="$(docker compose exec -T db psql -U postgres -d neuroscope -At -c "$COUNTS")"
CLOUD="$(docker compose exec -T db psql -At "$TARGET" -c "$COUNTS")"
paste <(echo "$LOCAL") <(echo "$CLOUD" | cut -d'|' -f2) | column -t
[ "$LOCAL" = "$CLOUD" ] && echo "Identical. Next: deploy/3_deploy_service.sh" || { echo "MISMATCH: the copy is incomplete."; exit 1; }
