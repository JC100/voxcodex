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

## Definition of done
Applies to any code change — a bug fix, a feature, a refactor, a review
finding, doesn't matter which:
1. Implement it.
2. Add or extend tests covering it (see Testing below) — not optional, and
   not limited to just the lines touched.
3. Run the full test suite; it must be green.
4. Run `ruff check voxcodex tests` and `mypy`; both must be clean.
5. If this closes, advances, or contradicts anything in `TODO.md`, update
   `TODO.md` in the same commit — never a separate "update the TODO" pass.
6. Commit, then push.
7. Check that CI actually went green (`gh run list` / `gh run view` on the
   branch) — a clean local run does not guarantee this (see Dev environment
   below). Don't consider the work item done on the strength of the local
   run alone.

## Testing
- Tests are allowed to fail — a failing test is a signal of real work still
  to do, and belongs on `TODO.md`, not hidden.
- Never add a try/except (or other suppression) inside a test to make a
  failure disappear, unless that exception is one we deliberately built
  into the production code itself.
- Write tests as thoroughly as practical, covering as much of the codebase
  as we reasonably can — not just the lines touched by the current change.
  Breadth is what catches a "small" change having wider-reaching effects
  than its stated scope.
- Tests verify correctness; they don't define the solution — don't shape
  code to pass a specific test rather than to be correct.
- Test doubles follow this repo's existing convention: a `Fake*` class per
  dependency (`FakeAPI`, `FakeProgressStore`, `FakeSettings`,
  `FakeLibraryCache`, `FakeChapterCache`, ...), wired in via an autouse
  fixture that monkeypatches the *module-level* reference (e.g.
  `library_module.chapter_cache`) — not `unittest.mock`.

## Dev environment
- `.venv-dev` has pytest, ruff, and mypy installed — use it
  (`.venv-dev/bin/pytest`, `.venv-dev/bin/ruff`, `.venv-dev/bin/mypy`) for
  all local checks. The plain `.venv` and the system Python don't have
  these.
- `.venv-dev` runs Python 3.12, one point release among several in CI's
  test matrix (currently 3.11-3.13; see `.github/workflows/tests.yml`). A
  dependency can resolve to a *different* version per interpreter (pip
  picks the newest release compatible with that specific Python) — so a
  clean local run does not prove every CI Python version passes. Hit this
  for real: `audible>=0.11` (needed for `audible.exceptions.AudibleError`,
  used since the M7 fix) requires Python>=3.11 itself, so at the time 3.10
  was still in the matrix it silently resolved the two-versions-old
  `audible==0.10.0` instead — which lacked that class entirely, breaking
  every 3.10 CI run (and the real app under 3.10) for 18 consecutive
  commits before anyone checked CI. Python 3.10 support was dropped as the
  fix (see git log around that date) rather than pinning to the older
  dependency surface forever. Lesson: check CI (step 7 of Definition of
  done above), don't just trust a green local run.

## Project structure
- `voxcodex/screens/` — Textual screens (UI): library, login, player,
  modals.
- `voxcodex/services/` — everything else: the Audible API client, auth,
  downloads, the local progress/library/chapter caches, settings, the mpv
  player wrapper.
- `voxcodex/config.py` — all filesystem paths (config/data dirs,
  individual files) and the shared private-write helpers.
- `tests/` — one file per `voxcodex/` module, same name.

## Backlog
- `TODO.md` is the live task list. Keep it current as part of doing the
  work (see Definition of done above), not as a separate reminder step.
- `docs/code-review-2026-08-31.html` is the full original code review
  (Critical/High/Medium/Low, rationale + suggested fix per finding).
  Critical, High, and Medium are done (see `TODO.md` and git log). Its line
  numbers are stale after the M1-M10 rewrites — relocate a finding by
  file/description, not by line.
