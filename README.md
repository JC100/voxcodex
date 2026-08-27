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
| `d` | Download selected book |
| `p` | Play selected book (streams if not downloaded) |
| `x` | Delete local download |
| `r` | Refresh library |
| `q` | Quit |

Player screen:

| Key | Action |
|---|---|
| `space` | Play / pause |
| `←` / `→` | Seek -10s / +30s |
| `shift+←` / `shift+→` | Seek -60s / +60s |
| `↑` / `↓` | Speed up / down |
| `q` / `esc` | Stop and go back |

## How it works

- Auth and all API calls go through the [`audible`](https://github.com/mkb79/Audible)
  package -- the same library `audible-cli` is built on.
- Downloads use Audible's content-licensing flow (`content/{asin}/licenserequest`)
  to get a CDN URL plus an AES key/iv, which is saved alongside the AAXC file
  as a small voucher JSON.
- Playback runs `mpv` as a subprocess, controlled over its JSON IPC socket,
  handing the AAXC key/iv straight to ffmpeg's demuxer (`-audible_key`/
  `-audible_iv`) so it can play/stream directly with no separate decrypt step.
- **Progress sync is one-directional (Audible -> this app).** This app reads
  your real position from Audible when it can, and always keeps its own
  local record of where you left off (`~/.local/share/audible-tui/` by
  default) so resuming works reliably within the app -- it just can't push
  a play made here back to Audible's own cross-device sync. This was
  investigated in depth and deliberately shelved rather than left
  unexamined -- see [`docs/whispersync-research.md`](docs/whispersync-research.md)
  for what was tried, what actually works, and why it doesn't reach the
  Android app or website.

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

- Config/auth: platform config dir (e.g. `~/.config/audible-tui/`)
- Downloads and progress cache: platform data dir (e.g.
  `~/.local/share/audible-tui/`)
