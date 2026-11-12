# NeuroScope

[![tests](https://github.com/gosainansh8/neuroscope/actions/workflows/tests.yml/badge.svg)](https://github.com/gosainansh8/neuroscope/actions/workflows/tests.yml)

Sourced drug-development intelligence for Alzheimer's disease, Parkinson's disease, and ALS. Ask about a drug's trials, US and EU approval, or published evidence; every answer cites its records, and code checks each citation.

![NeuroScope answering "Is opicapone approved for Parkinson's disease in the US and EU?" with verified FDA and EMA citations](docs/demo.gif)

**Live demo: https://neuroscope-api-561220657386.us-central1.run.app** (50 new questions a day; repeats come from a cache)

## What it does

- **Combines four public sources:** ClinicalTrials.gov (9,815 trials), Drugs@FDA and FDA labels, the European Medicines Agency, and PubMed (7,838 abstracts, searchable by meaning).
- **Identifies each drug under all its names:** codes, brands, and generic names (BIA 9-1067, Ongentys, opicapone) become one drug. Only exact or same-record matches merge names, never fuzzy ones, and every link records where it came from. A wrong merge is fixed by correcting the alias and re-linking.
- **Never infers approval:** approval comes only from FDA and EMA records, never from trial phase. A drug counts as approved for a disease only when its label or indication text says so, and answers quote that text.
- **Answers with checked citations:** a Gemini agent can only call six read-only lookups. Every trial ID, FDA application, EMA product number, and PubMed ID in an answer must come from a lookup made for that answer, or it is flagged.
- **Tracks change:** a weekly job on Google Cloud refreshes every source and records new trials and changes in status, phase, and completion date.

## How it works

```
ClinicalTrials.gov ─┐                         ┌─ find_drug ─────────┐
Drugs@FDA + labels ─┤   Postgres + pgvector   ├─ get_trials         │
EMA medicines ──────┼─► drugs, aliases, ──────┼─ get_fda_status     ├─► Gemini ─► answer ─► citation check
PubMed abstracts ───┘   trials, approvals,    ├─ get_ema_status     │
                        passages + embeddings └─ search_literature, list_pipeline
```

Each trial is tagged with every disease whose registry search returned it, provided its own text names that disease. That second condition drops studies the registry returns through synonyms, such as cancer trials matched by "PD" (as in PD-1).

## Results

Scored by code (no model judges) on questions whose answers come from the database. The case file is `evals/cases.json` and full reports are in `evals/results/`:

| Run (2026-11-12, `gemini-3.8-flash`) | Passed | Invented citations |
|---|---|---|
| Alzheimer's cases | 38–39 of 39 | 0 |
| EU approval cases | 5 of 5 | 0 |

## Run it locally

Requires Python 3.11+ and Docker.

```bash
docker compose up -d                                  # Postgres 16 + pgvector
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,api,vertex]"
cp .env.example .env && set -a && source .env && set +a
python -m neuroscope.db init
REQUIRE_DB_TESTS=1 pytest                             # 260 tests, no network needed

python -m neuroscope.ingest --disease alzheimers      # also: parkinsons, als
python -m neuroscope.fda load && python -m neuroscope.fda labels && python -m neuroscope.ema load
python -m neuroscope.pubmed load --disease alzheimers --min-trials 2 --per-drug 40
python -m neuroscope.embed run                        # needs a Google Cloud project (see docs)
uvicorn neuroscope.api:app --port 8080                # web page at http://localhost:8080
```

Deployment to Cloud Run and Cloud SQL is covered in [`deploy/README.md`](deploy/README.md). The merge rules, review history, per-source details, and known limitations are in [`docs/DETAILS.md`](docs/DETAILS.md).

## Stack

Python, Postgres with pgvector, Gemini and text embeddings on Vertex AI, FastAPI, Cloud Run, Cloud SQL, Cloud Scheduler, and GitHub Actions. The web page is a single self-contained HTML file.
