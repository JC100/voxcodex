# Changelog

## Unreleased

### Fixed
- **Resuming a "Finished" book now actually restarts at 0 instead of
  seeking to the end.** `PlayerScreen` was re-deriving its mpv start
  position from `book.is_finished` after `LibraryScreen._launch_player`
  had already cleared that flag (to un-finish the book on resume), so it
  always took the "resume from saved progress" branch -- for a finished
  book, that's ~100% through, so mpv hit EOF immediately and nothing
  played. The start position is now computed once by the caller and
  passed in explicitly.
- **A transient mpv read failure could silently erase the saved listening
  position, locally and on every other device.** A failed IPC read of
  mpv's playback position was indistinguishable from "genuinely at
  position 0", so a single dropped read could zero out and push a bogus
  position to Audible. Reads that fail now raise instead of silently
  defaulting, and are skipped rather than treated as real progress.
- **A duplicate ASIN in the library could silently truncate the table and
  then crash the app on the next keypress.** The library load now dedupes
  by ASIN (keeping the first occurrence), the same way it already handled
  a missing ASIN.
- **The library table's active filter and its downloaded-size label could
  go stale after a download, delete, unmark, or a playback session** --
  e.g. downloading a book while filtered to "Downloaded" left it invisible,
  and deleting a download left the size label reading the pre-deletion
  total. All five affected actions now recompute the filter instead of
  just re-rendering the previous (now stale) result.
- **"Unmark finished" could leave a book unreachable by any library
  filter.** Clearing the flag alone left a book still at ~100% progress
  matching only the "Finished" filter -- not "In progress", not "Not
  started" -- so the state was stuck and a second press was a no-op.
  Unmarking now also pulls the tracked position back under the threshold.
- **The Chapter column stopped advancing after a listening session.** It
  stayed at whatever chapter the book was on when the library was last
  loaded, even after playing well past it, until the next full refresh.
- **`get_license` could trust an unconfirmed remote position.** It parsed
  `last_position_heard` without checking its status the way the equivalent
  parser elsewhere already does, so a "DoesNotExist" record (a title never
  played anywhere) could in principle feed in a bogus position and reset a
  real resume point.
- **Remote-position sync could silently stop working for a large library.**
  It sent every ASIN as one unchunked query string, plausibly past a
  gateway's request-line limit on a several-hundred-title library -- the
  request is now chunked (100 ASINs at a time) and results merged.
- **A non-JSON 200 response (a captive portal, a proxy error page, an
  Amazon maintenance page) could crash playback or chapter fetching with
  no message shown.** Both now raise a typed error the existing failure
  handling already catches, so the user gets "Could not start
  playback..." instead of nothing.
- **A malformed entry in the local progress cache could crash the whole
  library load.** Reading or writing a position now tolerates a
  wrong-shape cache entry instead of raising.

### Security
- **The Amazon account password and vault password could leak into
  Textual's worker logging** (devtools console, or a crash traceback
  rendered with local variables) via the default worker description for
  the three login/unlock workers. They now carry an explicit,
  non-sensitive description instead.
- **Closed two code-execution primitives in how mpv is invoked.** The DRM
  key/iv from a license response were written unvalidated into mpv's
  config-file include, and the stream URL was passed as a bare positional
  argument -- both server-controlled values, and either could (under a
  hostile or compromised license response) get mpv to load and execute an
  attacker-controlled script. The key/iv are now rejected unless they're
  plain hex, and the stream URL is now passed after a `--` terminator.

## 0.5.0

Closes the mid-book progress sync investigation -- the last known gap
before a 1.0 release. Progress sync (resume position, finished state,
and now mid-book percent/time-left) is fully two-directional.

### Added
- **Mid-book progress now pushed to Audible's own library tile.** Closing
  the player reports the session's `StartListening`/`Listening` events
  (the same shape the real Android app sends, captured off a live
  account -- see `docs/library-progress-sync-investigation.md`), so
  `percent_complete`/"time left" on the official app/website can catch up
  to what VoxCodex actually played, not just resume position and the
  "Finished" badge as before. Best-effort like the other Audible pushes:
  a failure surfaces the existing "...Audible sync failed" status rather
  than blocking anything locally, and titles played before this existed
  (no `license_id` on the saved voucher) are silently skipped until
  re-downloaded or streamed once.
- **Resuming a book marked "Finished" un-finishes it immediately.**
  Previously the flag only cleared when you explicitly pressed `u`, so
  playing a finished book on this device while another device (or the
  website) still showed "Finished" until you closed the player -- now
  it clears the moment playback starts, done once per resume rather than
  on every checkpoint.

## 0.4.0

Closes out every remaining finding (Medium and Low) from the outside code
review, plus a Python 3.10 compatibility fix. The Critical/High entries
below were fixed ahead of this release but hadn't been given a version
number yet.

### Added
- **Chapter counts load instantly on repeat sessions.** Chapter lists are
  now cached to disk (they never change for a book), and the background
  fetch batches table rebuilds instead of redrawing once per title.
- **Search no longer lags on a large library.** Typing is debounced
  (150ms) instead of re-filtering and rebuilding the table on every
  keystroke.
- **A status line when Audible sync fails.** Progress/finished-state
  pushes now surface "...Audible sync failed" instead of failing
  silently with only a debug log to explain it.
- **A way to undo a mis-detected "Finished" flag.** The 98%-of-runtime
  heuristic can mis-fire on a book with long trailing credits -- press
  `u` on a finished book to unmark it (pushed to Audible too).
- **Download size column, total, and bulk cleanup.** The library table
  shows each download's size, the sort/filter line shows the running
  total downloaded, and `X` bulk-deletes every finished download after
  one confirmation.
- Resume position now also refreshes from the license response at play
  time, not just at library load -- catches a title played on another
  device since the last library refresh.
- Stale `.part` files left by a killed download are swept on startup.
- A hard safety cap on library pagination, and library pages no longer
  get silently truncated by a short-but-nonempty page.

### Fixed
- **Every local file is now private (0600/0700).** Config/data
  directories, the settings/progress/library caches, and downloaded
  vouchers (which hold the AES decryption key) were previously created
  with world-readable permissions; the DRM key and iv no longer appear
  on mpv's command line either (`/proc/<pid>/cmdline` is world-readable)
  -- both now go through private files instead.
- **Progress is resolved by recency, not by whichever number is
  larger.** Restarting a book from chapter 1 on another device used to
  get stuck showing the old, higher position forever; only an in-app
  close could ever lower it.
- Narrowed several overly-broad exception handlers (table selection,
  chapter fetch, opening a title to play) so a real bug surfaces instead
  of looking identical to an expected, harmless failure.
- An unrecognized playback quality value is now rejected instead of
  silently treated as "high"; a library item with no ASIN no longer
  crashes the table; the "Finished" filter and the actual
  finished-detection threshold now agree (both 98%); the theme is no
  longer re-written to disk on every launch.
- **Debug logging no longer leaks credentials.** `audible.client`/`audible.login`
  DEBUG logging (raw API response bodies -- licence vouchers, signed CDN URLs --
  and login-page contents) is now off unless `VOXCODEX_DEBUG` is set, which
  prints a warning about what it exposes. The log file is created `0600` and
  rotates at 1 MB. Login-page form `value`s are no longer written to the log.
- **Background workers no longer cancel each other or crash the app.** The
  library fetch, download, and playback-open workers each get their own worker
  group instead of sharing one -- pressing play no longer aborts an in-flight
  download, etc. Workers now check for cancellation and no longer tear the app
  down with a traceback if the screen is closed while they're still running.
- **mpv is no longer orphaned.** The player handle is now published before the
  (up to 8 s) blocking startup, so closing the player or quitting mid-start
  stops mpv instead of leaving a headless process holding the audio device;
  `stop()` during startup also breaks the IPC connect loop promptly. mpv now
  runs with `--idle=once` so it exits at end-of-file rather than idling
  forever, and a failed IPC connect kills the process it just spawned.
- **The mpv IPC socket is no longer world-reachable.** It moves from a
  guessable `/tmp/voxcodex-mpv-<id>.sock` (any local user could connect and,
  via mpv's `run` command, get code execution as you) into a per-session
  `mkdtemp` directory created `0700`, removed on stop.
- **Player IPC errors no longer crash the app.** Socket failures
  (`BrokenPipeError`, `ConnectionResetError`, timeouts) are now funnelled into
  `MpvError`, transport keypresses against a dead mpv are swallowed, and the
  connect retry loop no longer leaks a socket per failed attempt. The line
  reader no longer relies on `socket.makefile()` across a socket timeout.
- **Settings no longer silently revert each other.** The app, library screen
  and player screen shared one `settings.json` through separate in-memory
  copies, so (for example) changing the theme from the command palette would
  roll back a sort order you'd just cycled in the library. There is now one
  `Settings` passed down from the app, every write is a reload-modify-write of
  just its own key under a process-wide lock, and all of `settings.json`,
  `progress_cache.json` and `library_cache.json` are written atomically
  (temp file + rename) so a crash mid-write can't truncate them.
- **Listening position is saved even on a hard quit.** It used to persist
  only when you left the player with `q` / `esc`; closing the terminal or
  `ctrl+q` discarded the whole session, locally and remotely. The player now
  checkpoints position roughly every 15 s and always on unmount, with the
  close still the authoritative flush (and the only thing that pushes to
  Audible / marks a book finished).
- **Truncated downloads are rejected.** A download that ends short of the
  server-stated size is discarded rather than renamed into place to fail later
  at playback; interrupted downloads no longer leave a `.part` file behind.
- **A wedged mpv no longer freezes the UI.** The player polled mpv's position
  with four blocking IPC round trips *on the event loop* every second (up to a
  20 s freeze if mpv stalled); those reads now happen on a background worker
  and only the render touches the UI. IPC command timeout cut from 5 s to
  1.5 s, and closing the player is bounded to ~2 s instead of ~5 s. One lock
  serialises socket I/O now that reads and transport commands run on separate
  threads.
- **Login can't hang the process on exit.** Quitting (`ctrl+q`) while an
  OTP / CAPTCHA prompt was open left the login worker blocked forever on
  `event.wait()`, so the process never exited. The wait now polls a
  shutting-down flag and gives up.
- **Double-submitting the login form is ignored.** The sign-in / unlock /
  browser-login workers are now exclusive and guarded by the busy state, so a
  fast double-click can't start two logins (which corrupted the diagnostics
  monkeypatch permanently and leaked an HTTP client). A second successful
  login now also closes the previous API client.

### Changed
- **Python 3.10 is no longer supported.** `audible` (a core dependency)
  itself requires Python>=3.11 as of the version this app now depends
  on; `requires-python` was raised to match rather than stay pinned to
  an increasingly old `audible` release indefinitely.
- Added ruff and mypy (strict); CI now runs lint and typecheck jobs
  alongside the test matrix (now Python 3.11-3.13).
- **Dependency floors are now real.** `textual>=0.86` (the theme APIs used at
  startup landed there; the old `>=0.60` `AttributeError`d on launch),
  `audible>=0.11,<0.13`. A smoke test pins the private `audible` internals
  the app reaches into so a bad bump fails in CI.

### Known gap
- The library-page **percent / "time left"** number for a book you're
  partway through still doesn't update from a VoxCodex play -- unchanged
  from 0.3.0 below; see `TODO.md` and
  `docs/library-progress-sync-investigation.md`. A finished book
  displays correctly (the "Finished" badge wins).

## 0.3.0

Two-way sync with Audible's cross-device state.

### Added
- **Resume position now pushes back to Audible.** When you stop playback in
  VoxCodex, your position is written to Audible's cross-device sync
  (`PUT /1.0/lastpositions/{asin}`), so the official app and website pick up
  where you left off here. Best-effort — your local resume point never depends
  on it. (`docs/whispersync-research.md`)
- **Finished state syncs both ways.** Reach the end of a book in VoxCodex (≥98%
  of runtime) and it's marked finished on Audible too; a book marked finished
  elsewhere already showed as finished here on library load.
  (`docs/library-progress-sync-investigation.md`)

### Known gap
- The library-page **percent / "time left"** for a book you're *partway*
  through does not yet update from a VoxCodex play — that number is fed by a
  separate Audible system whose write format isn't pinned down. A *finished*
  book displays correctly (the "Finished" badge wins). Closing this, plus
  sending listening-interval events, is the remaining work before 1.0.

## 0.2.0

- Rename from `audible-tui` to VoxCodex; AGPL-3.0.
- Library sorting/filtering, offline library cache, chapter navigation,
  volume + sleep timer, persisted playback speed/volume.
- Read Audible's own last-listened position on library load.
