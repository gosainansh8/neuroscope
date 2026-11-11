#!/usr/bin/env bash
# One-time setup: APIs, the database instance, its password (kept in Secret Manager),
# and the service account the API runs as. Safe to re-run: existing pieces are kept.
#
# COST: this creates a Cloud SQL instance, which is billed for every hour it runs
# (about $8-10 a month for the smallest size) plus storage. Stop it with deploy/stop.sh.
set -euo pipefail
cd "$(dirname "$0")/.." && source deploy/config.sh

echo "Project: $PROJECT_ID   Region: $REGION   Instance: $SQL_INSTANCE"
gcloud services enable sqladmin.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
    artifactregistry.googleapis.com secretmanager.googleapis.com aiplatform.googleapis.com \
    --project "$PROJECT_ID"

if gcloud sql instances describe "$SQL_INSTANCE" --project "$PROJECT_ID" >/dev/null 2>&1; then
    echo "Database instance $SQL_INSTANCE already exists; keeping it."
else
    echo "Creating database instance (takes 5-10 minutes)..."
    # Smallest shared-core size, one zone, no automated backups: a demo, not production.
    gcloud sql instances create "$SQL_INSTANCE" --project "$PROJECT_ID" --region "$REGION" \
        --database-version=POSTGRES_16 --edition=enterprise --tier=db-f1-micro \
        --storage-type=HDD --storage-size=10 --no-storage-auto-increase \
        --availability-type=zonal --no-backup
fi

gcloud sql databases describe "$DB_NAME" --instance "$SQL_INSTANCE" --project "$PROJECT_ID" >/dev/null 2>&1 \
    || gcloud sql databases create "$DB_NAME" --instance "$SQL_INSTANCE" --project "$PROJECT_ID"

if gcloud secrets describe "$SECRET" --project "$PROJECT_ID" >/dev/null 2>&1; then
    echo "Secret $SECRET already exists; keeping the current database password."
else
    # The password is generated here, set on the database user, and stored only in Secret Manager.
    PASSWORD="$(openssl rand -hex 24)"
    if gcloud sql users list --instance "$SQL_INSTANCE" --project "$PROJECT_ID" --format="value(name)" | grep -qx "$DB_USER"; then
        gcloud sql users set-password "$DB_USER" --instance "$SQL_INSTANCE" --project "$PROJECT_ID" --password "$PASSWORD"
    else
        gcloud sql users create "$DB_USER" --instance "$SQL_INSTANCE" --project "$PROJECT_ID" --password "$PASSWORD"
    fi
    printf 'postgresql://%s:%s@/%s?host=/cloudsql/%s' "$DB_USER" "$PASSWORD" "$DB_NAME" "$CONNECTION_NAME" \
        | gcloud secrets create "$SECRET" --project "$PROJECT_ID" --replication-policy=automatic --data-file=-
    unset PASSWORD
fi

gcloud iam service-accounts describe "$SERVICE_ACCOUNT_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1 \
    || gcloud iam service-accounts create "$SERVICE_ACCOUNT" --project "$PROJECT_ID" \
           --display-name "NeuroScope API (Cloud Run)"
# Least privilege: connect to the database, call Vertex AI, read this one secret.
for role in roles/cloudsql.client roles/aiplatform.user; do
    gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:$SERVICE_ACCOUNT_EMAIL" \
        --role "$role" --condition=None --quiet >/dev/null
done
gcloud secrets add-iam-policy-binding "$SECRET" --project "$PROJECT_ID" \
    --member "serviceAccount:$SERVICE_ACCOUNT_EMAIL" --role roles/secretmanager.secretAccessor --quiet >/dev/null

echo "Setup done. Next: deploy/2_load_data.sh"
