# VoxCodex

A terminal UI for browsing, downloading, and playing the audiobooks already in
your Audible library.

This app only reads your existing library. It never touches purchasing,
checkout, or payment endpoints -- buying/adding books stays in the official
Audible app or website.

## Requirements

- Python 3.11+
- [mpv](https://mpv.io/) on your `PATH` (used for playback; not required for
  browsing/downloading only). `ffmpeg` itself isn't required at runtime --
  mpv typically links against libavformat internally to read/decrypt AAXC,
  so a normal mpv install already covers it.

  | Distro / OS | Install command |
  |---|---|
  | Debian / Ubuntu | `sudo apt install mpv` |
  | Fedora | `sudo dnf install mpv` |
  | Arch | `sudo pacman -S mpv` |
  | macOS (Homebrew) | `brew install mpv` |

## Installation

The recommended way to install is [pipx](https://pipx.pypa.io/), which keeps
this (and its dependencies) in its own isolated environment and puts a
`voxcodex` command on your `PATH` -- no messing with a venv yourself, and it
works cleanly on distros that block plain `pip install` outside one (Debian/
Ubuntu, Fedora, Arch all do this by default now).

| Distro / OS | Install pipx with |
|---|---|
| Debian / Ubuntu | `sudo apt install pipx` |
| Fedora | `sudo dnf install pipx` |
| Arch | `sudo pacman -S python-pipx` |
| macOS (Homebrew) | `brew install pipx` |
| Anything else | `python3 -m pip install --user pipx` |

Then:

```bash
pipx install git+https://github.com/JC100/voxcodex.git
voxcodex
```

**To update** to the latest commit later:

```bash
pipx upgrade voxcodex
```

To pin a specific released version instead of always tracking the latest
commit, install (or upgrade to) a tag: `pipx install git+https://github.com/JC100/voxcodex.git@v0.3.0`.
See [Releases](https://github.com/JC100/voxcodex/releases) for what's tagged.

On first run you'll be asked to sign in with your Amazon account (the same
login used by the official Audible app), and optionally set a local "vault
password" to encrypt the saved login on disk. If you skip it, the file is
still written with `chmod 600` (owner-read/write only).

## Development setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/voxcodex
# or: .venv/bin/python -m voxcodex
```

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

Playback speed and volume persist across sessions (`~/.config/voxcodex/settings.json`)
-- adjust them once with `↑`/`↓`/`]`/`[` and every future play starts there.
That file also tracks, but doesn't yet surface in the UI, which title you
most recently played *in this app* and which one Audible's own record shows
as most recently played *elsewhere* -- kept as two separate values rather
than merged into one "last played".

## How it works

- Auth and all API calls go through the [`audible`](https://github.com/mkb79/Audible)
  package -- the same library `audible-cli` is built on.
- Downloads use Audible's content-licensing flow (`content/{asin}/licenserequest`)
  to get a CDN URL plus an AES key/iv, which is saved alongside the AAXC file
  as a small voucher JSON.
- Playback runs `mpv` as a subprocess, controlled over its JSON IPC socket
  (kept in a private `0700` temp dir, since mpv's IPC can execute programs),
  handing the AAXC key/iv straight to ffmpeg's demuxer (`-audible_key`/
  `-audible_iv`) so it can play/stream directly with no separate decrypt step.
- **Browsing and playing downloaded books works offline.** Every successful
  library fetch is cached (`~/.local/share/voxcodex/library_cache.json`);
  if a fresh fetch fails for any reason (no connection, an Audible outage),
  the library screen falls back to that cache instead of just showing an
  error, and says so ("Offline -- showing last known library, cached Xm/h/d
  ago"). Local download status and resume position are still read fresh off
  disk in that fallback too, so anything already downloaded is exactly as
  playable as when you're online -- only actions that inherently need a live
  connection (downloading something new, streaming something you haven't
  downloaded, fetching chapter metadata) are actually unavailable.
- **Progress sync is mostly two-directional now, with one known gap.** This
  app reads your real position from Audible, always keeps its own local
  record of where you left off (`~/.local/share/voxcodex/` by default), and
  pushes back:
  - **Resume position** -- your position here propagates to Audible's
    cross-device sync, so the app/website resume where you stopped in
    VoxCodex. See [`docs/whispersync-research.md`](docs/whispersync-research.md).
  - **Finished state** -- reach the end of a book here and it's marked
    finished on Audible too (and vice versa on load).
  - **Known gap:** the library-page *percent / "time left"* number for a book
    you're partway through does not update from a VoxCodex play -- that field
    is fed by a separate Audible system whose exact write format isn't pinned
    down yet. A *finished* book shows correctly (the "Finished" badge wins).
    See [`docs/library-progress-sync-investigation.md`](docs/library-progress-sync-investigation.md).
  (The *read* side of position sync had been silently broken since day one,
  returning nothing on every real account despite looking like it worked --
  fixed in `services/progress.py`.)

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

- Config/auth/settings: platform config dir (e.g. `~/.config/voxcodex/`)
- Downloads, progress cache, and the offline library cache: platform data
  dir (e.g. `~/.local/share/voxcodex/`)

## License

[GNU AGPL-3.0](LICENSE) -- matching the license of the
[`audible`](https://github.com/mkb79/Audible) library this project is built on.
