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
- [x] M10 -- The log file loses its 0600 permissions on the first
      rotation (`app.py`). **Fixed** 2026-09-21: added
      `_PrivateRotatingFileHandler`, which re-`chmod`s the file to 0600
      inside `_open()` after every call (initial delayed open and every
      reopen after a rollover), rather than relying on the one-time
      pre-creation chmod that a plain `open()` after rotation silently
      undid. Added `test_log_file_is_created_at_0600` and
      `test_log_file_keeps_0600_permissions_after_rotation` (the latter
      forces a rollover via `doRollover()` directly rather than writing a
      megabyte of log lines, and checks both the live file and its `.1`
      backup).
- [x] M11 -- `chapter_cache.load()` raises on a non-dict cache file,
      despite documenting that it never raises (`services/chapter_cache.py`).
      **Fixed** 2026-09-21: added an `isinstance(data, dict)` guard (the
      root cause was `.items()` on a non-dict raising `AttributeError`,
      which wasn't in the except tuple -- also added there for defense in
      depth). Created `tests/test_chapter_cache.py`, which didn't exist at
      all despite the repo's one-file-per-module convention -- covers
      round-tripping, a corrupted file, a drifted `Chapter` schema, all
      five non-object top-level JSON shapes (list/null/string/number/
      bool), and a best-effort `save()` failure.
- [x] M12 -- `playback_speed`/`playback_volume` crash on a
      corrupted-but-parseable settings value (`services/settings.py`).
      **Fixed** 2026-09-21: added a `_float(key, default, min_value,
      max_value)` helper -- guards the conversion (`TypeError`/
      `ValueError`) and clamps the result to each property's valid range,
      since an in-range-type-but-out-of-range value (e.g. speed 999)
      otherwise only gets clamped incrementally, by the +/- actions' own
      `min()`/`max()`. Added four tests covering a non-numeric value,
      `null`, and an out-of-range value for each property.
- [x] M14 -- A corrupt or unreadable download voucher hangs the play flow
      forever, with no error shown (`screens/library.py` /
      `services/download.py`). **Fixed** 2026-09-21: `load_voucher` now
      catches `json.JSONDecodeError`/`OSError` and returns `None` on a
      parse failure, the same as a missing voucher -- which
      `_open_player`'s existing "no voucher" handling already turns into
      a clean, user-visible `RuntimeError` (message reworded to cover
      both "missing" and "unreadable"). Added
      `test_load_voucher_returns_none_for_corrupted_json` and an
      end-to-end `test_play_shows_an_error_instead_of_hanging_on_a_corrupt_voucher`
      (against a real corrupted file on disk, not a mocked `load_voucher`).

All 6 High and all 16 Medium findings from the 2026-09-21 review are now
done.

- [x] L1 -- An ASIN is never validated before being used as a filename
      (`services/download.py`). **Fixed** 2026-09-21: added
      `_require_valid_asin` (restricting to `[A-Za-z0-9]+`), applied at
      `voucher_path_for`/`audio_path_for` -- the two places an ASIN first
      becomes a path. `is_downloaded`/`downloaded_size` catch the new
      `InvalidAsin` and fail safe (False/None) since they're called
      unconditionally for every book on every library load; other call
      sites (`download_book`, `_open_player`) are already inside existing
      broad exception handling, so `InvalidAsin` was also added to
      `_PLAYER_OPEN_ERRORS` for a clean message there. Added 3 tests.
- [x] L2 -- `stop()` closes the mpv socket while a concurrent blocking
      `recv` may still be in flight (`services/player.py`). **Fixed**
      2026-09-21: `sock.shutdown(SHUT_RDWR)` before `sock.close()`, which
      forces a concurrent blocked `recv()` to return immediately (as EOF)
      without invalidating the fd, rather than racing a `close()` that
      could let the fd be reused by an unrelated new socket before that
      blocked read wakes up. Added
      `test_stop_shuts_down_the_socket_before_closing_it`.
- [x] L3 -- A SIGKILLed mpv process is never reaped (`services/player.py`).
      **Fixed** 2026-09-21: added `proc.wait(timeout=1.0)` after
      `proc.kill()` -- SIGKILL alone doesn't reap the process. Added
      `test_stop_reaps_the_process_after_a_sigkill`.
- [x] L4 -- `_read_line` re-arms its full timeout on every recv rather
      than honoring an absolute deadline, so a peer trickling bytes with
      no newline can hold `_io_lock` indefinitely, blocking every
      transport key (`services/player.py`). **Fixed** 2026-09-21:
      `_read_line` now takes an absolute deadline and shrinks the
      per-recv timeout against it, raising `TimeoutError` (caught
      specifically in `_command` for the existing message) rather than
      letting each trickled byte reset a fixed timeout window. Added
      `test_command_honors_an_absolute_deadline_against_a_trickling_peer`
      (verified it reproduces indefinite blocking against the pre-fix
      code).
- [x] L5 -- No fsync before either atomic rename (settings/progress/cache
      writes, and the finished-download rename) (`config.py` /
      `services/download.py`). **Fixed** 2026-09-21: `atomic_write_text`
      now `flush()`s and `os.fsync()`s before its `os.replace`, and
      `download_book` does the same for the audio file before its rename
      -- previously power loss shortly after a write could land the
      rename durable but the data behind it not, contradicting
      `atomic_write_text`'s own crash-safety docstring. Added
      `test_atomic_write_text_fsyncs_before_the_rename` and
      `test_download_book_fsyncs_the_audio_file_before_renaming_it`
      (verified both catch the regression when the fsync is removed).
- [x] L6 -- `delete_download` has a TOCTOU between its `exists()` check
      and `unlink()` (`services/download.py` / `screens/library.py`).
      **Fixed** 2026-09-21: `delete_download` now uses
      `unlink(missing_ok=True)` instead of a separate `exists()` check.
      The bulk-delete confirm callback in
      `action_delete_finished_downloads` also now catches `OSError` per
      book so one failure (a permissions error, or the same race) doesn't
      abort the rest of the batch, reporting how many failed in the
      status message. Added 3 tests.
- [x] L7 -- `_last_position_ms` is seeded from `book.progress_ms` even
      when playback is about to start at 0 (a finished-book restart)
      (`screens/player_screen.py`). **Already fixed as a side effect of
      H1** -- `_last_position_ms` is now seeded from
      `self._start_position_ms` (the same value `on_mount` computes for
      `start_seconds`), not `book.progress_ms`. Added
      `test_hard_quit_before_the_first_poll_flushes_the_real_start_position`
      to cover it explicitly: `action_close`'s own live position read
      already masks this for a graceful `q`/escape, so the regression
      test drives the hard-quit backstop path (`on_unmount` without
      `action_close`) instead, where it's still live. Verified the test
      fails against the pre-H1 seeding.
- [x] L8 -- `_flush_progress` marks progress as saved even when the save
      callback raised, suppressing the next periodic retry at the same
      position (`screens/player_screen.py`). **Fixed** 2026-09-21: the
      "saved" bookkeeping now only runs on the success path (an
      `except`-block `return` before it). Added
      `test_periodic_checkpoint_retries_after_a_failure_at_the_same_position`
      (verified it fails against the pre-fix unconditional bookkeeping).
- [x] L9 -- The sleep timer and checkpoint counter assume exactly one
      tick per second, but a poll that takes longer than a second is
      skipped entirely rather than counted, so both drift long
      (`screens/player_screen.py`). **Fixed** 2026-09-21: `_tick` now
      measures real elapsed time via `time.monotonic()` and uses that
      delta for both the checkpoint accumulator (renamed
      `_seconds_since_checkpoint`, `_CHECKPOINT_EVERY_TICKS` ->
      `_CHECKPOINT_EVERY_SECONDS`) and the sleep-timer countdown, instead
      of a fixed `-1`/`+1` per call. Added a `fake_clock` fixture and two
      new tests demonstrating the fix (`test_checkpoint_fires_from_real_elapsed_time_not_a_tick_count`,
      `test_sleep_timer_counts_down_by_real_elapsed_time_not_a_fixed_decrement`
      -- both verified to fail against the pre-fix per-call decrement);
      adjusted 4 existing tests that drove `_tick` in a tight loop
      assuming 1 call == 1 second to advance the fake clock explicitly.
- [x] L10 -- The chapter row goes stale when the current-chapter lookup
      returns `None` (seeking before the first chapter's start)
      (`screens/player_screen.py`). **Fixed** 2026-09-21: added an
      `else: chapter_row.update("")` branch alongside the existing
      no-chapters case. Added
      `test_chapter_row_clears_when_position_is_before_the_first_chapter`
      (verified it fails against the pre-fix missing branch).
- [x] L11 -- content-length is compared against decoded byte count, so
      a CDN that ever compresses would make every download fail as
      "truncated" (`services/download.py`). **Fixed** 2026-09-21: the
      truncation check now compares against `resp.num_bytes_downloaded`
      (the raw, possibly-still-compressed wire byte count, tracked from
      `iter_raw` underneath `iter_bytes`) instead of the local decoded-
      byte counter. Added
      `test_download_book_does_not_flag_a_compressed_transfer_as_truncated`
      (verified it fails against the pre-fix decoded-byte comparison).
- [x] L12 -- An auth file left at 0644 by a pre-hardening install is
      never tightened on `load()`, only re-chmod'd on the next fresh
      login (`services/auth.py`). **Fixed** 2026-09-21: `load()` now also
      `chmod`s the file to 0600 before reading it (defense in depth only
      -- `CONFIG_DIR` is already 0700 -- but cheap). Added
      `test_load_tightens_an_auth_file_left_at_0644` (verified it fails
      against the pre-fix `load()`).
- [x] L13 -- CAPTCHA/OTP verification-page text is logged at INFO
      unconditionally, and Amazon's page text typically includes a
      masked destination (partial email/phone) -- mild PII in a
      persistent log file (`services/auth.py`). **Fixed** 2026-09-21:
      demoted all of `_log_cvf_page`'s logging (the page text and its
      per-field metadata) to DEBUG, so it's only captured when the user
      opts into `VOXCODEX_DEBUG`. Updated the existing field-length test
      to capture at DEBUG, and added
      `test_log_cvf_page_logs_at_debug_not_info` (verified it fails
      against the pre-fix INFO level).
- [x] L14 -- The login-diagnostics monkeypatch resolves five private
      `audible.login` names outside its own `try`, while already holding
      a module lock -- a future rename would raise `AttributeError` and
      leave the lock permanently held (`services/auth.py`). **Fixed**
      2026-09-21: name resolution and wrapper installation now happen
      inside a `try`/`except Exception` that releases the lock and
      degrades to no diagnostics (logging a warning) instead of failing
      closed. Added
      `test_diagnostics_degrades_instead_of_failing_closed_on_a_missing_name`
      (verified it fails against the pre-fix unguarded `getattr`).
- [x] L15 -- ASINs are interpolated into request paths with no
      percent-encoding -- a value containing `?` or `#` would inject a
      query/fragment (`services/api.py`). **Fixed** 2026-09-21: moved the
      `InvalidAsin`/`require_valid_asin` shape validator (from the L1 fix
      in `download.py`) into `api.py` -- ASINs' real entry point, and
      where the other request-path builders live too -- and applied it in
      `get_license`, `push_last_position`, `get_chapters`, and (alongside
      the existing presence/dedupe checks) `get_library`'s item loop.
      `download.py` now imports and re-exports the shared validator
      instead of duplicating it. Added 5 tests (verified
      `test_get_license_rejects_a_malformed_asin` fails against the
      pre-fix unguarded interpolation).
- [x] L16 -- `_book_from_item`'s type coercion is inconsistent: `title`
      defaults via `.get(..., "Untitled")` (doesn't catch an explicit
      `null`), and `runtime_min`/`percent_complete` are used
      arithmetically with no numeric coercion (`services/api.py`).
      **Fixed** 2026-09-21: `title` now falls back via `or "Untitled"`
      (matching the `subtitle`/`purchase_date` pattern two lines away),
      and `runtime_min`/`percent_complete` are coerced with
      `int()`/`float()`, matching the equivalent chapter-parsing code.
      Added 2 tests (verified both fail against the pre-fix code).
- [x] L17 -- Modal message text is rendered as Rich markup, and at least
      one call site interpolates a publisher-supplied book title into it
      -- a title containing `[/...]`-shaped text raises `MarkupError`
      inside the confirm dialog (`screens/modals.py`). **Fixed**
      2026-09-21: both `PromptModal` and `ConfirmModal` now render their
      message `Static` with `markup=False` (the bold-markup title line
      above it is untouched -- never interpolated with untrusted data).
      Added 2 tests using an unmatched-closing-tag-shaped message
      (verified both fail against the pre-fix markup-enabled rendering).
- [x] L18 -- Neither modal is dismissible by keyboard -- the only way out
      of a CAPTCHA/OTP prompt or a delete-confirmation is to tab to a
      button (`screens/modals.py`). **Fixed** 2026-09-21: added an escape
      binding to both -- `PromptModal` maps it to Cancel, `ConfirmModal`
      maps it to No (the safe, non-destructive default). Added 2 tests
      (verified both fail without the binding).
- [x] L19 -- `chapter_cache.save()` iterates a dict a different worker
      thread can concurrently mutate, which can raise `RuntimeError:
      dictionary changed size during iteration` -- not caught by
      `save()`'s own `except OSError`, silently losing that session's
      chapter cache write (`services/chapter_cache.py`). **Fixed**
      2026-09-21: snapshot with `dict(chapters_by_asin)` before iterating
      (a single atomic C-level copy). Added
      `test_save_tolerates_the_dict_being_mutated_concurrently` -- a real
      background thread mutating a 2000-entry dict while `save()` runs
      repeatedly, calibrated to reliably reproduce the `RuntimeError`
      within ~50 attempts against the pre-fix code (verified 3/3 runs)
      and to complete in well under a second either way.
- [x] L20 -- Switching the active download mid-flight can hide the new
      download's progress bar and post a stale "Downloaded: ..." status
      for the cancelled one while the new one runs invisibly
      (`screens/library.py`). **Fixed** 2026-09-21: added
      `self._active_download_asin`, set when a download starts; the three
      completion callbacks (`_update_download_bar`, `_download_failed`,
      `_download_succeeded`) now no-op on the shared progress bar/status
      unless they match it (per-book state like `is_downloaded` and the
      filtered table still update regardless -- only the singular shared
      UI elements are guarded). Added
      `test_download_completing_after_being_superseded_does_not_clobber_the_new_one`
      (verified it fails against the pre-fix unconditional callbacks).
- [x] L21 -- A library refresh completing mid-download orphans the
      in-flight `Book` object -- the download's completion handler
      mutates a `Book` no longer present in `self._books`, so the
      finished download doesn't show as downloaded until the next manual
      refresh (`screens/library.py`). **Fixed** 2026-09-21: added
      `_book_by_asin` (extracted from `_selected_book`'s existing
      lookup), and `_download_succeeded` now re-resolves the current
      `Book` object by ASIN before mutating `is_downloaded`, falling back
      to the closure-captured one only if the title is genuinely gone
      from the library. Added
      `test_download_completing_after_a_refresh_still_marks_the_current_book`
      (verified it fails against the pre-fix direct mutation).
- [x] L22 -- A successful-but-empty library fetch overwrites a good
      offline cache with nothing (`screens/library.py`). **Fixed**
      2026-09-21: guarded with `if books: library_cache.save(books)`.
      Added `test_successful_but_empty_fetch_does_not_overwrite_the_offline_cache`
      (verified it fails against the pre-fix unconditional save).
- [x] L23 -- Two DOM queries reached from the background library-load
      worker (`_populate`/`_populate_offline` via `call_from_thread`)
      aren't guarded against `NoMatches` the way `_refresh_table` already
      is -- `_apply_filters_and_sort`'s own `#search` query and
      `_update_sort_filter_label`'s `#sort-filter` query
      (`screens/library.py`). Harmless today under `exit_on_error=False`,
      but inconsistent with the pattern established everywhere else in the
      file. **Fixed** 2026-09-21: wrapped both in `try/except NoMatches:
      return`, matching `_refresh_table`'s existing style. Added
      `test_apply_filters_and_sort_does_not_raise_if_search_box_is_gone`
      and `test_update_sort_filter_label_does_not_raise_if_label_is_gone`
      (verified both fail against the pre-fix unguarded queries).
- [x] L24 -- Minor display inconsistencies in `Book` (`models.py`): unknown
      runtime rendered `"0m"` while unknown remaining-time rendered blank
      for the same "not known" state, and `time_left_display` could report
      `"done"` for a book with well under a minute genuinely remaining
      (never started) because it rounded to 0 minutes before checking for
      "finished". **Fixed** 2026-09-21: `runtime_display` now returns `""`
      when `runtime_min == 0`, matching `progress_pct`/`time_left_display`'s
      existing "duration unknown" convention; `time_left_display` now
      checks the unrounded `remaining_ms` for the true "finished" case and
      falls back to `"<1m left"` (rather than `"done"`) for a nonzero
      remainder that rounds to 0 minutes. Added
      `test_time_left_display_sub_minute_remainder_is_not_done`,
      `test_time_left_display_done_only_when_truly_zero_remaining`, and
      updated `test_runtime_display_zero` to
      `test_runtime_display_blank_when_unknown` (verified both new/changed
      assertions fail against the pre-fix code).
- [x] L25 -- Dead code: `Settings.reload()` has zero callers anywhere in
      the app (`services/settings.py`). **Fixed** 2026-09-21: deleted it.
      No user-visible effect; no test change needed.
- [x] L26 -- A cluster of dead code: `positions_from_annotations`/
      `fetch_remote_positions` had no production caller (only tests,
      `services/progress.py`); the `quality` parameter threaded through
      `download_book`/`get_license`/`get_chapters` was never overridden by
      any call site (`services/download.py`, `services/api.py`);
      `License.codec` was written to the voucher and never read back;
      `Book.cover_url` was parsed and stored but never rendered (a TUI
      can't display it); `Book.local_audio_path`/`local_voucher_path` were
      never set or read at all; three of the four `LIBRARY_RESPONSE_GROUPS`
      fields (`customer_rights`, `product_desc`, `product_extended_attrs`)
      were requested but never consumed, adding payload to every library
      fetch for nothing. **Fixed** 2026-09-21: removed all of the above.
      `quality` is now hardcoded to `"High"` at both call sites (the value
      every real caller already used) rather than deleted outright, since
      the live Audible API genuinely expects that field on the wire --
      only the app-level knob nothing ever varied is gone. Left `media` in
      `LIBRARY_RESPONSE_GROUPS` alone (unverifiable against the live API
      in this session, and not one of the three the review named as
      confirmed-unused). Updated/removed the tests that covered the
      deleted behavior; added `test_get_license_requests_high_quality` and
      `test_get_chapters_requests_high_quality` to cover the now-hardcoded
      value.
- [ ] 1 more Low finding -- see the doc for the full list and suggested
      order of work.
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
