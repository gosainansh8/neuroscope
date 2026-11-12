#!/usr/bin/env bash
# Run any NeuroScope command against the CLOUD database, from this machine.
#   deploy/cloud.sh python -m neuroscope.curate unreviewed
#   deploy/cloud.sh python -m neuroscope.curate apply data/curation/some_file.csv --dry-run
#   deploy/cloud.sh python -m neuroscope.report
# Once the weekly refresh runs, the cloud database is the newest copy: review new names here.
set -euo pipefail
cd "$(dirname "$0")/.." && source deploy/config.sh
[ $# -gt 0 ] || { echo "Usage: deploy/cloud.sh <command> [args...]"; exit 2; }
source deploy/_proxy.sh
[ -x .venv/bin/python ] && export PATH="$PWD/.venv/bin:$PATH"
DATABASE_URL="$CLOUD_URL" "$@"
