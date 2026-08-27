# audible-tui

A terminal UI for browsing, downloading, and playing the audiobooks already in
your Audible library.

This app only reads your existing library. It never touches purchasing,
checkout, or payment endpoints -- buying/adding books stays in the official
Audible app or website.

## Requirements

- Python 3.10+
- [mpv](https://mpv.io/) on your `PATH` (used for playback; not required for
  browsing/downloading only)
- `ffmpeg` is not required at runtime by this app, but mpv typically links
  against libavformat internally to read/decrypt AAXC, so a normal mpv
  install already covers it.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/audible-tui
# or: .venv/bin/python -m audible_tui
```

On first run you'll be asked to sign in with your Amazon account (the same
login used by the official Audible app), and optionally set a local "vault
password" to encrypt the saved login on disk. If you skip it, the file is
still written with `chmod 600` (owner-read/write only).

## Keybindings

Library screen:

| Key | Action |
|---|---|
| `/` | Search (title / author / series) |
| `↓` or `Enter` (from search) | Jump to the list |
| `↑` (from the list's top row) | Jump back to search |
| `d` | Download selected book |
| `p` / `space` | Play selected book (streams if not downloaded) |
| `x` | Delete local download |
| `r` | Refresh library |
| `o` | Cycle sort (Recent → Title → Author → Series → Progress → Recent) |
| `f` | Cycle filter (All → Downloaded → In progress → Finished → Not started → All) |
| `t` | Cycle progress column (% → time left → both → %) |
| `q` | Quit |

The search box has focus by default, so `d`/`p`/`o`/`f`/etc. would just be
typed as search text until you leave it -- `↓` or `Enter` moves focus to the
list (also shown in the search placeholder), and `↑` from the top row goes
back the other way.

Sort and filter apply client-side to whatever's already loaded (including
the offline cache), so cycling them is instant and needs no network call.
The line above the table always shows the current sort/filter and how many
titles that leaves (e.g. `Sort: Title   Filter: Downloaded   (3/42 shown)`)
-- sort, filter, and progress-column choices all persist across sessions
the same way playback speed/volume do.

The Chapter column (`current/total`, e.g. `6/15`) fills in progressively in
the background after the table's already showing -- one API call per book
not already known this session, kept off the main load/offline-fallback
path entirely. Blank means either not fetched yet or the title genuinely
has no chapter data (podcasts, samples, some older titles); pressing play
reuses whatever this already found rather than fetching it again. A book
you haven't started yet shows `0/total`, not `1/total` -- you can't be "on"
a chapter you haven't actually started listening to.

The "Downloaded" column shows whether a title is saved locally for offline
play -- not whether you've ever played it.

Player screen:

| Key | Action |
|---|---|
| `space` | Play / pause |
| `←` / `→` | Seek -30s / +30s |
| `shift+←` / `shift+→` | Seek -60s / +60s |
| `↑` / `↓` | Speed up / down |
| `]` / `[` | Volume up / down |
| `s` | Cycle sleep timer (off → 15 → 30 → 45 → 60 min → off) |
| `n` / `p` | Next / previous chapter |
| `q` / `esc` | Stop and go back |

The chapter line shows the current chapter's own elapsed/total time
alongside its title and number, e.g. `Chapter 6/15: Some Title   (2:14 / 18:30)`.

The sleep timer only counts down while actually playing (pausing freezes
it); when it hits zero it pauses playback and resets to off.

Chapter navigation needs Audible's chapter metadata for that title, fetched
alongside the license/voucher whenever you hit play; a title with no
chapter data (podcasts, samples, some older titles) or a failed fetch just
means no chapter row/navigation for that session -- playback itself is
unaffected either way.

`ctrl+p` opens Textual's built-in command palette (labeled "Commands" in the
footer) -- among other things, a "Theme" command to pick from Textual's
built-in themes. That choice persists across sessions too, the same way the
rest of this section's settings do (Textual itself doesn't remember it
between runs on its own).

Playback speed and volume persist across sessions (`~/.config/audible-tui/settings.json`)
-- adjust them once with `↑`/`↓`/`]`/`[` and every future play starts there.
That file also tracks, but doesn't yet surface in the UI, which title you
most recently played *in this app* and which one Audible's own record shows
as most recently played *elsewhere* -- kept as two separate values rather
than merged into one "last played" for the same reason progress sync is
one-directional (see below): this app's plays never reach Audible's side,
so there's no way to compare them on equal footing yet.

## How it works

- Auth and all API calls go through the [`audible`](https://github.com/mkb79/Audible)
  package -- the same library `audible-cli` is built on.
- Downloads use Audible's content-licensing flow (`content/{asin}/licenserequest`)
  to get a CDN URL plus an AES key/iv, which is saved alongside the AAXC file
  as a small voucher JSON.
- Playback runs `mpv` as a subprocess, controlled over its JSON IPC socket,
  handing the AAXC key/iv straight to ffmpeg's demuxer (`-audible_key`/
  `-audible_iv`) so it can play/stream directly with no separate decrypt step.
- **Browsing and playing downloaded books works offline.** Every successful
  library fetch is cached (`~/.local/share/audible-tui/library_cache.json`);
  if a fresh fetch fails for any reason (no connection, an Audible outage),
  the library screen falls back to that cache instead of just showing an
  error, and says so ("Offline -- showing last known library, cached Xm/h/d
  ago"). Local download status and resume position are still read fresh off
  disk in that fallback too, so anything already downloaded is exactly as
  playable as when you're online -- only actions that inherently need a live
  connection (downloading something new, streaming something you haven't
  downloaded, fetching chapter metadata) are actually unavailable.
- **Progress sync is one-directional (Audible -> this app).** This app reads
  your real position from Audible when it can, and always keeps its own
  local record of where you left off (`~/.local/share/audible-tui/` by
  default) so resuming works reliably within the app -- it just can't push
  a play made here back to Audible's own cross-device sync. This was
  investigated in depth and deliberately shelved rather than left
  unexamined -- see [`docs/whispersync-research.md`](docs/whispersync-research.md)
  for what was tried, what actually works, and why it doesn't reach the
  Android app or website.
  (The *read* side of this had been silently broken since day one, returning
  nothing on every real account despite looking like it worked -- the actual
  response shape wasn't confirmed against a live account until this was
  revisited; fixed in `services/progress.py`.)

## Running tests

```bash
.venv/bin/pip install -e ".[test]"
.venv/bin/pytest
```

Unit tests cover the service layer (parsing, download, progress tracking,
mpv control) against fakes, and Textual-pilot integration tests drive every
screen and modal (`LibraryScreen`, `PlayerScreen`, `LoginScreen`, the modal
dialogs) through their real keybindings/clicks/focus routing -- not by
calling `action_*` methods directly. None of it needs a real Audible
account, network, or mpv process: `LoginScreen`'s tests fake the `auth`
module (`audible.Authenticator.from_login` etc.) rather than talking to
Amazon, so they cover the screen's own state machine -- field navigation,
validation, busy/error states, and the blocking-prompt relay used for
OTP/CVF/CAPTCHA -- not Amazon's actual login pages, CAPTCHAs, or
anti-automation checks. That real HTTP flow still needs manual testing (see
the `run` skill / tmux for driving the TUI) -- there's no faking a website
you don't control.

## Local data

- Config/auth/settings: platform config dir (e.g. `~/.config/audible-tui/`)
- Downloads, progress cache, and the offline library cache: platform data
  dir (e.g. `~/.local/share/audible-tui/`)
