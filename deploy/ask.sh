#!/usr/bin/env bash
# Ask the deployed API a question, signed in as you.   deploy/ask.sh "your question"
# With no argument, shows /health.
set -euo pipefail
cd "$(dirname "$0")/.." && source deploy/config.sh
URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"
TOKEN="$(gcloud auth print-identity-token)"
if [ $# -eq 0 ]; then
    curl -sS -H "Authorization: Bearer $TOKEN" "$URL/health"; echo
else
    BODY="$(python3 -c 'import json,sys; print(json.dumps({"question": sys.argv[1]}))' "$1")"
    curl -sS -X POST "$URL/ask" -H "Authorization: Bearer $TOKEN" -H "content-type: application/json" -d "$BODY" \
        | python3 -c 'import json,sys
d=json.load(sys.stdin)
if "answer" not in d: print(d); sys.exit(1)
for l in d["lookups"]: print("[lookup]", l["tool"], l["arguments"])
print(); print(d["answer"]); print()
print("Citations verified:", d["citations_verified"], "| unsupported:", sum(d["unsupported_citations"].values(), []))'
fi
