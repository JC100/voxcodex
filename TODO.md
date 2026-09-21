# TODO

## Status

- Every finding from the 2026-08-31 code review is done: Critical (C1-C4),
  High (H1-H10), Medium (M1-M10), and Low (L1-L13) below.
- The library-page progress sync gap (mid-book `percent_complete` /
  `time_remaining_seconds`) -- the last thing standing between here and a
  public 1.0 release -- is now closed too (see "Closed: mid-book progress
  sync" below). No open work remains from either the review or the sync
  investigation.

Full finding detail (rationale, suggested fix) lives in
`docs/code-review-2026-08-31.html`. Its line numbers are stale after the
M1-M10 rewrites -- relocate a finding by file/description, not by line.

## Open work

None -- the mid-book progress sync gap (below) was the last item, and it's
now closed.

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
