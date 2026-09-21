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
- [x] H2 -- A duplicate ASIN silently truncates the library table, then
      crashes on the next keypress (`screens/library.py`). **Fixed**
      2026-09-21: `AudibleAPI.get_library` now dedupes by ASIN (keep first
      occurrence, log the duplicate) the same way it already handled the
      empty-ASIN case, tracking `seen_asins` across the whole pagination
      loop since a repeat can span pages. Added
      `test_get_library_dedupes_repeated_asins_within_a_page` and
      `test_get_library_dedupes_an_asin_repeated_across_pages`.
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
- [x] H4 -- Amazon account password and vault password leak into Textual
      worker descriptions/logs (`screens/login.py`). **Fixed** 2026-09-21:
      added an explicit `description=` to the `@work` decorators on
      `_do_login`, `_do_unlock`, and `_do_external_login` -- Textual skips
      the `repr()`-the-positional-args fallback (which put both passwords
      in plaintext on the Worker) whenever one is given. Added three tests
      asserting the actual `Worker.description` doesn't contain the
      account or vault password.
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
- [x] M13 -- The table's active filter and the downloaded-size label go
      stale after download, delete, unmark, or a playback session
      (`screens/library.py`). **Fixed** 2026-09-21: the five handlers
      (`_download_succeeded`, the two delete confirmations,
      `action_unmark_finished`, and `_on_progress`'s final branch) now
      call `_apply_filters_and_sort()` instead of `_refresh_table()`, so
      the filtered list and the size/count label are recomputed instead of
      just re-rendered stale. Added
      `test_download_succeeded_recomputes_the_active_filter`,
      `test_delete_confirmed_updates_the_total_downloaded_size_label`, a
      size-label assertion in the existing delete-finished-downloads test,
      and `test_unmark_finished_recomputes_the_active_filter`.
- [x] M15 -- "Unmark finished" (`u`) can leave a book unreachable by any
      filter (`screens/library.py`). **Fixed** 2026-09-21:
      `action_unmark_finished` now also pulls `progress_ms` back to just
      under the finished threshold when it's still at/above it, so the
      book actually lands in "In progress" instead of staying stuck
      matching only "Finished". Added
      `test_unmark_finished_makes_the_book_reachable_by_in_progress_filter`.
- [x] M16 -- The Chapter column doesn't advance after a listening session
      (`screens/library.py`). **Fixed** 2026-09-21: `_on_progress`'s final
      branch now recomputes `chapter_total`/`chapter_current` from
      `_chapter_cache` (not the session's local `chapters`, which can be
      an empty placeholder from a deliberately-uncached transient fetch
      failure -- see `_open_player`) before the table rebuild. Added
      `test_chapter_column_advances_after_a_listening_session_closes` and
      `test_chapter_column_unchanged_when_the_chapter_fetch_never_succeeded`.
- [x] M1 -- `get_license` is missing the `status == "Exists"` guard its
      sibling parser requires (`services/api.py` vs. `services/progress.py`).
      **Fixed** 2026-09-21: added `parse_last_position_heard` (mirroring
      `_existing_last_position_heard`'s guard) and used it in `get_license`.
      Added `test_get_license_ignores_a_does_not_exist_position`.
- [x] M2 -- Remote-position lookups sent every ASIN in the library as one
      unchunked query string (`services/progress.py`). **Fixed**
      2026-09-21: `fetch_remote_annotations` now chunks the ASIN list
      (100/request) and merges results across chunks, logging at WARNING
      (was DEBUG) when a chunk fails outright. Added
      `test_fetch_remote_annotations_chunks_a_large_asin_list` and
      `test_fetch_remote_annotations_merges_across_a_failed_chunk`.
- [x] M3 -- A non-JSON 200 response crashes the player/chapter workers
      instead of showing an error (`services/api.py`). **Fixed**
      2026-09-21: added `InvalidResponse`, raised from `get_license` and
      `get_chapters` when the response isn't a dict, and added to
      `_PLAYER_OPEN_ERRORS`/`_CHAPTER_FETCH_ERRORS` in `library.py`. Added
      `test_get_license_raises_invalid_response_on_a_non_json_200`,
      `test_get_chapters_raises_invalid_response_on_a_non_json_200`,
      `test_play_surfaces_a_non_json_license_response_as_a_playback_failure`,
      and `test_play_still_works_when_chapter_metadata_is_non_json`.
- [x] M4 -- `most_recent_external_play`'s premise is now false, and the
      feature it feeds is dead, write-only state (`services/progress.py` /
      `services/settings.py`). **Fixed** 2026-09-21: deleted
      `most_recent_external_play`, `Settings.last_played_in_app` /
      `last_played_externally` (getters, setters, and the `_last_played`
      helper), and their only call sites (`LibraryScreen._load` and
      `PlayerScreen._start_succeeded`) -- nothing read either value
      anywhere in the app, and the "this app's own plays don't reach this
      endpoint" premise the function's docstring relied on stopped being
      true once the mid-book sync push shipped. Removed the now-dead tests
      in `test_progress.py`, `test_settings.py`, `test_library_screen.py`,
      and `test_player_screen.py`.
- [x] M5 -- `ProgressStore` has no type guard on cache entries, unlike its
      siblings -- a malformed entry takes down the whole library load
      (`services/progress.py`). **Fixed** 2026-09-21: `get_position_ms`
      and `get_updated_at` now guard against a non-dict entry (not just a
      non-numeric value inside one), and `set_position_ms` replaces a
      non-dict entry instead of trying to mutate it (which would raise
      `TypeError` on item assignment). Added four tests covering both
      malformed shapes on read and the replace-on-write behavior.
- [x] M6 -- `MpvPlayer.start()` can leak a temp dir holding the DRM key,
      and a non-`MpvError` startup failure wedges the player screen
      forever (`services/player.py` / `screens/player_screen.py`).
      **Fixed** 2026-09-21: `Popen` now runs inside the same cleanup
      `try` as `_connect()`, so a `FileNotFoundError` (or any other
      startup failure) calls `self.stop()` and cleans up the temp dir
      instead of leaking it. `PlayerScreen._start_player` now also
      catches `OSError` (was only `MpvNotFoundError`/`MpvError`), so a
      real-world failure of that shape shows an error instead of wedging
      the screen on "Starting player..." forever. Added
      `test_start_cleans_up_the_temp_dir_when_popen_itself_fails` and
      `test_start_failure_from_os_error_also_shown`.
- [x] M7 -- Two downloads racing on the same book collide on one temp
      filename, losing the download and orphaning its voucher
      (`services/download.py` / `screens/library.py`). **Fixed**
      2026-09-21: `download_book` now uses `tempfile.mkstemp` for a
      unique temp name per attempt (the `*.part` sweep glob still
      matches), and the voucher write + rename are inside the same
      cleanup `try` so a failure in either also removes the other.
      `action_download_selected` now also rejects a same-book
      double-press with a status message via a new
      `_in_flight_downloads` set, instead of racing a second worker.
      Added six tests across `test_download.py` and
      `test_library_screen.py` covering the unique-tmp-name behavior, a
      concrete two-attempts-in-flight race, voucher cleanup on a failed
      rename, the double-press rejection, and retry-after-failure.
- [x] M8 -- Transport keys, close, and unmount all do blocking mpv IPC on
      the Textual event loop (`screens/player_screen.py`). **Fixed**
      2026-09-21: `_control` now dispatches every transport command
      (play/pause/seek/speed/volume) to a background worker instead of
      running it inline, mirroring how the poll path already avoids the
      event loop. `action_close` similarly moves its position read +
      `stop()` off the event loop, dismissing via `call_from_thread` once
      they finish. `on_unmount`'s `stop()` is deliberately left
      synchronous (documented why in a comment) -- the common case is
      already a fast no-op after `action_close`, and backgrounding the
      rare hard-quit path risks the process exiting before the worker
      runs, leaking the mpv subprocess. Added
      `test_transport_command_does_not_block_the_event_loop` and
      `test_action_close_does_not_block_the_event_loop`; adjusted a
      couple of existing tests that asserted on a now-async side effect
      without waiting for it.
- [ ] 4 more Medium and 27 Low findings -- see the doc for the full list
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
