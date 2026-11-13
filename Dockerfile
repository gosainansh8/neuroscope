# The NeuroScope API (agent over the database). Built by `gcloud run deploy --source .`.
FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY neuroscope ./neuroscope
COPY db ./db
COPY data/seed_drugs.csv data/seed_aliases.csv data/sec_companies.csv ./data/

# Editable install keeps db/ and data/ where neuroscope.db expects them.
RUN pip install --no-cache-dir -e ".[vertex,api,team,tracing]"

ENV PYTHONUNBUFFERED=1
# Cloud Run sets PORT. One worker: the service is capped at one small instance.
CMD ["sh", "-c", "exec uvicorn neuroscope.api:app --host 0.0.0.0 --port ${PORT:-8080}"]
