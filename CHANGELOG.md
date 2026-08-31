# Changelog

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
