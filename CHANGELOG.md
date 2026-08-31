# Changelog

## Unreleased

Critical fixes from an outside code review.

### Fixed
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
- **mpv is no longer orphaned.** Closing the player (or quitting) while mpv is
  still starting now stops it instead of leaving a headless process holding the
  audio device. A failed IPC connect kills the process it just spawned.
- **Truncated downloads are rejected.** A download that ends short of the
  server-stated size is discarded rather than renamed into place to fail later
  at playback; interrupted downloads no longer leave a `.part` file behind.

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
