# Agent evaluation, 2026-11-12 09:37 UTC

Model `gemini-3.8-flash`. **45 of 53 cases passed**; 181 of 190 individual checks passed.
Answers with an unsupported (invented) citation: 0. Attribution warnings: 0.
Tokens: 490,508 input, 21,626 output.

| Category | Passed |
|---|---|
| approval | 10 of 10 |
| approved-other-use | 5 of 5 |
| no-fda-record | 4 of 6 |
| trials | 6 of 7 |
| literature | 4 of 5 |
| pipeline | 0 of 2 |
| outside-data | 2 of 3 |
| judgment | 1 of 1 |
| eu_approval | 5 of 5 |
| parkinsons | 4 of 4 |
| als | 4 of 5 |

## Failures

- **norecord-gantenerumab**: Is gantenerumab approved?  
  Failed: verdict (expected no, got unclear)
- **norecord-trontinemab**: Is trontinemab approved?  
  Failed: verdict (expected no, got unclear)
- **trials-simufilam**: What happened in simufilam's Phase 3 program?  
  Failed: answered (Vertex AI request failed (504 DEADLINE_EXCEEDED): Deadline expired before operation could complete.)
- **lit-donanemab-aria**: What does the literature say about brain swelling with donanemab?  
  Failed: answered (Vertex AI request failed (504 DEADLINE_EXCEEDED): Deadline expired before operation could complete.)
- **pipeline-phase3-unapproved**: Which drugs have an active Phase 3 trial but no FDA approval record?  
  Failed: mentions /trontinemab|remternetug/
- **pipeline-antiamyloid-approved**: Which anti-amyloid antibodies are FDA approved?  
  Failed: mentions /lecanemab/; mentions /donanemab/
- **missing-europe**: Is lecanemab approved in Europe?  
  Failed: says the data does not cover it
- **als-relyvrio**: Is AMX0035 FDA approved, and what happened in Europe?  
  Failed: verdict (expected yes, got unclear)
