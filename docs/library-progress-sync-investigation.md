# Library-page progress sync: investigation notes (2026-08-30, overnight)

**Status: partially resolved + implemented; one piece still open.** Picks up
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
- **Still open: the mid-book `percent_complete` / "time left" number.** No
  client-submittable call was found that moves it *to a correct value*.
  `stats/events` activity *does* perturb it (it recomputed on a ~30-min+ delay
  in testing) but synthetic `Listening` events drove it to **0 %**, not to the
  real position — the exact `Listening` payload the app sends still needs a
  capture. Until then VoxCodex does **not** send `Listening` events (they make
  it worse). For a *finished* book this doesn't matter — the "Finished" badge
  wins over the percent. It only shows for books left partway through.
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

## Open questions / next steps

1. **Capture the real `Listening` payload** (see above) — the one thing
   blocking full mid-book progress sync. This is what stands between here and
   the v1.0.0 bar (feature parity with the Android app on position + finished
   + progateted percent + royalty-side listening events).
2. ~~Switch the position push to `PUT /1.0/lastpositions/{asin}`~~ — **done in
   v0.3.0.** `push_last_heard` → `push_last_position`; Fiona sidecar,
   `content_version`, and the constructed `guid` are all gone.
3. **Re-poll `percent_complete` on the test titles over the next day** to see
   whether the backend eventually self-corrects the 0 % it's showing now
   (would tell us whether the pipeline is just slow vs. genuinely needs the
   right event shape).
4. **Decide product stance for going public:** "resume position + finished
   state sync both ways; the in-progress % on the library tile updates once you
   open the book on an official client" may be an acceptable v1 if the capture
   turns out hard. The finished-state sync (shipped) covers the most visible
   case.

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
