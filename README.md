# NeuroScope

Sourced neuroscience drug-development intelligence. Launching with Alzheimer's disease.

Milestone 1: the structured data layer. Pulls Alzheimer's trials from ClinicalTrials.gov, normalizes them, resolves drug names to canonical drugs with full provenance, and tracks every change between runs.

Milestone 2: FDA approval. Loads approval records from Drugs@FDA and indications from FDA labels, matched to the same canonical drugs. Approval status comes only from these records.

Milestone 3: documents. Loads PubMed abstracts for the most-studied drugs, links each to the drugs it names, and offers keyword search. Semantic search uses Vertex AI embeddings and needs a Google Cloud project.

Milestone 4: an agent. Answers questions by calling read-only lookups over the trials, FDA records, and abstracts, cites its sources, and has its citations checked in code. There is no cloud deployment yet.

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
REQUIRE_DB_TESTS=1 pytest -rs                 # expect: 207 passed, 0 skipped

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
python -m neuroscope.fda load                              # FDA approvals (downloads Drugs@FDA)
python -m neuroscope.fda labels --save-raw data/raw/fda/labels.jsonl   # FDA label indications
python -m neuroscope.pubmed load --save-raw data/raw/pubmed/ad.xml     # PubMed abstracts
python -m neuroscope.search "amyloid related imaging abnormalities" --drug lecanemab
python -m neuroscope.agent "Is semaglutide approved for Alzheimer's disease?" --show-steps
```

## What is allowed to merge two drug names

Exactly two things, and nothing else:

1. Exact key match with a trusted alias: `verified`, or `unreviewed` but learned from the same NCT ID, or the same-name case (the intervention's registered name is the drug's own canonical name). Keys are normalized strings: `BAN-2401`, `BAN 2401`, and `ban2401` all become `ban2401`; accents are dropped and Greek letters spelled out (`Aβ` = `Abeta`). Doses, dose levels, routes, salts, and dosage forms (tablet, capsule, pill, patch, ER) are stripped first (`Lecanemab 10 mg/kg IV` becomes `lecanemab`). Other words, such as `PET scan`, are kept. There is no fuzzy matching, partial matching, or similarity scoring.
2. Same-record equivalence. Names a sponsor lists for one intervention in one registry record (the registered name, a name in parentheses, a code after a comma as in `Brexpiprazole, OPC-34712`, and that intervention's `otherNames`) are treated as one drug. Aliases learned this way are stored as `unreviewed` with the NCT ID that taught them.

Never a basis for merging: shared words, shared mechanism, shared sponsor, appearing in the same trial, or string similarity.

Ambiguity: if one intervention's names point at two different drugs, nothing merges and nothing is learned from that record. If the registered name matches a drug, the intervention links there; otherwise it stays unlinked. Either way it lands in `drug_alias_conflicts`.

Combinations: `Donepezil + Memantine` is its own drug with an order-independent key, so `Memantine and Donepezil` matches it. It never links to donepezil or memantine alone. Known ingredients are recorded in `drug_components`. A combination record ignores `otherNames` that name a single known drug, and a single-drug record ignores combination-looking `otherNames`. A comma list (`Rivastigmine, memantine`) is a combination unless it is a one-word name next to a code. A name listing alternatives (`donepezil, rivastigmine or galantamine`) is never a combination.

Alternatives: a registered name that lists two or more usable names joined by `or` (`18F-Florbetapir or 18F-Flutemetamol`) does not say which drug the arm received. It is left unlinked as `ambiguous_name`, keeps its original text, and teaches no aliases; an `A or B` otherName is never learned either. `curate ambiguous` and the report list these. They cannot be linked by curation, by design.

Display names: a new drug is named after its registered name. A one-word, not-all-caps letters-only otherName (`tilavonemab` for `ABBV-8E12`) replaces a registered code, since it is likely the nonproprietary name. A multi-word description (`gamma secretase inhibitor`) or an acronym (`DN-TNF`) never does; it is still stored as an alias with its source and NCT ID.

Placebo: an intervention is a placebo if its name says placebo, sham, or sugar pill. If it is a drug joined to a bare placebo (`DAOIB plus placebo`, `Placebo and S-equol`), the drug part is resolved and `is_placebo` is false. A placebo that describes what it matches (`Placebo to Match Emtricitabine/Tenofovir`) stays placebo and unlinked.

Radiotracers: the isotope label is part of the identity and is never discarded. Every notation of one label is rewritten to a prefix (`18F-MK-6240`, `[18F]MK6240`, and `MK-6240 F 18` all become `[18F]MK-6240`). A labeled name never matches the unlabeled compound; a human decides that with `curate map`. A label on its own, like `(18F)`, is never an alias. A name with two labels is left as registered.

### Unreviewed aliases wait for review

An alias learned from trial A links only trial A, with one exception: a trial whose *registered name* equals a drug's own canonical name links to it (34 trials registered as florbetapir are one drug; no two different names are being equated). A hit through an otherName or parenthetical never qualifies. Otherwise, if trial B's names hit an unreviewed alias, B is left unlinked with `resolution_method = 'pending_review'` and `matched_alias_key` naming the alias. Nothing is learned from B. A pending hit that disagrees with a trusted match is logged as a conflict. To link the waiting trials:

```bash
python -m neuroscope.curate unreviewed            # ranked by trials waiting on each alias
python -m neuroscope.curate verify "XYZ-1234"     # or map / reject
python -m neuroscope.curate reresolve
```

Many corrections at once: put them in a CSV (`action,alias,drug,confidence,reason`) and run `python -m neuroscope.curate apply FILE.csv --dry-run` to see the before/after counts with nothing changed, then without `--dry-run` to apply all rows (or none, if any row is invalid) and reresolve. Actions: `map` (this name is this drug; mapping a name to itself confirms it or splits it off as its own drug), `reject` (this text is not a drug name: it never matches, never names a drug, and an intervention registered only under it stays unlinked), and `resolve` (the alias column holds an NCT ID; that trial's conflicts are marked reviewed and its links stay as they are). `data/curation/proposed_aliases_2026-11-10.csv` holds the 69 corrections from the first full load, approved and applied on 2026-11-10 (pending 127 to 59, conflicts 99 to 44). Mappings were checked against registry records that list both names and, for less common codes, public sources. It merges unlabeled tracer names, PET-scan names, and salt forms into the base drug by decision, not by rule. Two later one-line files (`review_round3`, `review_round4`) merged duplicates found while loading FDA and PubMed data. `data/curation/review_round2_2026-11-10.csv` (96 rows, applied the same day) finished the review: pending 59 to 6, open conflicts 44 to 0, with 31 conflicts marked reviewed and left as they are (multi-tracer studies, formulation variants kept separate, wrong sponsor otherNames). A fresh database needs all four files applied again after ingestion: `proposed_aliases`, then rounds 2, 3, and 4.

This replaced an earlier default in which unreviewed aliases matched everywhere. In a 1,500-record live sample, that default had linked FDG-PET interventions to insulin, `AH110690` to the wrong tracer, and `Treatment A` to atabecestat. The cost is more unlinked interventions until review (127 of 7,217 in the full Alzheimer's load of 4,341 trials on 2026-11-10, plus 99 conflicts) and some duplicate auto-created drugs that surface as conflicts (e.g. `bapineuzumab` and `AAB-001`) instead of being silently merged. `NEUROSCOPE_TRUST_UNREVIEWED_ALIASES=1` restores the old behavior for ingestion and `reresolve`.

## Provenance and correcting a wrong merge

- Every intervention row keeps the original registered name and `otherNames` exactly as published, plus `resolution_method` (`matched`, `created`, `conflict`, `pending_review`, `placebo`, `non_drug`, `no_usable_name`) and the alias key that decided the link (or, for `pending_review`, the alias awaiting review).
- Every alias records its source (`seed`, `manual`, `ctgov_name`, `ctgov_parenthetical`, `ctgov_other_name`), its review status (`verified`, `unreviewed`, `rejected`), and the first NCT ID it came from.
- Aliases, not trial links, are the source of truth. To fix a wrong merge:

```bash
python -m neuroscope.curate map "DEF-200" defumab --note "sponsor pipeline page"
python -m neuroscope.curate reresolve
```

`reresolve` recomputes every intervention's drug from its original names. No trial, intervention, or original name is deleted; auto-created drugs left with nothing pointing at them are removed. `reject` tombstones a name so ingestion can never re-learn it. Every correction is written to `alias_corrections`. This exact scenario is a test (`test_wrong_merge_is_reversible`).

## FDA approval

```bash
python -m neuroscope.fda load          # Drugs@FDA bulk export (about 9 MB, no API key)
python -m neuroscope.fda labels        # labels for the approved applications found above
python -m neuroscope.report            # includes the FDA sections
```

Two separate questions, answered from two FDA sources:

1. **Is the drug FDA-approved for anything?** From Drugs@FDA. An application counts as approved only if its original submission has status `AP`; a tentative approval does not count. `v_drug_fda_approval` gives the first approval date (from NDA/BLA applications, not later generics), brand names, the number of generic applications, and whether any product is still marketed.
2. **Does its label mention Alzheimer's as a use?** From FDA drug labels. The "Indications and Usage" text is stored verbatim in `fda_labels`, and `indications_mention_alzheimers` is a plain text search of that section. It finds labels worth reading; it is not a regulatory determination. Tracer labels match because they say "evaluation of Alzheimer's disease", and a label can also mention the disease in a limitation of use. `NULL` means no label was retrieved, not "no".

Matching follows the same rule as trials: exact key against a trusted name (a verified alias or the drug's own canonical name), never an unreviewed alias. The published ingredient is tried as is, then with three FDA naming conventions removed: the four-letter biologic suffix (`LECANEMAB-IRMB`), the radiolabel notation (`FLORBETAPIR F-18`), and a trailing salt (`DONEPEZIL HYDROCHLORIDE`). Esters such as acetate are kept. A multi-ingredient product matches only a combination drug with exactly those ingredients. Each product row records the published ingredients, the key that matched, and whether the match was `exact` or `normalized`.

Labels are fetched for approved NDA and BLA applications. If a drug has no marketed NDA/BLA product (galantamine's brand is discontinued), its approved generic labels are used instead.

On the 2026-11-06 export: 162 of our drugs matched an approved application. For 12 the label indications mention Alzheimer's (donepezil, rivastigmine, galantamine, memantine, lecanemab, donanemab, brexpiprazole, dextromethorphan + bupropion, and four PET tracers); 131 are approved for other uses only (semaglutide, risperidone); 19 have no label retrieved (aducanumab, whose label is no longer published).

Both loads replace all FDA rows, so re-run them after correcting aliases. Raw downloads are kept under `data/raw/fda/` and can be replayed with `--file` and `--from-jsonl`.

## Documents and search

```bash
python -m neuroscope.pubmed load                 # drugs with 5+ trials, 20 abstracts each (no API key)
python -m neuroscope.search "cognitive decline" --drug Leqembi --limit 5
```

For each drug, PubMed is searched for abstracts naming the drug and Alzheimer's. Each abstract is one row in `documents` (title, journal, date with its precision, PMID link) and one or more overlapping text chunks in `chunks`. Only abstracts are stored; PubMed does not carry full text.

A document is linked to a drug only when a trusted name of that drug (a verified alias or its own canonical name, four or more characters) appears in the title or abstract as a whole word. Being returned by a search is not enough, and unreviewed aliases are never used. `document_drugs.matched_name` records the name that was found. Links are recomputed on every load, so they follow alias corrections.

Search is Postgres full-text search: it matches word stems (`abnormality` finds `abnormalities`) and ranks by term frequency. It is not semantic search. `--drug` accepts any trusted name, including brands and codes. Every hit prints its source link and publication date.

First load (2026-11-10): 823 abstracts for 67 drugs, 1,312 chunks, 801 abstracts linked to 223 drugs.

### Semantic search (Vertex AI embeddings)

Keyword search cannot connect "brain swelling" to an abstract that says "cerebral edema". Embeddings can: each chunk is turned into 768 numbers that place it by meaning, and a search finds the chunks nearest the question.

One-time setup (needs a Google Cloud project with billing enabled):

```bash
brew install --cask google-cloud-sdk             # the gcloud tool
gcloud auth application-default login            # sign in; stores credentials on this machine only
gcloud services enable aiplatform.googleapis.com --project YOUR_PROJECT_ID
pip install -e ".[vertex]"
# then set GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID in .env and re-source it
```

```bash
python -m neuroscope.embed status                # how many chunks have embeddings
python -m neuroscope.embed run --limit 50        # try a small batch first
python -m neuroscope.embed run                   # the rest; safe to interrupt and re-run
python -m neuroscope.search "brain swelling side effects" --semantic --drug lecanemab
```

The model is `text-embedding-005` (768 dimensions, matching `chunks.embedding`). Each chunk records the model that embedded it; a chunk is embedded again only if it is new or the model changes. Semantic search compares only chunks embedded by the current model and reports cosine similarity. Tests use a stand-in embedder, so they need no network or credentials.

Embedded on 2026-11-11: all 1,312 chunks, in about one minute. On three plain-language questions ("brain swelling side effects", "drugs that calm aggressive behavior in dementia", "does the medicine slow memory loss"), keyword search returned nothing and semantic search returned relevant abstracts, such as the ARIA papers for the first. Ranking is approximate: similarity scores cluster between 0.55 and 0.75, and a broad review can outrank a specific study. Results are a starting point for reading, not an answer.

## The agent

```bash
python -m neuroscope.agent "What is the regulatory status of lecanemab, and what trials is it in?"
python -m neuroscope.agent "Is masitinib FDA approved? It finished a Phase 3 trial." --show-steps
```

The model (Gemini on Vertex AI) cannot see the database. It can only call five read-only lookups in `neuroscope/tools.py`, read what they return, and write an answer:

| Lookup | Returns |
|---|---|
| `find_drug` | the canonical drug and its verified names, or "not found" with suggestions |
| `get_trials` | trial counts and the most relevant trials, with NCT IDs |
| `get_fda_status` | approval records with application numbers, and the label's indication text |
| `search_literature` | PubMed abstracts by meaning, with PMIDs |
| `list_pipeline` | drugs by trial phase, with whether an FDA record exists |

Each lookup returns its identifiers and the caveats that belong to the data ("trial phase is not regulatory status", "no FDA record is not proof of non-approval").

Two things keep answers honest:

1. **Instructions.** Use only what the lookups return; cite every claim by its identifier; approval comes only from `get_fda_status`; say a drug is approved for Alzheimer's only when the label text says so, and quote it; say what is missing when the data does not answer.
2. **A check in code.** After the model answers, every NCT ID, FDA application number, and PMID in the answer is compared with what the lookups returned in that run. Any identifier that no lookup returned is printed as a warning. This catches invented citations. It does not check that a correctly cited source supports the sentence it is attached to; that still needs a reader.

Checked live on 2026-11-11 with `gemini-3.8-flash`:

| Question | Answer |
|---|---|
| Lecanemab's status and trials | Approved (BLA761269, 2023-01-06), label quoted, 8 trials listed; 10 identifiers, all verified |
| Is semaglutide approved for Alzheimer's? | No: approved for other uses, with the label's indications quoted |
| Is masitinib approved? It finished Phase 3 | No matched FDA record; states that finishing Phase 3 is not approval |
| Brain swelling with donanemab | Summarizes five abstracts on ARIA-E with PMIDs, and notes the abstracts lack incidence figures |
| Lecanemab's revenue last quarter | Says the database does not hold revenue data |

Settings: `NEUROSCOPE_AGENT_MODEL` (default `gemini-3.8-flash`) and `NEUROSCOPE_AGENT_LOCATION` (default `global`; Gemini is not served from `us-central1` for this project, while embeddings are). A question costs roughly 2,000 to 9,000 input tokens. Requests time out after 60 seconds.

## Data integrity

- Phase and recruitment status live on trials only. `v_drug_pipeline` summarizes trial phases and says so; it does not represent regulatory status.
- FDA approval is never inferred from phase, completion, or a Phase 4 trial. It comes only from FDA records (`v_drug_fda_approval`), and "approved" there means approved for some use, not for Alzheimer's.
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
data/curation/           alias correction files for `curate apply`
neuroscope/ctgov.py      API client (paging, retries, fetch by ID) and parser
neuroscope/normalize.py  pure functions: phases, statuses, dates, names, combinations
neuroscope/resolver.py   the merge rules above
neuroscope/ingest.py     per-study transactions, snapshots, change log
neuroscope/curate.py     review and correction CLI
neuroscope/fda.py        FDA approvals (Drugs@FDA) and label indications
neuroscope/pubmed.py     PubMed abstracts, chunking, and drug links
neuroscope/embed.py      chunk embeddings (Vertex AI)
neuroscope/search.py     keyword and semantic search over chunks
neuroscope/tools.py      read-only lookups the agent can call
neuroscope/agent.py      the agent loop, Gemini adapter, and citation check
neuroscope/live_check.py live two-run verification
neuroscope/review.py     per-trial Markdown review
neuroscope/report.py     database summary
tests/                   207 tests; all fixtures synthetic (fake NCT9xxxxxxx IDs), names from live records
```

## Known limitations

- The agent's citation check verifies that identifiers are real, not that the cited source supports the claim. The model also writes its own literature search queries, using its own vocabulary (it turned "brain swelling" into "ARIA-E").
- Agent answers are not reproducible word for word, and a request to Vertex AI can time out (one did during testing and succeeded on retry). There is no automatic retry.
- The agent has no memory between questions and no evaluation set beyond the five questions above.

- Drug classes typed in as interventions (`cholinesterase inhibitors`, `ssri`) exist as drug rows and collect document links like a drug would.
- Document links are whole-word name matches. A short or common name can match text that is not about the drug (`insulin`, `saline`), and names under four characters never link.

- A drug missing from `v_drug_fda_approval` is not proven unapproved. Products match by active ingredient only, so a drug stored under a brand or code name with no verified generic name (`zunveyl`) does not match, and Namzaric does not match because there is no donepezil + memantine drug yet.
- The Alzheimer's flag is a text search of the label, not a reading of it.
- FDA data covers the United States only; there is no EMA or other regulator data.

- Until aliases are reviewed, the same drug can exist twice as auto-created drugs (e.g. `bapineuzumab` and `AAB-001`); records naming both become conflicts.
- Instruction-like names are not cleaned by rule (`Donepezil drug treatment`, `Administer Leqemi 10 mg/kg, every two weeks.`). The ones in the first full load were mapped by hand; new ones need `curate map`.
- Six interventions stay pending on purpose: an ApoE test and a drug-class arm (not specific drugs), two COVID-19 vaccine arms, and two BCG records where `Bacillus of Calmette and Guerin` is misread as a combination because of the word `and`.
- About 20 long descriptive names (`Standard of Care medication for early Alzheimer's Disease`) still exist as single-trial drugs.
- Formulation and extract variants are kept as separate drugs on purpose (`AGB101` vs levetiracetam, `EGb 761` vs ginkgo biloba, intranasal insulin vs insulin).
- Alternatives are detected only by the word `or`; other phrasings (`either A/B`, `A vs B`) are not recognized.
- A one-word otherName can still become a poor display name: a brand (`Interferon beta-1a` is named `rebif`) or a sponsor's misspelling (`flortauapir`).
- `reresolve` re-links trials but never renames drugs. Drugs created before the display-name rule keep their old names (rename with `curate map`).
- Sponsors sometimes put procedures or other arms in `otherNames`. Those names are learned as unreviewed aliases of the record's drug; they no longer affect other trials.
- Dietary supplements and other non-drug intervention types are stored but not resolved to drugs.
- Combination detection uses separators (`+`, `/`, `&`, `;`, `and`, `plus`, `in combination with`). An unusual phrasing can be missed, and a name that legitimately contains one of these words would be split.
- Seed aliases are curated by hand. Verify them against primary sources.
- Migrations are plain idempotent SQL files that `db init` re-applies on every run. There is no version table and no rollback.
