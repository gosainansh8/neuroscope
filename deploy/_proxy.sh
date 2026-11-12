# Start the Cloud SQL proxy and set CLOUD_URL (for use from this machine) and
# CLOUD_URL_DOCKER (for use from inside the local database container).
# Source this after deploy/config.sh; the proxy stops when the calling script exits.
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
_SECRET_URL="$(gcloud secrets versions access latest --secret "$SECRET" --project "$PROJECT_ID")"
_PASSWORD="$(printf '%s' "$_SECRET_URL" | sed -E 's#^postgresql://[^:]+:([^@]+)@.*#\1#')"
CLOUD_URL="postgresql://${DB_USER}:${_PASSWORD}@127.0.0.1:${PROXY_PORT}/${DB_NAME}"
CLOUD_URL_DOCKER="postgresql://${DB_USER}:${_PASSWORD}@host.docker.internal:${PROXY_PORT}/${DB_NAME}"
unset _SECRET_URL _PASSWORD
