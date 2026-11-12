#!/usr/bin/env bash
# Copy the local database (Docker) into the cloud database. Replaces what is there.
# Needs the local database running (docker compose up -d) and the cloud instance started.
#
# Refuses if the cloud copy has data newer than the local one (for example from the weekly
# refresh), because copying would roll it back. Pass --force to copy anyway.
set -euo pipefail
cd "$(dirname "$0")/.." && source deploy/config.sh

# Check the local database first: a copy that starts without it can leave the cloud half-empty.
if ! docker info >/dev/null 2>&1; then
    echo "Docker is not running. Start Docker Desktop, then: docker compose up -d" >&2; exit 1
fi
if ! docker compose exec -T db pg_isready -U postgres >/dev/null 2>&1; then
    echo "The local database is not running. Start it with: docker compose up -d" >&2; exit 1
fi
LOCAL_TRIALS="$(docker compose exec -T db psql -U postgres -d neuroscope -At -c "SELECT count(*) FROM trials")"
if [ "${LOCAL_TRIALS:-0}" -lt 1 ]; then
    echo "The local database has no trials; refusing to replace the cloud copy with it." >&2; exit 1
fi

source deploy/_proxy.sh
TARGET="$CLOUD_URL_DOCKER"

LATEST="SELECT COALESCE(MAX(started_at), '-infinity') FROM ingestion_runs WHERE status = 'succeeded'"
LOCAL_LATEST="$(docker compose exec -T db psql -U postgres -d neuroscope -At -c "$LATEST")"
CLOUD_LATEST="$(docker compose exec -T db psql -At "$TARGET" -c "SELECT CASE WHEN to_regclass('ingestion_runs') IS NULL THEN '-infinity' ELSE ($LATEST)::text END" 2>/dev/null || echo "-infinity")"
NEWER="$(docker compose exec -T db psql -U postgres -At -c "SELECT '${CLOUD_LATEST}'::timestamptz > '${LOCAL_LATEST}'::timestamptz")"
if [ "$NEWER" = "t" ] && [ "${1:-}" != "--force" ]; then
    echo "The cloud database has newer data (last ingestion ${CLOUD_LATEST}) than the local one (${LOCAL_LATEST})."
    echo "Copying would roll it back. Review in the cloud with deploy/cloud.sh instead, or re-run with --force."
    exit 1
fi

echo "Resetting the cloud database and copying local data (a few minutes)..."
docker compose exec -T db psql -q "$TARGET" -v ON_ERROR_STOP=1 \
    -c "DROP SCHEMA IF EXISTS public CASCADE" -c "CREATE SCHEMA public"
docker compose exec -T db sh -c \
    "pg_dump -U postgres --no-owner --no-privileges neuroscope | psql -q -v ON_ERROR_STOP=1 '$TARGET'" >/dev/null

echo "Row counts, local vs cloud:"
COUNTS="SELECT 'trials', count(*) FROM trials UNION ALL SELECT 'drugs', count(*) FROM drugs UNION ALL SELECT 'interventions', count(*) FROM trial_interventions UNION ALL SELECT 'fda_applications', count(*) FROM fda_applications UNION ALL SELECT 'fda_labels', count(*) FROM fda_labels UNION ALL SELECT 'ema_medicines', count(*) FROM ema_medicines UNION ALL SELECT 'drug_mechanisms', count(*) FROM drug_mechanisms UNION ALL SELECT 'outcome_terms', count(*) FROM trial_outcome_terms UNION ALL SELECT 'documents', count(*) FROM documents UNION ALL SELECT 'chunks_with_embeddings', count(*) FROM chunks WHERE embedding IS NOT NULL ORDER BY 1"
LOCAL="$(docker compose exec -T db psql -U postgres -d neuroscope -At -c "$COUNTS")"
CLOUD="$(docker compose exec -T db psql -At "$TARGET" -c "$COUNTS")"
paste <(echo "$LOCAL") <(echo "$CLOUD" | cut -d'|' -f2) | column -t
[ "$LOCAL" = "$CLOUD" ] && echo "Identical. Next: deploy/3_deploy_service.sh" || { echo "MISMATCH: the copy is incomplete."; exit 1; }
