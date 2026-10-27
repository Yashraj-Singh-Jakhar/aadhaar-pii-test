# Changes after the freeze

Every change to a frozen file after the first freeze is listed here, with the date, the reason and the rule in DESIGN.md that allows it. None yet.

## 27 Oct 2026: 19 void Gemini trial lines set aside (raw data, not a frozen file)
- What: 19 lines for `gemini-3.5-flash-lite` moved from `data/trial.jsonl` to `data/void/trial-2026-10-27-bad-project-id.jsonl`. A copy of `data/trial.jsonl` as it was before the move: `data/void/trial-before-void-move-2026-10-27.jsonl`.
- Why: the Google Cloud project ID typed at the start held a stray space, so Python refused to build the web address (`InvalidURL`) before any request was sent. No request reached Google and nothing was billed, but `run.py` booked each try at the worst case ($2.98 in all, not spent), which hit the $3.00 step 1 stop before the cost probe. Real step 1 spend at that point: about $0.011 (Claude Haiku 5.5 and GPT-6 Luna, 48 answers each, unaffected and kept).
- Rule: DESIGN.md section 6 counts spend from billed token counts; these tries had none. Yashraj's decision, 27 Oct 2026: "Yes: move the 19 void Gemini lines to data/void (kept, logged in CHANGES.md), then rerun step 1 with the project ID on the command line."
- Known gap left in `run.py` (frozen, not changed): an address refused on this computer is booked as "may have been billed". The rerun gives the project ID on the command line (`GOOGLE_CLOUD_PROJECT=...`), so it is not typed.
- Undo: `cp data/void/trial-before-void-move-2026-10-27.jsonl data/trial.jsonl`
