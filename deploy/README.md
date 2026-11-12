# Deploying NeuroScope to Google Cloud

What gets created in your project:

| Piece | What it is | Cost |
|---|---|---|
| Cloud SQL instance `neuroscope-db` | Postgres 16 with pgvector, smallest shared size, 10 GB | Billed per hour while running (about $8-10 a month if left on); storage only while stopped |
| Cloud Run service `neuroscope-api` | The agent's HTTP API, at most one small instance, none when idle | Nothing while idle; within the free tier at this usage |
| Secret `neuroscope-database-url` | The database address and password | Negligible |
| Service account `neuroscope-run` | The identity the API runs as: may connect to the database, call Vertex AI, and read that one secret | Free |
| A role on the default compute service account | Lets Google's build service read the uploaded source and store the built image | Free |
| Container image in Artifact Registry | The built website and API | Free under 0.5 GB |
| Cloud Run job `neuroscope-refresh` and scheduler `neuroscope-weekly-refresh` (optional) | The weekly data refresh | A few minutes of compute a week |

The database is the only piece with a running cost, so **stop it when you are not using it**.

## One-time setup

```bash
gcloud auth login                       # sign the gcloud command in (separate from the app's sign-in)
set -a && source .env && set +a         # provides GOOGLE_CLOUD_PROJECT
docker compose up -d                    # the local database is the source of the data

deploy/1_setup.sh                       # APIs, database instance (5-10 min), password, service account
deploy/2_load_data.sh                   # copy the local database to the cloud and compare row counts
deploy/3_deploy_service.sh              # build and deploy the website and API, private (a few minutes)
deploy/3_deploy_service.sh --public     # ...or public: anyone with the link can use it
deploy/4_schedule_refresh.sh --run-now  # optional: refresh all data every Monday, and once now
```

Each script can be re-run. `1_setup.sh` keeps anything that already exists.

## Everyday use

```bash
deploy/status.sh                        # is the database running (and costing money)?
deploy/start.sh                         # start the database; wait about a minute
deploy/ask.sh                           # /health: what the cloud database holds
deploy/ask.sh "Is semaglutide approved for Alzheimer's disease?"
deploy/open.sh                          # open the web page in your browser (runs a signed local proxy)
deploy/stop.sh                          # stop the database when you are done
```

While the database is stopped, the API still responds, with a 503 saying the database is stopped.

## Who can use it

**Private (the default).** Google rejects any request that does not carry an identity token from an account the project allows. `deploy/ask.sh` sends yours, and `deploy/open.sh` runs a local proxy that does the same for your browser. Opening the service's URL directly shows Google's "Forbidden" page.

**Public (`--public`).** Anyone with the link can open the page and ask questions. Every question that reaches the model costs money, so the API enforces limits that do not depend on who is asking:

| Limit | Default | Setting |
|---|---|---|
| Questions sent to the model per day | 50 | `NEUROSCOPE_DAILY_QUESTION_LIMIT` |
| Questions sent to the model per month | 500 | `NEUROSCOPE_MONTHLY_QUESTION_LIMIT` |
| Questions per visitor per 10 minutes | 5 | `NEUROSCOPE_VISITOR_LIMIT` |
| Repeated questions | answered from a 24-hour cache, no model call | |

Set the variables before running the deploy script to change them. The daily and monthly counts are stored in the database, so they survive restarts. When a cap is reached the page says so, and cached answers (including the example questions, once asked) keep working.

What the caps do: put a ceiling on model spending. At roughly half a cent per question (an estimate, not a quoted price), 500 questions a month is a few dollars at most. What they do not do: stop one person from using up the day's questions, which makes the page show "limit reached" to everyone until the next day. Visitors cannot change or read anything beyond the five read-only lookups.

To go back to private, re-run `deploy/3_deploy_service.sh` without `--public`.

## Weekly refresh

`deploy/4_schedule_refresh.sh` creates a Cloud Run job that runs `python -m neuroscope.refresh` against the cloud database, and a Cloud Scheduler trigger for every Monday at 06:00 UTC. Each run re-fetches trials for every disease, the FDA export, FDA labels, the EMA list, PubMed abstracts, and new SEC filings, re-reads outcome terms and ChEMBL mechanisms, embeds new passages, and clears saved answers. The SEC step needs `SEC_USER_AGENT` (a contact the SEC asks automated clients for); the script passes the value from your `.env`, so source it before running. Without it, that one step is recorded as failed and the others still run. Steps run independently; each run is recorded in `refresh_runs` (see `deploy/cloud.sh python -m neuroscope.report`). Cost: a few minutes of compute a week and cents of embeddings; Cloud Scheduler's first 3 jobs are free.

Once the refresh runs, **the cloud database is the newest copy**. New trial names land in its review queue:

```bash
deploy/cloud.sh python -m neuroscope.curate unreviewed
deploy/cloud.sh python -m neuroscope.curate apply data/curation/your_file.csv --dry-run
```

`deploy/cloud.sh` runs any command against the cloud database through the proxy.

To apply a correction file, use `deploy/apply_corrections.sh FILE.csv` rather than applying it to one database: it shows a dry run on the cloud database, asks for confirmation, then applies the file to the cloud and to your local database (add `--dry-run` to only look, `--yes` to skip the question). Cloud paper links follow at the next weekly refresh.

## Updating

- Code change: `deploy/3_deploy_service.sh`, then `deploy/4_schedule_refresh.sh` so the job uses the same code.
- Copying local data over the cloud copy: `deploy/2_load_data.sh`. It refuses when the cloud copy has newer trial data than the local one (it would roll back refreshes); `--force` overrides. Copying also resets the question counter and saved answers.

## Removing everything

```bash
deploy/teardown.sh                      # deletes the API, the database and its data, and the secret
```

It asks you to type the instance name first. Your local database and this repository are untouched.

## Not included

No custom domain, no automated backups, and no high availability. The database password is generated by `1_setup.sh` and exists only in Secret Manager.
