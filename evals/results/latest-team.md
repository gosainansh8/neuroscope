# Agent evaluation, 2026-11-13 06:24 UTC

Model `gemini-3.8-flash, team agent`. **63 of 66 cases passed**; 255 of 258 individual checks passed.
Answers with an unsupported (invented) citation: 0. Attribution warnings: 4.
Tokens: 0 input, 0 output.
Median answer time 29.7 s; median 43.3 generated tokens/s; 7.6 model calls and $0.01434 per answer on average ($0.9467 in all, estimated from token counts).

| Category | Passed |
|---|---|
| approval | 9 of 10 |
| approved-other-use | 5 of 5 |
| no-fda-record | 5 of 6 |
| trials | 7 of 7 |
| literature | 5 of 5 |
| pipeline | 2 of 2 |
| outside-data | 2 of 3 |
| judgment | 1 of 1 |
| eu_approval | 5 of 5 |
| parkinsons | 4 of 4 |
| als | 5 of 5 |
| programs | 2 of 2 |
| comparison | 2 of 2 |
| disclosures | 1 of 1 |
| multi-part | 8 of 8 |

## Failures

- **fda-kisunla-brand**: Is Kisunla approved, and when?  
  Failed: mentions /donanemab/
- **norecord-masitinib**: Is masitinib FDA approved? It finished a Phase 3 trial.  
  Failed: mentions /no (matched )?FDA approval record|no matched FDA/
- **missing-japan**: Is lecanemab approved in Japan?  
  Failed: says the data does not cover it

## Attribution warnings

- multi-istradefylline: NCT00955045 belongs to istradefylline, but its sentence names levodopa
- multi-istradefylline: NCT00957203 belongs to istradefylline, but its sentence names levodopa
- multi-istradefylline: NCT05885360 belongs to istradefylline, but its sentence names levodopa
- multi-tavapadon: 0001551152-26-000008 belongs to ABBV-1758, adalimumab, botulinum toxin, carbidopa, dexamethasone, lenalidomide, levodopa, levodopa-carbidopa intestinal gel, but its sentence names tavapadon
