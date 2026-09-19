# Working conventions for this repo

## Commits & pushing
- Commit at least once per discrete item of work (e.g. one commit per finding
  when working through a review list), not one giant commit at the end.
- Push to the remote regularly — after each commit, or at worst every few
  commits. The user's laptop is older hardware; uncommitted/unpushed local
  work is at real risk of being lost to drive failure.
- Do work on a feature/fix branch, never directly on `main`. `main` is kept
  clean for production use. Open a PR to merge into `main` once the branch
  is in a good state — branches double as incremental backups along the way,
  even before the work is "done".

## Review findings
- `scratch/code-review-2026-08-31.html` is the source-of-truth list of
  findings from the 2026-08-31 code review (Critical/High/Medium/Low, IDs
  C1-C4, H1-H10, M1-M10, L1-L13). Critical and High are fixed (see commits
  `0e0dbf8`, `4cabca0`, `56ddb6a`, `3c5e526` on `fix/critical-review-findings`).
  Medium and Low are being worked through in order.
