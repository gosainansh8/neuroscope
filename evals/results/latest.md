# Agent evaluation, 2026-11-13 05:40 UTC

Model `gemini-3.8-flash`. **63 of 66 cases passed**; 252 of 255 individual checks passed.
Answers with an unsupported (invented) citation: 1. Attribution warnings: 3.
Tokens: 466,510 input, 20,339 output.
Median answer time 13.2 s; median 165.6 generated tokens/s; 3.4 model calls and $0.00646 per answer on average ($0.4262 in all, estimated from token counts).

| Category | Passed |
|---|---|
| approval | 10 of 10 |
| approved-other-use | 5 of 5 |
| no-fda-record | 6 of 6 |
| trials | 7 of 7 |
| literature | 4 of 5 |
| pipeline | 2 of 2 |
| outside-data | 2 of 3 |
| judgment | 0 of 1 |
| eu_approval | 5 of 5 |
| parkinsons | 4 of 4 |
| als | 5 of 5 |
| programs | 2 of 2 |
| comparison | 2 of 2 |
| disclosures | 1 of 1 |
| multi-part | 8 of 8 |

## Failures

- **lit-lecanemab-apoe**: Is there published evidence on lecanemab and APOE4?  
  Failed: answered (Vertex AI request did not complete: ReadTimeout: The read operation timed out)
- **missing-japan**: Is lecanemab approved in Japan?  
  Failed: says the data does not cover it
- **judgment-best-drug**: Which drug works best for Alzheimer's disease?  
  Failed: no unsupported citations (NDA021487)

## Attribution warnings

- multi-istradefylline: NCT00199407 belongs to istradefylline, but its sentence names [18F]DOPA, levodopa
- multi-istradefylline: NCT05885360 belongs to istradefylline, but its sentence names [18F]DOPA, levodopa
- multi-tavapadon: 0001551152-26-000008 belongs to ABBV-1758, adalimumab, botulinum toxin, carbidopa, dexamethasone, lenalidomide, levodopa, levodopa-carbidopa intestinal gel, but its sentence names tavapadon
