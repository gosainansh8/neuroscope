# NeuroScope

Sourced neuroscience drug-development intelligence. Launching with Alzheimer's disease.

Milestone 1: the structured data layer. Pulls Alzheimer's trials from ClinicalTrials.gov, normalizes them, resolves drug names to canonical drugs with full provenance, and tracks every change between runs. No agents or cloud deployment yet.

## Setup

Requirements: Python 3.11+, Docker. If `python3 --version` is older, create the venv with `python3.11`.

```bash
docker compose up -d                          # Postgres 16 + pgvector on localhost:5432
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env && set -a && source .env && set +a
python -m neuroscope.db init                  # tables, migrations, 25 curated drugs and their aliases
```

Upgrading an existing database: pull, then run `python -m neuroscope.db init` again. It only creates what is missing and applies `db/migrations/`; no trial, intervention, alias, snapshot, or change record is touched. Run `python -m neuroscope.curate reresolve` afterwards to apply new resolution rules to existing interventions.

## Verify it

```bash
# 1. Tests. REQUIRE_DB_TESTS=1 turns a missing database into an error instead of silent skips.
REQUIRE_DB_TESTS=1 pytest -rs                 # expect: 149 passed, 0 skipped

# 2. Live check: 25 Alzheimer's studies in pages of 10, then re-fetch the same
#    25 by NCT ID and confirm nothing duplicated. Writes live_review.md.
python -m neuroscope.live_check

# 3. Read the summary and the review.
python -m neuroscope.report
open live_review.md
```

`live_check` exits non-zero if any check fails: pagination (3 pages for 25 studies), no duplicate IDs, no load failures, run 2 creates no trials, and row counts identical after the re-run. It also prints how many live records lacked each optional field.

## Everyday commands

```bash
python -m neuroscope.ingest --save-raw data/raw/ad.jsonl   # full Alzheimer's ingestion
python -m neuroscope.ingest --from-jsonl data/raw/ad.jsonl # replay offline (labeled 'replay')
python -m neuroscope.review --limit 10 --origin live_api --out review.md
python -m neuroscope.curate unreviewed                     # learned aliases, most trials waiting first
python -m neuroscope.curate conflicts
```

## What is allowed to merge two drug names

Exactly two things, and nothing else:

1. Exact key match with a trusted alias: `verified`, or `unreviewed` but learned from the same NCT ID. Keys are normalized strings: `BAN-2401`, `BAN 2401`, and `ban2401` all become `ban2401`; accents are dropped and Greek letters spelled out (`Aβ` = `Abeta`). Doses, dose levels, routes, salts, and dosage forms (tablet, capsule, pill, patch, ER) are stripped first (`Lecanemab 10 mg/kg IV` becomes `lecanemab`). Other words, such as `PET scan`, are kept. There is no fuzzy matching, partial matching, or similarity scoring.
2. Same-record equivalence. Names a sponsor lists for one intervention in one registry record (the registered name, a name in parentheses, a code after a comma as in `Brexpiprazole, OPC-34712`, and that intervention's `otherNames`) are treated as one drug. Aliases learned this way are stored as `unreviewed` with the NCT ID that taught them.

Never a basis for merging: shared words, shared mechanism, shared sponsor, appearing in the same trial, or string similarity.

Ambiguity: if one intervention's names point at two different drugs, nothing merges and nothing is learned from that record. If the registered name matches a drug, the intervention links there; otherwise it stays unlinked. Either way it lands in `drug_alias_conflicts`.

Combinations: `Donepezil + Memantine` is its own drug with an order-independent key, so `Memantine and Donepezil` matches it. It never links to donepezil or memantine alone. Known ingredients are recorded in `drug_components`. A combination record ignores `otherNames` that name a single known drug, and a single-drug record ignores combination-looking `otherNames`. A comma list (`Rivastigmine, memantine`) is a combination unless it is a one-word name next to a code. A name listing alternatives (`donepezil, rivastigmine or galantamine`) is never a combination.

Alternatives: a registered name that lists two or more usable names joined by `or` (`18F-Florbetapir or 18F-Flutemetamol`) does not say which drug the arm received. It is left unlinked as `ambiguous_name`, keeps its original text, and teaches no aliases; an `A or B` otherName is never learned either. `curate ambiguous` and the report list these. They cannot be linked by curation, by design.

Display names: a new drug is named after its registered name. A one-word, not-all-caps letters-only otherName (`tilavonemab` for `ABBV-8E12`) replaces a registered code, since it is likely the nonproprietary name. A multi-word description (`gamma secretase inhibitor`) or an acronym (`DN-TNF`) never does; it is still stored as an alias with its source and NCT ID.

Placebo: an intervention is a placebo if its name says placebo, sham, or sugar pill. If it is a drug joined to a bare placebo (`DAOIB plus placebo`, `Placebo and S-equol`), the drug part is resolved and `is_placebo` is false. A placebo that describes what it matches (`Placebo to Match Emtricitabine/Tenofovir`) stays placebo and unlinked.

Radiotracers: the isotope label is part of the identity and is never discarded. Every notation of one label is rewritten to a prefix (`18F-MK-6240`, `[18F]MK6240`, and `MK-6240 F 18` all become `[18F]MK-6240`). A labeled name never matches the unlabeled compound; a human decides that with `curate map`. A label on its own, like `(18F)`, is never an alias. A name with two labels is left as registered.

### Unreviewed aliases wait for review

An alias learned from trial A links only trial A. If trial B's names hit it, B is left unlinked with `resolution_method = 'pending_review'` and `matched_alias_key` naming the alias. Nothing is learned from B. A pending hit that disagrees with a trusted match is logged as a conflict. To link the waiting trials:

```bash
python -m neuroscope.curate unreviewed            # ranked by trials waiting on each alias
python -m neuroscope.curate verify "XYZ-1234"     # or map / reject
python -m neuroscope.curate reresolve
```

This replaced an earlier default in which unreviewed aliases matched everywhere. In a 1,500-record live sample, that default had linked FDG-PET interventions to insulin, `AH110690` to the wrong tracer, and `Treatment A` to atabecestat. The cost is more unlinked interventions until review (184 of 2,469 in that sample) and some duplicate auto-created drugs that surface as conflicts (e.g. `bapineuzumab` and `AAB-001`) instead of being silently merged. `NEUROSCOPE_TRUST_UNREVIEWED_ALIASES=1` restores the old behavior for ingestion and `reresolve`.

## Provenance and correcting a wrong merge

- Every intervention row keeps the original registered name and `otherNames` exactly as published, plus `resolution_method` (`matched`, `created`, `conflict`, `pending_review`, `placebo`, `non_drug`, `no_usable_name`) and the alias key that decided the link (or, for `pending_review`, the alias awaiting review).
- Every alias records its source (`seed`, `manual`, `ctgov_name`, `ctgov_parenthetical`, `ctgov_other_name`), its review status (`verified`, `unreviewed`, `rejected`), and the first NCT ID it came from.
- Aliases, not trial links, are the source of truth. To fix a wrong merge:

```bash
python -m neuroscope.curate map "DEF-200" defumab --note "sponsor pipeline page"
python -m neuroscope.curate reresolve
```

`reresolve` recomputes every intervention's drug from its original names. No trial, intervention, or original name is deleted; auto-created drugs left with nothing pointing at them are removed. `reject` tombstones a name so ingestion can never re-learn it. Every correction is written to `alias_corrections`. This exact scenario is a test (`test_wrong_merge_is_reversible`).

## Data integrity

- Phase and recruitment status live on trials only. `v_drug_pipeline` summarizes trial phases and says so; it does not represent regulatory status.
- FDA approval is never inferred from phase, completion, or a Phase 4 trial. Approval will come only from FDA records in a later milestone.
- Raw API responses are kept in `trial_snapshots` (JSONB), one row per distinct version of each record.
- Two dates everywhere: `last_update_posted` (when the source changed) and `last_retrieved_at` (when we pulled it).
- Every trial and run is labeled `live_api`, `replay`, or `synthetic`. Reports print the label on every trial, and synthetic trials get no source link.
- Change detection hashes each record excluding `derivedSection`, which CT.gov rewrites every release and would otherwise flag every trial as changed.

## Layout

```
db/schema.sql            tables, indexes, v_drug_pipeline
db/migrations/           idempotent changes to existing tables, applied in order by `db init`
data/seed_*.csv          25 curated drugs with code and brand names (verify before trusting)
data/raw/                raw CT.gov JSONL (git-ignored), incl. the 1,500-record validation sample
data/validation/         before/after resolution report for that sample, and the script that made it
neuroscope/ctgov.py      API client (paging, retries, fetch by ID) and parser
neuroscope/normalize.py  pure functions: phases, statuses, dates, names, combinations
neuroscope/resolver.py   the merge rules above
neuroscope/ingest.py     per-study transactions, snapshots, change log
neuroscope/curate.py     review and correction CLI
neuroscope/live_check.py live two-run verification
neuroscope/review.py     per-trial Markdown review
neuroscope/report.py     database summary
tests/                   149 tests; all fixtures synthetic (fake NCT9xxxxxxx IDs), names from live records
```

## Known limitations

- Until aliases are reviewed, the same drug can exist twice as auto-created drugs (e.g. `bapineuzumab` and `AAB-001`); records naming both become conflicts.
- Alternatives are detected only by the word `or`; other phrasings (`either A/B`, `A vs B`) are not recognized.
- A one-word otherName can still become a poor display name: a brand (`Interferon beta-1a` is named `rebif`) or a sponsor's misspelling (`flortauapir`).
- `reresolve` re-links trials but never renames drugs. Drugs created before the display-name rule keep their old names (rename with `curate map`).
- Sponsors sometimes put procedures or other arms in `otherNames`. Those names are learned as unreviewed aliases of the record's drug; they no longer affect other trials.
- Dietary supplements and other non-drug intervention types are stored but not resolved to drugs.
- Combination detection uses separators (`+`, `/`, `&`, `;`, `and`, `plus`, `in combination with`). An unusual phrasing can be missed, and a name that legitimately contains one of these words would be split.
- Seed aliases are curated by hand. Verify them against primary sources.
- Migrations are plain idempotent SQL files that `db init` re-applies on every run. There is no version table and no rollback.
