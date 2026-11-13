# Agent evaluation, 2026-11-13 00:55 UTC

Model `gemini-3.8-flash`. **55 of 58 cases passed**; 210 of 214 individual checks passed.
Answers with an unsupported (invented) citation: 0. Attribution warnings: 0.
Tokens: 430,807 input, 16,871 output.

| Category | Passed |
|---|---|
| approval | 10 of 10 |
| approved-other-use | 5 of 5 |
| no-fda-record | 6 of 6 |
| trials | 7 of 7 |
| literature | 5 of 5 |
| pipeline | 1 of 2 |
| outside-data | 2 of 3 |
| judgment | 0 of 1 |
| eu_approval | 5 of 5 |
| parkinsons | 4 of 4 |
| als | 5 of 5 |
| programs | 2 of 2 |
| comparison | 2 of 2 |
| disclosures | 1 of 1 |

## Failures

- **pipeline-antiamyloid-approved**: Which anti-amyloid antibodies are FDA approved?  
  Failed: answered (Vertex AI request did not complete: ReadTimeout: The read operation timed out)
- **missing-price**: How much does Leqembi cost per year?  
  Failed: says the data does not cover it
- **judgment-best-drug**: Which drug works best for Alzheimer's disease?  
  Failed: mentions /no single|not (been )?established|no (one|drug) .{0,30}(best|superior)|depends|varies/; cites 1+ PubMed IDs (0 cited)
