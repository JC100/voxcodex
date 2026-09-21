# TODO

## Status

- Every finding from the 2026-08-31 code review is done: Critical (C1-C4),
  High (H1-H10), Medium (M1-M10), and Low (L1-L13) below.
- The library-page progress sync gap (mid-book `percent_complete` /
  `time_remaining_seconds`) is closed (see "Closed: mid-book progress
  sync" below).
- **2026-09-21: the planned second code review is done** -- four
  fresh-context reviewers plus manual verification, full detail in
  `docs/code-review-2026-09-21.html`. 0 Critical, 6 High, 16 Medium, 27
  Low. This is now the last thing standing before 1.0; see "Open work"
  below for the punch list.

Full finding detail (rationale, suggested fix) for either review lives in
`docs/code-review-2026-08-31.html` / `docs/code-review-2026-09-21.html`.
The 08-31 doc's line numbers are stale after the M1-M10 rewrites --
relocate a finding by file/description, not by line.

## Open work

From `docs/code-review-2026-09-21.html` (not yet actioned):

- [x] H1 -- Resuming a "Finished" book seeks to the end instead of
      restarting (`screens/library.py` / `screens/player_screen.py`). Also
      the still-open Major finding from PR #2's CodeRabbit review.
      **Fixed** 2026-09-21: `LibraryScreen._launch_player` now passes the
      already-computed `session_start_position_ms` into `PlayerScreen` as
      an explicit `start_position_ms` constructor kwarg, instead of
      `PlayerScreen.on_mount` re-deriving it from `book.is_finished` --
      which by then had already been cleared by the same method. Added
      `test_resuming_a_finished_book_starts_mpv_at_zero_not_at_the_stale_progress`,
      which asserts the actual mpv start position through the full
      `LibraryScreen` -> `p` -> `PlayerScreen` path (the class of test the
      review noted was entirely missing).
- [ ] H2 -- A duplicate ASIN silently truncates the library table, then
      crashes on the next keypress (`screens/library.py`).
- [x] H3 -- A transient mpv read failure silently zeroes the saved
      position and pushes that to Audible (`screens/player_screen.py` /
      `services/player.py`). **Fixed** 2026-09-21: `MpvPlayer.position_seconds`
      no longer defaults to 0.0 on a failed IPC read -- it now raises
      `MpvError`, which `_poll_player`'s existing `except MpvError: return`
      already skips the tick on, and `action_close` now catches to keep
      the last known-good position instead of overwriting it. Together
      with M9 (below), also deleted the dead `_tick(snap=None)` branch
      that let this ship untested, and re-pointed the affected tests at
      real `_Playback` snapshots / the real `_poll()` path. Added
      `test_poll_skips_a_tick_instead_of_committing_a_failed_read_as_zero`
      (player_screen) and `test_position_seconds_raises_instead_of_defaulting_when_not_connected`
      / a dropped-connection assertion (player), covering the exact gap
      the review noted (`FakePlayer.position_seconds` never raised).
- [ ] H4 -- Amazon account password and vault password leak into Textual
      worker descriptions/logs (`screens/login.py`).
- [x] H5 -- Unvalidated DRM key/IV are newline-injectable into the mpv
      options file (`services/player.py`). **Fixed** 2026-09-21:
      `MpvPlayer.start` now rejects any key/iv that isn't a plain,
      even-length hex string via a new `_require_hex` helper, raising
      `MpvError` before anything is written to the options file. Added
      `test_start_rejects_non_hex_key_or_iv` (parametrized, including the
      concrete newline+`script=` injection) and
      `test_start_accepts_plain_hex_key_and_iv`.
- [x] H6 -- The stream URL is passed to mpv as a bare positional arg with
      no `--` terminator (`services/player.py`). **Fixed** 2026-09-21:
      `start()` now appends a literal `--` before `source` in the mpv
      argument list (and keeps `--start=` before that terminator, not
      after it). Added `test_start_puts_a_double_dash_terminator_before_the_source`,
      `test_start_with_a_leading_dash_source_is_not_treated_as_an_option`,
      and `test_start_with_a_start_seconds_puts_it_before_the_dash_terminator`.
      Also updated the existing key/iv tests off non-hex placeholder
      values ("key", "iv", "thekey", ...) now that H5 rejects them.
- [x] M9 -- A dead `_tick(snap=None)` code path is what most of the
      player-screen test suite actually exercised, masking H3 from the
      tests (`screens/player_screen.py` / `tests/test_player_screen.py`).
      **Fixed** 2026-09-21 alongside H3: `snap` is now a required
      parameter, the dead branch is deleted, and tests either build a real
      `_Playback` snapshot or go through the real `_poll()` path
      (`test_poll_shows_finished_when_mpv_has_exited`, renamed from the
      old direct-`_tick()` version, since the is_running check it exercises
      lives in `_poll_player`, not `_tick`).
- [ ] 15 more Medium and 27 Low findings -- see the doc for the full list
      and suggested order of work.
- [ ] Still-open Minor findings from PR #2's external review: `CLAUDE.md:68`
      stale step cross-reference; contradictory TL;DR in
      `docs/library-progress-sync-investigation.md:23-30`; the
      finished-state explanation below (under "Closed: mid-book progress
      sync") is stale -- it claims VoxCodex deliberately does *not*
      auto-clear `is_finished` on resume, but the follow-up two entries
      below it says the opposite is now true.

## Closed: mid-book progress sync (was the last thing before 1.0)

- [x] **Mid-book progress didn't sync to Audible's own library tile --
      fixed and confirmed live, 2026-09-21.** `percent_complete` /
      `time_remaining_seconds` didn't update from a VoxCodex play -- root
      cause found: they're driven by `PUT /1.0/stats/events` `Listening`
      events, whose exact accepted payload shape was never pinned down. A
      guessed shape was tried and made it *worse* (drove the percentage
      to 0% instead of the real value).
      **Captured** the real payload via network-level MITM (mitmproxy on
      a dedicated proxy box + Android CA-trust bind-mount) against the
      real Android app talking to the real backend -- confirmed schema
      for `Listening`/`StartListening`, full capture + rig notes in
      `docs/library-progress-sync-investigation.md` (2026-09-21 section).
      **Implemented** the same day: `AudibleAPI.push_listening_session` /
      `services.progress.push_listening_session` send a `StartListening` +
      `Listening` pair on player close, using the license/voucher's
      `license_id` (new field -- titles saved before this existed need a
      fresh `get_license()` or re-download before this can push for them)
      and the session's real start/end position and wall-clock time.
      Deliberately does *not* mirror the real app's accompanying
      `MarkAsUnfinished` on every play -- VoxCodex already has an
      explicit, user-triggered way to un-finish a book, and auto-clearing
      it just because playback resumed would fight that.
      **Confirmed against the live account, same day:** played
      `B01L790CUU` for real (~3 minutes, via VoxCodex itself, not a
      synthetic call) and re-read the raw library response immediately
      after --
      `percent_complete: 1.0`, `time_remaining_seconds: 18829` (out of a
      19,020s book, ~191s in -- exactly right, not 0% and not a
      coincidence: matches the actual position to the second). Updated
      within ~15-20s of the push, not the tens-of-minutes lag seen with
      the old broken payload shape -- the earlier guess wasn't just wrong
      in content, it may have also been hitting a genuinely slower
      recompute path. `is_finished` correctly stayed `True` (this test
      book was already finished; the deliberate no-`MarkAsUnfinished`
      choice above held).
      **Follow-up, same day:** the user pointed out that a book marked
      finished on Audible needs to un-finish the moment you *resume* it
      in VoxCodex, not stay stuck showing "Finished" on other devices
      while you're actively re-listening. `LibraryScreen._launch_player`
      now clears `is_finished` + pushes `set_finished(asin, False)`
      immediately on that transition (once, not per checkpoint). Also
      tried, live, sending a zero-length listening-session event at the
      same moment to reset the tile's stale `percent_complete` too --
      confirmed a genuine no-op server-side (not recompute lag: checked
      twice, 20+s apart, byte-for-byte unchanged) -- removed rather than
      shipped as dead weight. So: `is_finished` clears instantly;
      `percent_complete`/`time_remaining_seconds` stay stale until that
      session's own close, same as any other session.
      Full trail: `docs/library-progress-sync-investigation.md`,
      `docs/whispersync-research.md`; also noted in `CHANGELOG.md` and
      `README.md`.

## Low-priority findings (L1-L13)

- [x] L1 -- `MessageModal` (`screens/modals.py`) is dead code: defined and
      tested, never instantiated in production. Deleted it and its test.
- [x] L2 -- `License.last_position_ms` (`services/api.py`) is fetched but
      never read. Resolved against the local position by recency (same
      `_resolve_progress_ms` M5 added) right before opening a streaming
      title in `_open_player`.
- [x] L3 -- `set_finished(asin, False)` (`screens/library.py`) is
      unreachable -- no UI un-finishes a book. Added a "u" (unmark
      finished) keybinding.
- [x] L4 -- `on_authenticated` (`app.py`) is a plain method called
      directly, shadowing Textual's `on_*` handler convention. Made it a
      real `LoginScreen.Authenticated` message, handled via `@on(...)` on
      the app.
- [x] L5 -- Search (`screens/library.py`, `_apply_filters_and_sort`)
      re-sorts and rebuilds the whole table on every keystroke. Debounced
      with a 150ms `set_timer`.
- [x] L6 -- `Settings(path=config.SETTINGS_FILE)` (`services/settings.py`,
      and the equivalent in `progress.py`) binds the default path at
      import, not at call time. Both now take `path: Path | None = None`
      and resolve `config.SETTINGS_FILE`/`config.PROGRESS_CACHE_FILE`
      inside `__init__`.
- [x] L7 -- `table.add_row(key=book.asin)` (`screens/library.py`) can
      raise `DuplicateKey` if `book.asin` is empty/missing. `get_library`
      now skips (and logs) ASIN-less items at parse time.
- [x] L8 -- `_reset` (`screens/login.py`) pops its own screen then pushes
      a replacement from inside that screen's button handler. Switched to
      `switch_screen`.
- [x] L9 -- "Finished" is defined two ways: the library filter used
      `progress_pct >= 100`, `_reached_end` uses `_FINISHED_FRACTION`
      (98%). Filter now uses `_FINISHED_PCT` (98), derived from the same
      constant.
- [x] L10 -- `watch_theme` (`app.py`) writes to disk during `__init__`, so
      every launch does a settings write before the UI renders. Guarded
      with a `_loading_theme` flag.
- [x] L11 -- `import webbrowser` (`screens/login.py`,
      `_external_url_prompt`) is inside a function. Moved to module scope.
- [x] L12 -- `quality` (`services/api.py`, two call sites) silently
      coerced any non-"normal" value to "High". Added `_api_quality`,
      shared by both, which raises `ValueError` on anything else.
- [x] L13 -- `downloads/` has no UI affordance for total size, bulk
      cleanup, or per-item size display. Added a "Size" column, a total
      downloaded size in the sort/filter label, and a "X" (delete finished
      downloads) bulk-cleanup keybinding.

## Known flaky tests

- `tests/test_player_screen.py::test_poll_worker_reads_mpv_off_the_event_loop_and_renders`
  failed once in CI on Python 3.10 (`assert True is False` at
  `test_player_screen.py:872`), on a run from before this session's L-item
  work -- not reproduced locally or on any other CI run seen so far.
  Pre-existing, unrelated to the 2026-08-31 review. If it recurs: it's
  timing-sensitive (event-loop-vs-thread-poll ordering), so look there
  first rather than assuming a fresh regression. Not investigated further
  yet because it's a single occurrence.

## Keeping this file current

Whenever a task here is finished, check it off (or delete it) in the same
commit that does the work -- see "Definition of done" in `CLAUDE.md`. This
file should never need a separate "update the TODO" pass.
