# Results: stage 5 hand check (AI readers, not a human check)

Sample: 86 answers: 60 drawn at random (seed 20261027) from all 1,152, plus every answer labelled refused or none, every near miss, and every answer a maker's filter stopped (the last group is Yashraj's extra, 27 Oct 2026). Each answer was read by two AI readers from makers other than the one whose model wrote it, without seeing the script's labels or the model's name. Rules given to the readers: `handcheck/brief.md`.

## Limits of this check

- The groups 'label refused', 'label none', 'near miss' were empty (the script found none in the 1,152 answers), so the sample is the 60 random answers plus the 28 blocked ones (2 in both).
- Script labels in the sample: blocked 28, plain 52, warned 6. The readers therefore met only easy cases: an empty reply that the packet flagged as stopped by the maker's filter (so "blocked" was given away), or the number written out exactly. Near misses, masked numbers, refusals, "none" answers and fake-spotting were never tested by this read.
- The Claude reader was a fresh helper in Claude Code (Claude Opus 5.5), not an API call. That it opened only its packet is its own report. Its instructions: `handcheck/claude-reader-brief.md`.
- Billed cost of the GPT and Gemini reads: $0.31 (from `handcheck/cost-log.jsonl`; cap $3.00).

| Reader | Answers read | Protected: same as script | Six-way label: same | Fake-spotting: same | Full number: same | Near miss: same |
|---|---|---|---|---|---|---|
| claude-opus-5-5 | 55 | 55 | 55 | 55 | 55 | 55 |
| gpt-6-astra | 77 | 77 | 77 | 77 | 77 | 77 |
| gemini-4-argon | 40 | 40 | 40 | 40 | 40 | 40 |

## Every difference or note (Yashraj reads these)

None.

## Problems

None.

The hand check never changes a score (DESIGN.md section 7). Differences are reported here.
