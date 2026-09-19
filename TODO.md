# TODO

## Status

- Critical (C1-C4) and High (H1-H10) findings from the 2026-08-31 code
  review: done.
- Medium (M1-M10): done.
- Low (L1-L13): not started -- see below.

Full finding detail (rationale, suggested fix) lives in
`docs/code-review-2026-08-31.html`. Its line numbers are stale after the
M1-M10 rewrites -- relocate a finding by file/description, not by line.

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
- [ ] L9 -- "Finished" is defined two ways: the library filter uses
      `progress_pct >= 100`, `_reached_end` uses `_FINISHED_FRACTION`
      (98%). Use one constant in both.
- [ ] L10 -- `watch_theme` (`app.py`) writes to disk during `__init__`, so
      every launch does a settings write before the UI renders. Guard
      with a loading flag.
- [ ] L11 -- `import webbrowser` (`screens/login.py`,
      `_external_url_prompt`) is inside a function. Move to module scope.
- [ ] L12 -- `quality` (`services/api.py`, two call sites) silently
      coerces any non-"normal" value to "High". Validate against the two
      accepted values.
- [ ] L13 -- `downloads/` has no UI affordance for total size, bulk
      cleanup, or per-item size display.

## Keeping this file current

Whenever a task here is finished, check it off (or delete it) in the same
commit that does the work -- see "Definition of done" in `CLAUDE.md`. This
file should never need a separate "update the TODO" pass.
