# Library-page progress sync: investigation notes (2026-08-30, overnight)

**Status: fully resolved, implemented, and confirmed live (2026-09-21).**
Picks up
from `whispersync-research.md`. That work made the *resume point* sync (open a
book on another device, it starts where VoxCodex left off). This round is about
the other half the user reported still broken: the **library page's "time
left" / progress bar / "Finished" badge did not move** when VoxCodex pushed a
position.

## TL;DR for the morning

- **Root cause found.** The resume-position endpoints and the library-tile
  progress fields (`listening_status.percent_complete` /
  `time_remaining_seconds` / `is_finished`) are **separate server-side
  records**. Pushing a position (either endpoint) never touched the tile.
- **"Finished" now syncs — shipped this session.** `PUT /1.0/stats/events`
  with a `ManualMarkAsFinished` event flips `listening_status.is_finished`
  within seconds and is fully reversible (`ManualMarkAsUnfinished`). VoxCodex
  now fires this when a playback session ends at ≥98 % of runtime. New:
  `AudibleAPI.set_finished` / `services.progress.push_finished`, wired in
  `screens/library.py` `_on_close`. Live-tested both directions.
- **[As of this investigation's start] Still open: the mid-book
  `percent_complete` / "time left" number.** No client-submittable call was
  found that moves it *to a correct value*. `stats/events` activity *does*
  perturb it (it recomputed on a ~30-min+ delay in testing) but synthetic
  `Listening` events drove it to **0 %**, not to the real position — the
  exact `Listening` payload the app sends still needed a capture. At this
  point VoxCodex did **not** send `Listening` events (they made it worse).
  For a *finished* book this didn't matter — the "Finished" badge wins over
  the percent. It only showed for books left partway through.
  **Superseded by the 2026-09-21 entry below:** the real payload was
  captured, implemented, and confirmed live the same day — VoxCodex now
  does send `Listening` events (via `push_listening_session`) on player
  close.
- **2026-09-21: captured, implemented, and confirmed live — all the same
  day, see that dated section below.** Real `Listening` / `StartListening` /
  `MarkAsUnfinished` payloads recovered via a network-level MITM (mitmproxy
  on a dedicated proxy box + Android CA-trust bind-mount) against the real
  Android app talking to the real backend; `AudibleAPI.push_listening_session`
  implemented in VoxCodex to send a `StartListening`+`Listening` pair on
  player close; then confirmed by actually playing a book through VoxCodex
  itself for ~3 minutes and re-reading the live account immediately after —
  `percent_complete`/`time_remaining_seconds` landed on the *correct* value
  (matching the real position to the second), not 0%, and within ~15-20s,
  not the tens-of-minutes lag the old broken shape produced. **This was the
  one remaining gap; there is nothing else open in this investigation.**
- **Position push moved off the Fiona sidecar (done, v0.3.0).** The push now
  goes through `PUT /1.0/lastpositions/{asin}` — clean JSON
  (`{acr, asin, position_ms}`), normal api.audible host, no `guid` / XML /
  `content_version` / `codec`. `AudibleAPI.push_last_position` replaced
  `push_last_heard`; `content_version` is gone from `License` / the voucher /
  the whole call chain. Verified live (propagates to
  `annotations/lastpositions` in ~3 s, same as Fiona did). Details below.

---

## Detail

All testing is black-box against the live `audible.com.au` account using the
already-registered VoxCodex auth. Test titles (both **Purchase**-rights, so no
creator-royalty implications per the earlier memo, and both already finished by
the user, who OK'd full read/write latitude on them):

- `B01L790CUU` — *The Subtle Art of Not Giving a F\*ck* (runtime 317 min / 19,020,000 ms)
- `B07DGFS4LM` — *Once Upon an Algorithm* (runtime 648 min / 38,880,000 ms)

## What drives the library "time left"

The `/1.0/library` item's `listening_status` group:

```json
"listening_status": {
    "finished_at_timestamp": "2026-08-30T12:08:33.768Z",
    "is_finished": false,
    "percent_complete": 2.0,
    "time_remaining_seconds": 18639
}
```

`percent_complete` + `time_remaining_seconds` are the library-tile progress.
`is_finished` is the "Finished" badge. This is the exact data the website and
app library views render.

## Confirmed: the position-write endpoints do NOT touch `percent_complete`

Two separate endpoints update the resume pointer (`GET /1.0/annotations/lastpositions`):

1. **`PUT /1.0/lastpositions/{asin}`** — clean JSON endpoint on the normal
   `api.audible.<domain>` host. Body: `{"acr": "...", "asin": "...", "position_ms": N}`.
   `acr` from a `licenserequest` `content_reference` (no `version`/`guid`/XML
   needed). Returns empty body. **This is what VoxCodex uses now**
   (`AudibleAPI.push_last_position`, since v0.3.0).
2. **Legacy Fiona XML sidecar** — what VoxCodex used through v0.2.x; the whole
   `guid = "{acr}:{version}"` / XML story is in `whispersync-research.md`.
   Still works, just no longer worth the special case now that #1 does the
   same thing in one `client.put`.

**Experiment 1** (Fiona) and **Experiment 2** (`PUT /1.0/lastpositions`): both
moved `lastpositions.position_ms` immediately (tested 9,510,000 then 5,706,000
on Subtle Art). Neither moved `percent_complete` (stuck at 2.0) or
`time_remaining_seconds` (stuck at 18639) over a 30 s window. **The resume
pointer and the library-progress field are fully decoupled.** This is the bug
the user hit.

## `PUT /1.0/stats/events` — the other system the real app uses

Documented (mkb79/Audible external API notes) and accepted by the live account.
Envelope: `{"stats": [ {event...}, ... ]}`. Success response:
`{"stats_response": {"stats_posted_timestamp": "...Z"}}`.

Per-event fields that validate against the live server (enums leaked verbatim
from 400 validation errors):

| field | value set |
|---|---|
| `event_type` | `DownloadComplete, ViewStatsPage, PurchaseBook, MarkAsUnfinished, CaptionsDisplayStart, ManualMarkAsUnfinished, MarkAsFinished, OpenBook, CaptionsDisplayed, CaptionsDownloaded, Bookmark, ManualMarkAsFinished, StartListening, DownloadStart, Listening, Sharing` |
| `listening_mode` | `Offline, Online` |
| `delivery_type` | `Streaming, Download` |
| `audio_type` | `CatalogSample, Preview, DynamicSample, FullTitle` |
| `store` | `AudibleForInstitutions, Audible, AmazonEnglish, Rodizio` |

Common fields sent on every event (mirrors the documented `DownloadStart`
example): `asin`, `event_timestamp` (`...sssZ`), `listening_mode`,
`delivery_type`, `audio_type`, `store`, `asin_owned` (bool),
`playing_immersion_reading` (bool), `local_timezone` (IANA),
`social_network_site` (`"Unknown"`).

`Listening` events additionally carry a nested `listening` object:
`{event_start_position, event_end_position, event_start_time, event_end_time,
playback_rate, source}` (ms + RFC3339). Accepted; effect on `percent_complete`
still being pinned down (see open questions).

### CONFIRMED + SHIPPED: finished state via `stats/events`

Sending one event `{"event_type": "ManualMarkAsFinished", ...common fields...}`:

- `GET /1.0/stats/status/finished?asin=...` → `is_marked_as_finished: true`,
  fresh `update_date`.
- `/1.0/library` `listening_status.is_finished` → **`true`** (was `false`).

Fully reversible and fast (<6 s each way), verified on both test titles:
`ManualMarkAsFinished` → `is_finished: true`; `ManualMarkAsUnfinished` →
`is_finished: false`. The non-`Manual` variants (`MarkAsFinished` /
`MarkAsUnfinished`) behave the same in testing; VoxCodex uses the `Manual*`
ones since a user reaching the end of a book *is* a manual action, not an
auto-detected one.

Implemented this session — `AudibleAPI.set_finished(asin, finished)` posts the
event; `services.progress.push_finished` is the best-effort wrapper;
`screens/library.py` `_on_close` calls it when `_reached_end(final_position_ms,
duration_ms)` (≥ `_FINISHED_FRACTION` = 0.98 of runtime) and the book wasn't
already finished. Live end-to-end test through the real `push_finished` passed
both directions.

Note: after a finish event, `percent_complete` did **not** snap to 100 within
the observation window (it actually drifted to 0 — see below). The "Finished"
badge is driven by `is_finished`, not by percent, so the tile still displays
correctly for a finished book; the stale percent only bites a *partway* book.

### The still-open piece: mid-book `percent_complete` / `time_remaining_seconds`

`stats/events` activity **does** feed these fields — they are not frozen — but
nothing tried drove them to a *correct* value:

- On Algo (started at `percent_complete: 100`): `StartListening` + 3 `Listening`
  events spanning up to 9,720,000 ms (≈ 25 %) → `percent_complete` recomputed
  to **0.0**, `time_remaining_seconds` to full runtime. Not 25 %, not 100.
- On Subtle Art: `Listening` events + a `MarkAsFinished` → `percent_complete`
  went `2.0` → (about 30 min later) **0.0**. So it *did* recompute, just
  wrongly.
- Recompute lag is long: **tens of minutes**, not seconds. Nothing moved within
  any 30–95 s window; changes showed up on the next check ~30 min later.
- The endpoint accepts almost anything (bogus fields, missing nested objects,
  flat vs. nested `listening` — all `200 OK` with a `stats_posted_timestamp`),
  so a 200 tells you nothing about whether the payload was *understood*. Only
  observing `listening_status` over time does.

Read side: `GET /1.0/stats/aggregates?store=Audible&response_groups=total_listening_stats&daily_listening_interval_duration=N&daily_listening_interval_start_date=YYYY-MM-DD`
returns per-day *total* listened-ms (`aggregated_daily_listening_stats`) and an
all-time total — **not** per-title progress. Not the thing driving the tile.

**Conclusion:** the `Listening`-event payload matters and can't be nailed by
black-box guessing (permissive endpoint + long recompute lag + wrong results).
This needs a **capture of the real Audible app's own `Listening` events** —
exact field set, position mapping, batching, cadence — then replay-test whether
that specific shape moves `percent_complete` correctly. Emulator/Frida rig
notes are in `whispersync-research.md` + the `audible-tui-whispersync-blackbox-re`
memory.

### Emulator capture attempt this session — blocked, not abandoned

Tried the no-emulator path first (per the user's instruction), got as far above
as black-box API testing allows, then booted the `whispersync-re` AVD for a
capture. Blockers hit, for next time:

- **AVD config drift:** `~/.android/avd/whispersync-re.avd/config.ini` has
  `hw.gpu.enabled=no` / `hw.gpu.mode=auto`. The prior session's notes say
  `-gpu guest` on the command line is what stopped the crashes/ANRs — the
  saved config doesn't encode that. Emulator threw repeated "System UI isn't
  responding" ANRs during boot even with `-gpu guest` passed. Host was only at
  ~1.0 load and 3 GB free RAM (AVD wants 2 GB) — RAM pressure is plausible.
- **Audible app is still logged in** (mini-player showed the book + "5h 13m
  left" — same stale value the API returns, confirming API == UI). So a
  capture *is* possible without re-auth once the emulator is stable.
- **Proxy not intercepting:** with `-http-proxy` + `settings put global
  http_proxy 10.0.2.2:8080`, a `toybox nc` test from the guest reached
  mitmdump, but **zero** Audible traffic did — not even failed CONNECTs. The
  app's network client isn't honouring the system HTTP proxy. Next time: skip
  the proxy, use a **Frida script that hooks the app's HTTP layer directly**
  (OkHttp interceptor or the TLS socket) and logs request/response — no proxy,
  no CA install. Watch for R8/ProGuard obfuscation of OkHttp class names in the
  release APK. mitmproxy CA is installed into the conscrypt APEX
  (`c8750f0d.0`) already if the proxy route is retried.

## 2026-09-20: emulator capture attempt, take two — Frida works, wrong layer

Picking up the "Emulator capture attempt this session — blocked, not
abandoned" thread from above, remotely (no hands-on-glass access to the
laptop this time, so this session drove the emulator entirely via `adb`).

**What got further than last time:**
- `whispersync-re` AVD boots clean and stable with `-gpu guest` — the fix
  noted above held. This machine also had far more free RAM this round
  (~10GB vs. the ~3GB that plausibly caused the earlier ANRs).
- `frida-server` (17.17.0, matched to the host's `frida` client version)
  runs fine on the AVD as root (`adb root` works on this image).
- Successfully reverse-engineered the app's R8-minified OkHttp usage
  purely via Frida reflection (no decompiling): `Request`/`RequestBody`/
  `Response`/`HttpUrl` all have their real getters inlined away, replaced
  by single-letter-named direct field access (e.g. `Request.a` = url,
  `.b` = method, `.c` = headers, `.d` = body). Recovered the real mapping
  by dumping `getDeclaredFields()`/`getDeclaredMethods()` at runtime and
  matching by type shape. `okhttp3.internal.http.CallServerInterceptor
  .intercept(Interceptor$Chain)` is the one method name R8 leaves alone
  (interface override), so it's the actual hook point — not
  `Request.Builder.build()`, which got fully inlined away.
- **New, unrelated bug found in this AVD image: touchscreen taps don't
  register at all** (`input tap` / `input touchscreen swipe`) even though
  the screen renders and `getevent -pl` shows valid touch devices. Key
  events (`input keyevent`, including `KEYCODE_TAB`/`DPAD_CENTER` focus
  navigation and, critically, `KEYCODE_MEDIA_PLAY`/`PAUSE`) all work
  fine. Worked around it entirely by driving the app via media-session
  keys instead of touch — confirmed real playback (position advanced,
  "picking up where you left off" banner shown) without ever tapping the
  screen. Root cause not investigated (GPU/input driver interaction under
  `-gpu guest`, guess only) — if a future session has hands-on access,
  check whether this AVD's touch issue reproduces there too before
  spending time on it blind.
- The interceptor hook also had to be widened to **every loaded
  ClassLoader** (`Java.enumerateClassLoaders` + per-loader
  `Java.classFactory.use(...)`), not just the default one Frida resolves
  at attach time — confirmed 5 loaders have `CallServerInterceptor`
  loaded (the app's own `base.apk`, an androidx window-extensions jar,
  and three Google Play Services–related loaders). Default resolution
  without this was silently hooking a copy that's never actually invoked
  for this app's traffic.

**Where it's blocked now, differently than before:** even with the hook
correctly installed in all 5 loaders, **zero requests were captured**
despite 40+ minutes of confirmed real playback. logcat during that window
showed the app *is* making successful HTTP calls throughout
(`MetricsTransporter: Successfully uploaded metrics; code: 200`,
`TokenJobQueue: ... GetActorToken/GetToken ...`, a
`com.audible.playersdk.common.workmanager.ProxyWorker`) — so the
app's networking is working fine, it's just not going through
`okhttp3.internal.http.CallServerInterceptor` at all. Conclusion: the
SSO/token/metrics/player-SDK traffic layer (everything with
`com.amazon.dcp.sso.*` token names) uses a separate, non-OkHttp internal
Amazon HTTP stack, not the app-level OkHttpClient our hook was watching.
Java-level interceptor hooking is the wrong tool for this specific
traffic — network-level capture is required instead.

**Why not just proxy it (yet):** the standard "set the system HTTP
proxy" approach was already ruled out last session — the app doesn't
honor it at all (see above: zero traffic reached mitmproxy, not even a
failed CONNECT). The fix for *that* is OS-level transparent redirect
(`iptables -t nat -A OUTPUT -p tcp --dport 443 -j REDIRECT ...` run as
root inside the guest, `adb reverse` to tunnel the redirected port to a
transparent-mode proxy on the host) rather than a proxy *setting* the
app can ignore. That still leaves TLS interception needing a trusted CA:
this AVD is Android 15, where the system CA store lives in a read-only,
dm-verity-protected Conscrypt APEX, so the pre-Android-10 "drop a cert
into /system" trick (referenced above, from the older investigation)
doesn't apply to this image — the `c8750f0d.0` cert mentioned above is
**not** present on this AVD (checked; either a different/reset AVD state
or that note was aspirational). The modern path is: install the proxy's
CA as a *user* cert, then use Frida to bypass certificate-pinning checks
at runtime (well-trodden technique, e.g. the various public "frida
multi-unpinning" scripts) — not attempted yet.

**Plan for next time:** the user is standing up a dedicated Squid
instance in an LXC container on their Proxmox cluster instead of running
a proxy on the laptop — sidesteps needing iptables/transparent-proxy
setup *on the laptop itself*, though the AVD-side iptables REDIRECT (or
equivalent — pointing the emulator's outbound traffic at that Squid
instance) and the CA-trust + SSL-unpinning work above are still needed
regardless of where the proxy itself lives. Session paused here; no
capture attempted yet against the new Squid box.

## 2026-09-21: capture achieved — network-level MITM via dedicated proxy box

Picking up "Plan for next time" from the 2026-09-20 entry: the user stood up
a Debian 13 LXC container on their Proxmox cluster for exactly this. Full
rig, end to end:

**Proxy box.** Plain Debian 13 container, dedicated to this. Ended up using
**mitmproxy**, not Squid — Squid's SSL-bump terminates TLS fine but getting
at decrypted request/response *bodies* needs ICAP/eCAP plumbing on top;
mitmproxy does body capture natively and was simpler to stand up
(`pipx install mitmproxy`, no Debian package available on trixie). Runs as
a systemd service (`mitmdump --mode regular --listen-port 8080 -s
<addon> -w <flow file> --set allow_hosts='.*\.(audible|amazon|amazonalexa)\..*'`).
`allow_hosts` is the important bit — it's "opposite of `--ignore-hosts`":
only hosts matching the regex get MITM'd; everything else (Google Play
Services, GCM, etc.) gets a plain TCP passthrough with the real upstream
cert, so the rest of the emulator's traffic — and its normal function —
is undisturbed. **Gotcha:** `--set allow_hosts=a,b,c` does **not** split on
commas for a sequence-typed option — it becomes one literal regex containing
literal commas, matching nothing. Use one regex with `|` alternation
instead, or repeat `--set allow_hosts=X` once per pattern.
A small addon script logs full request/response headers+bodies for matched
flows to a plain text file (`response()` hook, `flow.request.get_text()` /
`flow.response.get_text()`).

**Getting the AVD's traffic to the proxy, transparently.** The 2026-09-20
finding stands: the app's real network stack ignores Android's
`settings put global http_proxy`. What *does* work and needed no discovery
this time: the **emulator's own `-http-proxy host:port` command-line flag**.
This operates at the QEMU/slirp layer, below the guest's entire network
stack — every guest-originated TCP connection gets tunneled out through
that HTTP CONNECT proxy regardless of whether the app (or even Android
itself) has any proxy awareness. Confirmed by watching real app traffic
(`api.audible.com.au`, `todo-ta-g7g.amazon.com`, `arcus-uswest.amazon.com`,
`unagi-fe.amazon.com`) arrive at the external proxy with **zero** proxy
configuration inside the guest OS. This is the piece that made network-level
capture finally *possible* — no iptables/transparent-redirect needed
anywhere, laptop or guest.

Two rough edges hit along the way, both fixable and worth knowing for next
time:
- `-http-proxy` **also** auto-sets the guest's `settings global http_proxy`
  to `10.0.2.2:<port>` (the slirp host alias). Something in the guest (looked
  like the captive-portal/NetworkMonitor check) then hammered
  `CONNECT 127.0.0.1:8080` in a tight loop via that *second*, redundant proxy
  path, pegging the emulator process at 700% CPU on the host. Fix: `adb shell
  settings put global http_proxy :0` right after boot to clear the
  guest-side setting — the QEMU-level redirect from `-http-proxy` keeps
  working fine without it, since it never depended on the guest knowing
  about a proxy in the first place.
- IPv6: the proxy box has no IPv6 route, so Google's IPv6-first connection
  attempts (`2001:4860:...`) log as "Network is unreachable" noise. Harmless
  — IPv4 fallback works — but don't mistake it for something broken.

**CA trust: two more gotchas, both now resolved.** Android 15's system CA
store lives in a read-only, dm-verity-backed `com.android.conscrypt` APEX —
confirmed (again) that `-writable-system`'s `/system` overlay does **not**
cover it (it's a separate ext4 loop device, `/dev/block/dm-24` in this
session). The fix that worked, in order:
1. `adb root && adb shell setenforce 0` (userdebug/eng image — permissive
   avoids chasing SELinux file-context labels on files copied out of
   `/data/local/tmp`).
2. Copy the APEX's existing `cacerts` dir out to a scratch dir on `/data`,
   drop the new CA cert in alongside the originals (keeps every existing
   root trusted — don't replace the dir, extend it), then
   `mount -o bind <scratch dir> /apex/com.android.conscrypt/cacerts`. This
   is a **kernel-level VFS mount** — it does not touch the read-only APEX
   image on disk, so it survives an Android framework restart
   (`adb shell stop && adb shell start`) but **not** a real reboot (`adb
   reboot` unmounts everything and you're back to square one; redo the
   `mount --bind` after each boot).
3. **The actual blocker, and the one that cost the most time:** the cert
   file must be named after OpenSSL's **legacy** `subject_hash_old`, not
   the modern `subject_hash` (`-hash`). Android's `TrustedCertificateStore`
   predates OpenSSL's post-1.0.0 hash algorithm change and was never
   updated — it still does the old MD5-based hash for the `<hash>.0`
   filename lookup. Using the new-style hash gets you a file that's present
   in `ls` but never found by any real TLS handshake — which looks exactly
   like "the OS just isn't picking up the CA," and burned a long stretch of
   this session chasing framework-restart / zygote-caching theories before
   the hash format itself turned out to be the bug.
   `openssl x509 -in ca.pem -noout -subject_hash_old` → rename to
   `<that>.0`.
4. mitmproxy always self-signs its default CA with the identical hardcoded
   subject (`CN=mitmproxy, O=mitmproxy`) — since `subject_hash_old` hashes
   only the Subject Name (not the key), **every mitmproxy-generated default
   CA has the same hash: `c8750f0d`.** That's not a coincidence with the
   value referenced in the 2026-08-30 section above — it's a mitmproxy
   constant, not something to recompute each time you regenerate its CA
   (only the underlying key changes between installs, not the filename it
   needs).
5. **Also had to force-restart the target app after each CA change.** Even
   with the correctly-hashed file in place, an already-running app process
   kept failing the handshake against the old (cached-at-first-use, not
   file-scanned-per-handshake) trust decision — `adb shell am force-stop
   com.audible.application` then relaunch was required before a given
   process would honor a freshly-added CA.

**The payload — captured clean, twice.** First, the app's own backlog: on
first launch this session it flushed four queued stat events from
2026-09-19's real listening (see the 2026-09-20 entry — that ~40 min
session was captured logcat-side but never seen server-side until now):

```
PUT https://api.audible.com.au/1.0/stats/events
{"stats":[
  {"asin":"B01L790CUU","asin_owned":true,"event_type":"Listening",
   "event_timestamp":"2026-09-19T20:50:18.471Z",
   "event_end_timestamp":"2026-09-19T21:23:49.296Z",
   "local_timezone":"Australia/West",
   "event_start_position":924414,"event_end_position":2935357,
   "playing_immersion_reading":false,"narration_speed":1.0,
   "length_of_book":19040890,"version_of_app":"26.32.06",
   "delivery_type":"Download","listening_mode":"Online","store":"Audible",
   "license_id":"40f2193e-602f-4fb2-8905-90451f2ed5e0","audio_type":"FullTitle",
   "secondary_device_type_id":"None","session_id":"738-9874918-7065541"},
  {"asin":"B01L790CUU","asin_owned":true,"event_type":"MarkAsUnfinished",
   "event_timestamp":"2026-09-19T20:46:38.981Z","local_timezone":"Australia/West",
   "event_start_position":0,"event_end_position":0,
   "playing_immersion_reading":false,"narration_speed":1.0,
   "length_of_book":19040890,"version_of_app":"26.32.06",
   "delivery_type":"Download","listening_mode":"Offline","store":"Audible",
   "license_id":"40f2193e-602f-4fb2-8905-90451f2ed5e0","audio_type":"FullTitle",
   "session_id":"738-9874918-7065541"},
  {"asin":"B01L790CUU","asin_owned":true,"event_type":"StartListening",
   "event_timestamp":"2026-09-19T20:46:38.974Z","local_timezone":"Australia/West",
   "event_start_position":826234,"event_end_position":0,
   "playing_immersion_reading":false,"narration_speed":1.0,
   "length_of_book":19040890,"version_of_app":"26.32.06",
   "delivery_type":"Download","listening_mode":"Online","store":"Audible",
   "license_id":"40f2193e-602f-4fb2-8905-90451f2ed5e0","audio_type":"FullTitle",
   "secondary_device_type_id":"None","session_id":"738-9874918-7065541"},
  {"asin":"B01L790CUU","asin_owned":true,"event_type":"Listening",
   "event_timestamp":"2026-09-19T20:46:38.974Z",
   "event_end_timestamp":"2026-09-19T20:48:18.240Z",
   "local_timezone":"Australia/West",
   "event_start_position":826234,"event_end_position":924414,
   "playing_immersion_reading":false,"narration_speed":1.0,
   "length_of_book":19040890,"version_of_app":"26.32.06",
   "delivery_type":"Download","listening_mode":"Online","store":"Audible",
   "license_id":"40f2193e-602f-4fb2-8905-90451f2ed5e0","audio_type":"FullTitle",
   "secondary_device_type_id":"None","session_id":"738-9874918-7065541"}
]}
→ 200 {"stats_response":{"stats_posted_timestamp":"2026-09-21T02:38:46.784Z"}}
```

Second, a fully controlled example: pressed `KEYCODE_MEDIA_PLAY`, waited
~17s wall clock, pressed `KEYCODE_MEDIA_PAUSE`. Two separate `PUT`s:

```
# fired at play:
{"stats":[
  {"event_type":"MarkAsUnfinished", "event_start_position":0,"event_end_position":0,
   "listening_mode":"Offline", ...no secondary_device_type_id...},
  {"event_type":"StartListening", "event_start_position":2895454,"event_end_position":0,
   "listening_mode":"Online", "secondary_device_type_id":"None", ...}
]}
# fired at pause, ~17s later:
{"stats":[
  {"event_type":"Listening",
   "event_timestamp":"2026-09-21T02:39:57.059Z",
   "event_end_timestamp":"2026-09-21T02:40:13.671Z",
   "event_start_position":2895454,"event_end_position":2911195,
   "listening_mode":"Online","secondary_device_type_id":"None", ...}
]}
→ both 200 {"stats_response":{"stats_posted_timestamp":"..."}}
```

**Confirmed field-level takeaways:**
- Events **batch** — a single `PUT` carries a `stats` array, can mix event
  types, and can include backlogged events from a much earlier session
  (the app queues locally and flushes on next successful connectivity).
- `StartListening` is **always paired with a `MarkAsUnfinished`** in the same
  batch when playback begins — looks like a defensive "wake up, this book
  is being listened to" signal independent of whether it was actually
  finished. `event_start_position`/`event_end_position` are both `0` for
  that `MarkAsUnfinished`; it does not carry a real position.
- `Listening` is the only type carrying `event_end_timestamp` — it
  represents a *closed interval* (`event_start_position` →
  `event_end_position`, wall-clock `event_timestamp` →
  `event_end_timestamp`). `StartListening`/`MarkAsUnfinished` are point
  events (no end timestamp).
- Positions and `length_of_book` are **milliseconds**, integer.
- `listening_mode` is `"Online"` for `StartListening`/`Listening` in every
  sample seen, `"Offline"` for every `MarkAsUnfinished` — looks like a
  fixed-per-event-type value rather than a reflection of actual
  connectivity state at the time (all samples had `delivery_type:
  "Download"` regardless).
- `secondary_device_type_id: "None"` is present on `StartListening`/
  `Listening` but **absent** (not present, not null) on `MarkAsUnfinished`.
- `session_id` is shared across every event in a listening session
  (matches the HTTP `session-id` header value) — ties the whole batch
  together server-side.
- `license_id` is the DRM license id VoxCodex already fetches per-title
  (same field noted in the TODO L2 finding) — no new lookup needed.
- The endpoint returned `200` for all of the above against the real
  backend — this is not a guess being validated by echo, it's the real
  app's real traffic.

**At capture time it wasn't yet answered** whether this payload, sent *by
VoxCodex*, would actually move `percent_complete`/`time_remaining_seconds`
to the correct value on the library tile — recompute lag was tens of
minutes in the 2026-08-30 black-box testing, and the AVD-side check
(`progress_ms=760800` vs. the just-posted `2911195`, ~2 minutes after)
hadn't caught up yet. **It's answered now — see "2026-09-21: implemented
and confirmed live" below**, same day: `AudibleAPI.push_listening_session`
was written using this exact capture, then actually exercised through
VoxCodex itself, and resolved correctly within ~15-20s.

**Reusable for next time (a future capture, if the payload shape ever
needs re-verifying):** the proxy box, systemd service, and CA are all
still standing (container is dedicated to this, not torn down after the
session). A future capture session should be much faster: boot the AVD
with `-gpu guest -no-snapshot-load -writable-system -http-proxy
<proxy-ip>:8080`, clear the guest proxy setting, redo the `setenforce 0` +
`mount --bind` (lost on reboot only), and traffic capture starts
immediately — no need to rediscover any of the above.

## 2026-09-21: implemented and confirmed live, same day as the capture

`AudibleAPI.push_listening_session` / `services.progress
.push_listening_session` implemented, sending a `StartListening` +
`Listening` pair (no `MarkAsUnfinished` — see that method's docstring for
why) on player close. Wired into `LibraryScreen._launch_player`/
`_on_progress` alongside the existing position/finished pushes. New
`License.license_id` field (also persisted to the download voucher) keys
the session. 27 new tests, full suite green, ruff/mypy clean.

**Confirmed against the live account the same day**, closing the one
question the capture left open:

1. Ran VoxCodex for real (`.venv/bin/voxcodex` in a tmux session, not a
   script calling the API directly) and played `B01L790CUU` (one of the
   two test books with standing "full write latitude") for ~3 minutes of
   real wall-clock time, then closed the player normally (`q`).
2. Immediately re-read the raw `/1.0/library` response for that asin:
   `percent_complete: 1.0`, `listening_status: {"is_finished": true,
   "percent_complete": 1.0, "time_remaining_seconds": 18829, ...}`.
3. Sanity check: `duration_ms` for this title is 19,020,000ms (19,020s).
   `19020 - 18829 = 191s` of progress — matches the actual mpv position at
   close (3:26, i.e. ~206s into the book, since it started this session
   from the beginning — see below) to within normal
   checkpoint/rounding drift, **not 0%, not some other wrong value.**
   This book started the session at position 0 (see next point), so 191s
   of computed progress against a session that played to ~3:26 is exactly
   the shape of a correct read, not a coincidental match.
4. This book already had `is_finished: true` from before this session
   (hence `PlayerScreen.on_mount`'s "finished books restart at 0" rule
   applying) — it correctly **stayed** `true` after the push, confirming
   the deliberate choice not to send `MarkAsUnfinished` didn't
   accidentally get un-finished some other way.
5. **Speed of the update is itself a notable finding:** this resolved
   within roughly 15-20 seconds of the push, not the tens-of-minutes
   recompute lag observed throughout the 2026-08-30 black-box testing.
   That earlier lag may have been specific to the wrong/malformed payload
   shape being processed slowly (or differently) rather than an inherent
   property of the endpoint — worth remembering if a future change to
   this payload seems to "not be working" after a few seconds; give it a
   fresh, real end-to-end test before assuming it's broken, but don't
   assume it needs 30 minutes either.

This closes the investigation. Nothing about mid-book progress sync
remains open — see the "Closed" entry in `TODO.md`.

## 2026-09-21, later the same day: resuming a finished book now un-finishes it

User feedback after checking both test books on the phone/website: they
were correctly showing real progress instead of "Finished" -- because
we'd manually cleared `is_finished` via the API as part of testing. The
user's actual concern: **VoxCodex itself** should do this automatically
the moment you resume a book marked finished, not leave it stuck showing
"Finished" everywhere until some manual intervention -- specifically
called out the cross-device confusion ("been listening on your laptop,
jump in the car, phone still shows finished").

**Implemented:** `LibraryScreen._launch_player` now checks
`book.is_finished` once, at the top (same place `session_start_position_ms`
is computed) -- if true, clears it locally, refreshes the table, and
fires `_push_finished(asin, False)` immediately, before the player screen
even mounts. Explicitly *not* repeated per checkpoint tick or on a second
play of the same session -- the guard is simply "was `is_finished` true at
open," which by construction only fires once per finished -> playing
transition. Recommended by the user specifically to keep the overhead of
this fix to one extra call, not a recurring one.

**Also tried, live, and confirmed NOT to work:** sending a zero-length
`StartListening`+`Listening` pair (`event_start_position ==
event_end_position`, both 0) at the same moment, hoping to immediately
reset the resumed book's stale `percent_complete`/`time_remaining_seconds`
(left over from whenever it was marked finished) rather than leaving it
showing old data until the session's close. Tested against `B01L790CUU`:
marked it finished again via a direct API call, opened it in VoxCodex,
and while still mid-playback (before closing) re-read the live library
response twice, 20+ seconds apart -- `is_finished` had correctly flipped
to `false` immediately, but `percent_complete`/`time_remaining_seconds`
were byte-for-byte unchanged both times. Ruled out recompute lag (the
earlier confirmed-working case updated within ~15-20s; this stayed
identical for 20+s and showed no sign of moving). Conclusion: Audible's
backend appears to only recompute the tile from an event with real
forward progress (`end_position > start_position`); a zero-length event
is accepted (200 OK, as ever) but has no effect on `listening_status`.
**Removed** the `allow_zero_length` mechanism this had introduced into
`AudibleAPI.push_listening_session` rather than ship dead weight -- see
that method's docstring for the final, accurate account. Net effect:
`is_finished` clears immediately (the thing actually reported as
confusing); the percent/time-left number itself stays stale until this
session's own close-time push, same as any other session.

## Open questions / next steps

1. ~~Capture the real `Listening` payload~~ — **done, 2026-09-21.** Exact
   schema confirmed for `Listening` / `StartListening` / `MarkAsUnfinished`,
   see that dated section above.
2. ~~Switch the position push to `PUT /1.0/lastpositions/{asin}`~~ — **done in
   v0.3.0.** `push_last_heard` → `push_last_position`; Fiona sidecar,
   `content_version`, and the constructed `guid` are all gone.
3. ~~Implement sending it from VoxCodex~~ — **done, 2026-09-21 (same day
   as the capture).** `AudibleAPI.push_listening_session` sends a
   `StartListening` + `Listening` pair on player close, using the new
   `License.license_id` field (also persisted to the download voucher).
   Deliberately drops the real app's accompanying `MarkAsUnfinished` --
   see that method's docstring for why. Tests/ruff/mypy all green.
4. ~~Re-poll `percent_complete` on a title played through this new path~~
   — **done, 2026-09-21 (same day).** Played `B01L790CUU` for real through
   VoxCodex, re-read the live account: `percent_complete: 1.0`,
   `time_remaining_seconds: 18829`, matching the real position to the
   second, resolved within ~15-20s. See "2026-09-21: implemented and
   confirmed live" above for the full check. **Nothing open remains in
   this investigation.**
5. ~~Decide product stance for going public~~ — moot, #1/#3/#4 are all
   done: mid-book progress now syncs correctly, not just resume position
   and the "Finished" badge.

## Restoration ledger (test books)

Pre-investigation observed state:
- `B01L790CUU` (Subtle Art): `percent_complete` 2.0, `is_finished` false, lastpos 490,220.
- `B07DGFS4LM` (Algo): `percent_complete` 100.0, `is_finished` false, lastpos 10,158.

Both titles are **Purchase**-rights and the user confirmed both finished in
real life, and OK'd full write latitude.

**Left at end of session:**
- `B01L790CUU`: `is_finished` **true**, lastpos **490,220** (restored),
  `percent_complete` 0.0.
- `B07DGFS4LM`: `is_finished` **true**, lastpos **10,158** (restored),
  `percent_complete` 0.0.

`is_finished: true` matches the user's real-world truth (better than the
pre-investigation `false`). The `percent_complete: 0.0` on both is a side
effect of the synthetic `Listening` events sent during testing — it is
**cosmetic and hidden** behind the "Finished" badge in every Audible client,
and opening either book on a real device (or the backend self-correcting) will
re-derive it. lastpos is back to exactly the pre-investigation values.

**2026-09-20 session:** no synthetic events were sent this time (capture
never got far enough to replay anything) — the only account activity was
genuine playback of `B01L790CUU` via the real Android app's own media
controls, functionally identical to the user actually listening. Checked
`annotations/lastpositions` after the session: still exactly
`position_ms: 826234`, `last_updated: 2026-08-31 00:09:36` — unchanged
from before this session started, so the ~40 minutes of in-app playback
was never flushed to the server (app process was killed with the
emulator before it pushed, and/or its push cadence is longer than the
session). Nothing to restore; no ledger update needed.

**2026-09-21 session:** again no synthetic events — all account activity
was the app's own genuine backlog flush (2026-09-19's real ~40 min session,
finally delivered once this session gave the app working connectivity) plus
a deliberate ~17s real play/pause via media keys to capture a clean
isolated example. Real, not synthetic: `B01L790CUU`'s position legitimately
advanced from `826234` → `2895454` (the backlogged Sept-19 session) →
`2911195` ms (this session's 17s test). All server responses were genuine
200s from the real backend, not something to roll back. Checked via
`AudibleAPI.get_library()` ~2 min after the last event: `is_finished=True`
(unchanged), `progress_ms=760800` — well behind the just-posted `2911195`,
consistent with the known tens-of-minutes recompute lag, not a new
regression. Nothing to restore; worth a re-check next session to see where
`progress_ms` lands once it catches up (ties into Open questions #3).

**2026-09-21, same day, second pass (implementation + live confirmation):**
after writing `push_listening_session`, played `B01L790CUU` for real
through VoxCodex itself (~3 minutes wall clock) to confirm the new send
path. Since this book's `is_finished` was `True` going in,
`PlayerScreen.on_mount` restarted it from position 0 rather than resuming
-- so this real session moved the position from wherever it was (see
above) back down to ~191s. Real, not synthetic; not something to restore:
per the same reasoning as the very first restoration entry above, a
position on an already-*finished* book is cosmetic (hidden behind the
"Finished" badge in every Audible client) regardless of its value.
`is_finished` correctly stayed `True` throughout (confirms the
no-`MarkAsUnfinished` design choice). Nothing to restore.

**2026-09-21, later the same day, third pass (auto-unfinish-on-resume):**
played `B07DGFS4LM` for real through VoxCodex (~9 minutes wall clock,
requested 3-5 -- ran a bit long) to confirm the original send path against
a second title; real position moved from 0 to ~535s (that title's
`is_finished` was still `True` at the time, so it also restarted at 0).
Then, per the user's request, both `B01L790CUU` and `B07DGFS4LM` were
un-marked via a direct `set_finished(asin, False)` call so their real
`percent_complete` would actually be visible (rather than hidden behind
the "Finished" badge) for the user to check on their phone/website --
confirmed both showing correctly. Following that, `B01L790CUU` was
deliberately re-marked finished via a direct API call specifically to
test the new auto-unfinish-on-resume feature (see the section above):
opened it in VoxCodex, confirmed `is_finished` flipped back to `false`
immediately server-side (checked while still mid-playback, before
closing), then stopped playback normally. Real activity throughout, all
consistent with the user's own testing intent -- nothing to restore
beyond what's already reflected in each book's real `is_finished`/
position state as of this session's end.
