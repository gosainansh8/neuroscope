#!/usr/bin/env bash
# Apply a correction file to BOTH databases, so the live site and your local copy stay matched.
#
#   deploy/apply_corrections.sh data/curation/FILE.csv            dry-run on the cloud, ask, then apply to both
#   deploy/apply_corrections.sh data/curation/FILE.csv --dry-run  only show what would change
#   deploy/apply_corrections.sh data/curation/FILE.csv --yes      apply without asking
#
# Order: cloud dry run -> (confirm) -> cloud -> local -> local paper relink. The cloud goes first
# because it is what the live site uses. A file with an error stops at the dry run, so neither
# copy is half-updated. Cloud paper links are recomputed by the next weekly refresh.
set -euo pipefail
cd "$(dirname "$0")/.."
FILE="${1:-}"
[ -n "$FILE" ] && [ -f "$FILE" ] || { echo "Usage: deploy/apply_corrections.sh FILE.csv [--dry-run|--yes]"; exit 2; }
MODE="${2:-}"
set -a && source .env && set +a
[ -x .venv/bin/python ] && export PATH="$PWD/.venv/bin:$PATH"

docker compose exec -T db pg_isready -U postgres >/dev/null 2>&1 \
    || { echo "The local database is not running. Start it with: docker compose up -d"; exit 1; }

echo "== Dry run on the cloud database (nothing changes)"
deploy/cloud.sh python -m neuroscope.curate apply "$FILE" --dry-run
[ "$MODE" = "--dry-run" ] && exit 0

if [ "$MODE" != "--yes" ]; then
    read -r -p "Apply this file to the cloud and local databases? [y/N] " answer
    [ "$answer" = "y" ] || [ "$answer" = "Y" ] || { echo "Not applied."; exit 1; }
fi

echo "== Cloud"
deploy/cloud.sh python -m neuroscope.curate apply "$FILE"
echo "== Local"
python -m neuroscope.curate apply "$FILE"
python -m neuroscope.pubmed relink
echo "Applied to both databases. Cloud paper links update at the next weekly refresh"
echo "(or now: gcloud run jobs execute neuroscope-refresh --region ${NEUROSCOPE_REGION:-us-central1})."
