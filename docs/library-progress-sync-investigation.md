# Library-page progress sync: investigation notes (2026-08-30, overnight)

**Status: in progress.** Picks up from `whispersync-research.md`. That work
made the *resume point* sync (open a book on another device, it starts where
VoxCodex left off). This round is about the other half the user reported still
broken: the **library page's "time left" / progress bar does not move** when
VoxCodex pushes a position.

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

1. **Legacy Fiona XML sidecar** — what VoxCodex ships today
   (`AudibleAPI.push_last_heard`).
2. **`PUT /1.0/lastpositions/{asin}`** — a clean JSON endpoint on the normal
   `api.audible.<domain>` host. Body: `{"acr": "...", "asin": "...", "position_ms": N}`.
   `acr` from a `licenserequest` `content_reference` (no `version`/`guid`/XML
   needed). Returns empty body. **Propagates to `annotations/lastpositions`
   identically to the Fiona write.** → *This should replace the Fiona XML hack —
   simpler, same host as everything else, fewer moving parts.*

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

### CONFIRMED: `MarkAsFinished` via `stats/events` works

Sending one event `{"event_type": "MarkAsFinished", ...common fields...}` for
Subtle Art:

- `GET /1.0/stats/status/finished?asin=...` → `is_marked_as_finished: true`,
  fresh `update_date`.
- `/1.0/library` `listening_status.is_finished` → **`true`** (was `false`).

So "user finished this book in VoxCodex → mark it finished on Audible" is a
solved, one-call operation. `MarkAsUnfinished` / `ManualMarkAsFinished` /
`ManualMarkAsUnfinished` are sibling enum values (reversibility test pending).

Note: after `MarkAsFinished`, `percent_complete` still read `2.0` (not snapped
to 100) within the observation window — the "Finished" badge is driven by
`is_finished`, not by percent, so the tile still displays correctly.

### Observed but not yet explained: `StartListening` knocked `percent_complete` 100 → 0

On Algo (started at `percent_complete: 100`), a batch of `StartListening` + 3
`Listening` events (spanning up to 9,720,000 ms ≈ 25%) resulted in
`percent_complete: 0.0` and `time_remaining_seconds: 38880` (full runtime),
`is_finished: false` — i.e. it did NOT land on 25% (the events' end position)
and did NOT stay at 100. A later lone `Listening` event with `lastposition`
pre-set to 50% also left `percent_complete` at 0.0 across ~95 s.

Working hypothesis: `percent_complete` / `time_remaining_seconds` are served
from a **batch-updated aggregate** (an analytics pipeline behind
`stats/events`), not recomputed synchronously — so the real signal may just not
have propagated yet in these short windows. `StartListening` may reset the
aggregate to "0, in progress" and `Listening` events accumulate into it on a
delay. Needs a longer-horizon re-check (10 min – several hours) and, ideally, a
capture of the real app's exact `Listening` cadence/payload.

## Open questions / next steps

1. **Does `percent_complete` catch up?** Re-poll Subtle Art (Listening events
   sent ~12:36Z) and Algo over hours. If it eventually reflects the events,
   this is a lag problem, not a payload problem.
2. **Exact `Listening` payload the real app sends** — a capture would remove the
   guesswork on `source` values, whether `event_start_time`/`event_end_time`
   are required, batching size, and how position maps in. Emulator rig is
   available (see `whispersync-research.md` / memory) but the no-emulator path
   is being tried first.
3. **`GET /1.0/stats/aggregates`** needs `store=Audible` (400s without it) —
   worth reading to see if per-title listened-minutes show up there and
   correlate with `percent_complete`.
4. **Reversibility**: confirm `MarkAsUnfinished` cleanly reverts
   `is_finished`/finished-status list.
5. If lag-based: VoxCodex should send `StartListening` at play start and
   `Listening` segments during/at end of a session, plus `MarkAsFinished` when
   the user completes a book. If it turns out `percent_complete` never catches
   up from these events, fall back to the emulator capture.

## Restoration ledger (so test books can be put back)

Pre-investigation observed state:
- `B01L790CUU`: `percent_complete` 2.0, `is_finished` false, lastpos 490,220.
- `B07DGFS4LM`: `percent_complete` 100.0, `is_finished` false, lastpos 10,158.

User's real-world truth: both books finished. Final restoration target:
`MarkAsFinished` both, lastpos left near where VoxCodex last had it.
