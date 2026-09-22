# Cross-device listening-position sync: research notes (2026-08-26, updated 2026-08-31)

## Status: implemented. Push-to-Audible sync is live (`AudibleAPI.push_last_position` / `services.progress.push_position`).

**2026-08-31 update: the write now goes through `PUT /1.0/lastpositions/{asin}`**
(clean JSON, on the normal `api.audible.<domain>` host), not the Fiona XML
sidecar this doc spends most of its length on. Body is just
`{"acr": ..., "asin": ..., "position_ms": ...}` -- only `acr` from
`get_license()`'s `content_reference` is needed; no `guid`, no
`content_version`, no `codec`, no XML. It propagates to
`GET /1.0/annotations/lastpositions` identically to the Fiona write (verified
live). The Fiona sidecar path (and the whole `guid = "{acr}:{version}"` saga
below) still works and is what proved the mechanism, but there's no reason to
keep the special case now that the same effect is one `client.put` call. The
rest of this doc is kept as the historical trail -- read the
`content_version`/`guid`/XML detail as "how it was figured out", not "what the
code does".

**2026-08-30 update (still current): the 2026-08-26 conclusion below was
wrong.** That investigation wrote to the Fiona endpoint, got a 200 OK, read
its own write back, and concluded the path didn't reach real Audible clients.
It used a placeholder `guid="_LATEST_"`. The real requirement was
`guid="{acr}:{version}"` from `get_license()`'s `content_reference` -- with
that fixed the write **does** reach `GET annotations/lastpositions` (live
capture: `begin="221643"` there → a later `annotations/lastpositions` returned
`position_ms: 221643`, and the user confirmed matching progress on another
Audible client). No native-protocol reverse engineering was needed -- see
"2026-08-30: black-box capture, and the guid fix" below. The `PUT
/1.0/lastpositions/{asin}` endpoint (used now) was found during the follow-up
library-progress investigation; see docs/library-progress-sync-investigation.md.

This app now (`voxcodex/services/progress.py`, `voxcodex/services/api.py`,
wired in `voxcodex/screens/library.py`):

  - reads Audible's own last-position data on library load
    (`fetch_remote_positions`, via `GET annotations/lastpositions`),
  - pushes this app's final position back to Audible after a playback
    session ends (`push_position` → `AudibleAPI.push_last_position`, via
    `PUT /1.0/lastpositions/{asin}`), and
  - always keeps its own local cache (`ProgressStore`) as the resume point
    of record regardless of whether either remote call succeeds.

The push is best-effort and requires the real `acr` for that title (obtained
from `get_license()`, and persisted in the download voucher for offline
plays) -- see `push_position`'s docstring for exactly what happens when it
isn't available.

---

## Original investigation (2026-08-26/27) -- kept for the full trail

This app tracks listening position locally (`ProgressStore` in
`voxcodex/services/progress.py`) and does a best-effort *read* of
Audible's own last-position data (`fetch_remote_positions`, via
`GET annotations/lastpositions`) to seed that local record. It does **not**
push plays made in this app back to Audible, so other devices (the Android
app, the website) never see progress made here.

We looked into fixing that. This document is what we found, so a future
attempt doesn't have to re-derive it. Short version: the one real,
documented, working write path we found does not reach the current Android
app or website, and the mechanism that actually feeds those is an
undocumented native protocol, not a REST call. We backed out the code that
tried this (see "What we tried and reverted" below) and went back to
local-only.

**(This conclusion turned out to be wrong -- see the 2026-08-30 update
above. Kept as-is below since the schema/host findings were correct and the
trail is still useful; only the "does it actually reach other devices"
conclusion was mistaken, and it was mistaken because of a bad `guid`, not
because the endpoint itself doesn't matter.)**

## The core finding

Audible's listening-position sync is not part of the audiobook API
(`api.audible.<domain>`) at all. It rides on Amazon's older, originally
Kindle-focused **Whispersync** system. Critically, there appear to be *two*
different things both called Whispersync, and we only got the older one
working:

1. **Legacy REST "Fiona" sidecar** -- `https://cde-ta-g7g.amazon.com/FionaCDEServiceEngine/sidecar`,
   plain HTTPS, XML request bodies, JSON responses. This is real and
   reachable today (we implemented and tested a working read+write against
   it -- see below). But **writing to it does not show up in the current
   Audible Android app or website** -- confirmed by directly checking both
   after a write. It is not what current Audible clients read listening
   position from.
2. **A modern native sync protocol**, embedded in the current Audible
   Android app under `amazon.whispersync.communication.*` -- connection
   handshakes, custom message framing, its own request-signing scheme,
   spread across roughly 224 classes in the decompiled APK. This is
   presumably what the app and website actually read/write today. We found
   no documentation of it anywhere, and no evidence anyone in the relevant
   open-source communities has reverse-engineered or reimplemented it. It's
   also the kind of Amazon "device sync" SDK that historically ships with
   native (non-Java) components, so even a full decompile of the Java/Kotlin
   layer may not expose everything needed to speak it correctly.

Practical conclusion: the *documented, findable* path is real but proven
(by our own test, on this account) not to be the one that matters. The path
that matters isn't documented publicly anywhere we could find, and
reimplementing it would be a genuine reverse-engineering project against an
undocumented binary protocol, with real risk since it writes into the
account's actual cross-device sync state -- not a small fix.

## What we tried and reverted

Implemented in `voxcodex/services/progress.py` (`push_last_heard`,
`fetch_last_heard`) and wired into `voxcodex/screens/library.py`
(`_open_player` fetched a fresh position before playing; `_on_close` pushed
the final position after stopping). Both functions talked to the Fiona
sidecar endpoint above:

- **Write**: `POST` to the sidecar URL with `?type=AUDI`, body:
  ```xml
  <annotations version="1.0" timestamp="2026-08-26T09:15:30+0800">
    <book key="{ASIN}" type="AUDI" guid="_LATEST_">
      <last_heard action="modify" begin="{position_ms}" timestamp="2026-08-26T09:15:30+0800"/>
    </book>
  </annotations>
  ```
  `Content-Type: application/xml`. Timestamp format: `%Y-%m-%dT%H:%M:%S%z`
  (offset with no colon, e.g. `+0800`).
- **Read**: `GET` the same URL with `?type=AUDI&key={ASIN}`, JSON response:
  ```json
  {"payload": {"records": [{"type": "audible.last_heard", "startPosition": 252352, "creationTime": "..."}]}}
  ```
  (record `type` values are namespaced, e.g. `audible.last_heard`,
  `audible.bookmark`, `audible.note`, `audible.clip` -- different from the
  bare `last_heard` element name used in the write XML.)

**This part genuinely worked** -- verified independently: wrote a position
via the app, then in a separate script/session read it back from the same
endpoint and got the exact value written (`252352`ms). The round trip to
Amazon's servers is real. It just isn't the round trip that the Android app
or website consult.

We reverted this code once the user checked both the Android app and the
website and saw no change. Local-only tracking (what existed before) is
back in place; nothing about download or playback was touched by this.

## Why we believed the sidecar approach at first, and why that confidence was wrong

We cross-referenced two things that agreed on the sidecar's host/path/XML
schema:

- The real Audible Android app's own decompiled model classes
  (`com/audible/mobile/contentlicense/networking/model/LastPositionHeard.smali`
  in a public decompile, [zanjie1999/kindle-eink256](https://github.com/zanjie1999/kindle-eink256)).
- [`rmcrackan/AudibleApi`](https://github.com/rmcrackan/AudibleApi) (C#), the
  API client underlying **Libation**, an actively-maintained, widely-used
  audiobook backup tool -- specifically `Api.Records.cs` and
  `AnnotationBuilder.cs`, which implement both read (`GetRecordsAsync`) and
  write (`CreateRecordsAsync`) against this exact sidecar.

What we missed initially: **Libation's actual application code only ever
calls the read side.** `CreateRecordsAsync` (the write) has zero call sites
in Libation itself -- it's checked with
`gh api "search/code?q=repo:rmcrackan/Libation+CreateRecordsAsync"` and
returns nothing. The one place `GetRecordsAsync` is used
(`BookRecordsDialog.cs`) is a read-only "Clips and Bookmarks" info dialog,
not anything that drives resume position. So the write capability exists in
a reusable API client library but was never exercised/confirmed by the one
real production consumer we could find. It's unverified library code, not
a battle-tested feature -- we over-credited it as "confirmed" during the
first pass.

There's also older, independent prior art confirming the sidecar itself is
real Amazon infrastructure (not something we invented), but none of it
demonstrates a working *write* that surfaces anywhere either:

- A 2015 MobileRead Forums thread ("Reverse Engineering Whispersync") --
  discussion only, no working implementation shared in-thread.
- A detailed 2020 write-up, [ptbrowne's "Reverse engineer whispersync"](https://ptbrowne.github.io/posts/whispersync-reverse-engineering/) --
  hits the *same* `FionaCDEServiceEngine/sidecar` host/path, using the same
  ADP-token RSA request-signing scheme (`audible.Authenticator` in the
  `audible` package already implements this natively -- signing is a pure
  function of method + path + body + the account's device credentials, with
  no dependency on which host the request goes to, which is why our already
  -authenticated `api.client.session` could sign requests to this
  completely different host with no extra auth work). That write-up only
  achieved **read** (book list, highlights/annotations, last-read position)
  for the Kindle app specifically, and is explicit that it's a 2020
  point-in-time analysis with no claim about current app versions.

## What a future attempt would actually need

1. **Confirm there's still nobody who's published this.** Before any more
   reverse engineering, check again (search terms: "whispersync"
   "audible-cli" sync, GitHub issues on `mkb79/Audible` and
   `mkb79/audible-cli`, `rmcrackan/Libation` discussions/issues,
   r/audible / r/audiobooks) in case someone's cracked the native protocol
   since this was written (2026-08-26/27).
2. **If not**, the real work is reverse-engineering
   `amazon.whispersync.communication.*` from a current Audible APK decompile
   -- connection setup, message framing/serialization, its request-signing
   scheme (separate from the ADP-token scheme used elsewhere), and figuring
   out whether any of it lives in native (`.so`) libraries the Java/Kotlin
   decompile can't show. That's a multi-day-or-more protocol reverse
   -engineering project on its own, not a client feature.
3. **Test only against designated throwaway books.** This writes into the
   account's real, shared, cross-device sync state -- not local app state,
   not something a revert undoes. The user has agreed to name specific
   ASINs that are safe to experiment against once/if this is picked back up;
   do not test writes against arbitrary library books.
4. Given the depth above, local-only tracking may reasonably be the
   permanent answer rather than a stopgap -- that's a legitimate outcome of
   this research, not a failure to find the answer.

## Reference: why authentication "just worked" across hosts

`audible.Authenticator.sign_request` (in the installed `audible` package,
`audible/auth.py`) computes its signature from
`f"{method}\n{path}\n{date}\n{body}\n{adp_token}"` -- no host component at
all -- and `audible.Client` installs the `Authenticator` as the `auth=` on
its single shared `httpx.Client`. That means any request sent through
`api.client.session`, to any host, gets signed the same way. This is why no
new auth flow was needed to talk to the Fiona sidecar's completely different
host, and it would very likely also hold for probing other undocumented
Audible-family hosts in the future -- it's the account's device
registration being asserted, not something tied to a particular API.

---

## 2026-08-30: black-box capture, and the guid fix

This picked back up from "What a future attempt would actually need" above,
but changed the plan on step 1: the user intends to publish this
investigation, and wanted to avoid the IP/legal exposure of decompiling
Amazon's app and describing/reproducing its literal code. So this round was
**black-box only** -- capture real traffic from the genuine Audible Android
app and describe the protocol purely as observed input/output behavior,
never touching a decompile. That constraint turned out not to cost
anything: the answer was findable without ever looking at Amazon's code.

### Method

- Android emulator (`whispersync-re` AVD, Pixel 5 profile, Android 15,
  `google_apis` x86_64 image -- deliberately not a Play Store image, so it's
  rootable with no Play Integrity friction) via the Android SDK's
  `cmdline-tools`/`avdmanager`/`emulator` (AUR packages on this Arch
  machine).
- `mitmproxy`/`mitmdump` as the intercepting proxy (emulator's global HTTP
  proxy pointed at `10.0.2.2:8080`, the host-loopback alias from inside the
  guest).
- The real Audible APK (pulled from APKMirror rather than the user's own
  device, at the user's choice), signed into the user's real account.
- TLS interception without relying on the OS trust store at all: **Android
  14+ moved the real system CA trust store into an immutable APEX module**
  (`/apex/com.android.conscrypt/cacerts/`, read-only, ~145 certs) --
  `/system/etc/security/cacerts/` is a legacy path that's no longer
  consulted by Conscrypt on modern Android, so installing mitmproxy's CA
  there (the traditional approach) silently does nothing. Instead: `frida`
  + `objection`'s `android sslpinning disable`, attached to the *running*
  (not freshly spawned) Audible process, patching
  `com.android.org.conscrypt.TrustManagerImpl.verifyChain`/
  `checkTrustedRecursive` and `okhttp3.CertificatePinner.check` at runtime.
  This is a behavioral patch of the app's own trust checks via Frida, not a
  modification to its code on disk -- no decompile, no repackaged APK.
  - Attaching to a **freshly spawned** process reliably failed
    (`java.io.IOException: Permission denied` inside frida-java-bridge's
    `createTemporaryDex`) because the early-spawn window resolves
    `Application.getCacheDir()` before a real `Application` object exists,
    falling back to a hardcoded `/data/local/tmp` default that the app's own
    UID can't write to even after `chmod 777`. Attaching to the already-
    running process instead sidesteps the whole issue.
  - A background/frozen process can't be attached to either: Frida's
    ptrace-based attach hangs (`frida.TimedOutError`) against a process in
    the kernel's `do_freezer_trap` wait state (Android's cached-app
    freezer). Foreground the app first (`am start`/`monkey -c
    android.intent.category.LAUNCHER`), confirm it's not frozen
    (`ps -A` wait-channel column), then attach.
  - Android's own background connectivity probe (a plain HTTP(S) request to
    a Google host, done by `system_server`, not the app) uses the *real*
    trust store, so it fails once the network is proxied and the OS marks
    the network "partial" (not "validated") -- which some apps, Audible
    included, treat as "no network" regardless of whether the app's own
    (Frida-patched) traffic is working fine. Fix: `adb shell settings put
    global captive_portal_http_url ''` / `captive_portal_https_url ''` on
    both.
  - The emulator itself crashed three times across this session --
    `SIGSEGV` in its own `RenderThread` (GPU-emulation translation layer,
    confirmed via `coredumpctl`/`gdb`, though Google's bundled binaries ship
    with zero build-IDs so the crashing frame itself couldn't be
    symbolized), under both host-GPU passthrough and `-gpu
    swiftshader_indirect`. Software rendering also caused a separate,
    non-fatal problem: heavy CPU contention right after boot triggered
    repeated System UI ANRs and the Audible process itself being killed
    every 60-90s (`ActivityManager: ... has died: fg TRNB`) regardless of
    Frida. `-gpu guest` (fully in-guest software rendering, no host-side GL
    translation layer at all -- a different code path from both of the
    above) fixed both the crashes and the app-death cycling for the rest of
    the session.

### The finding: the write path from 2026-08-26 was real, just called wrong

Capturing a normal play/pause/seek session against a book the user had
already finished and named as safe to test with (*The Subtle Art of Not
Giving a F\*ck*, ASIN `B01L790CUU`) showed the real app itself reading *and
writing* the exact same `FionaCDEServiceEngine/sidecar` endpoint documented
above, on every meaningful position change:

```
GET  https://cde-ta-g7g.amazon.com/FionaCDEServiceEngine/sidecar?type=AUDI&key=B01L790CUU&format=M4A_XHE&guid=CR%21REDACTED-ACR%3A116727321&software_rev=...
POST https://cde-ta-g7g.amazon.com/FionaCDEServiceEngine/sidecar
     <?xml version="1.0" encoding="UTF-8"?>
     <annotations version="1.0" timestamp="2026-08-30T18:53:59+0800">
       <book key="B01L790CUU" type="AUDI" version="116727321" guid="CR!REDACTED-ACR:116727321" format="M4A_XHE">
         <last_heard action="modify" begin="221643" timestamp="2026-08-30T18:53:58+0800"/>
       </book>
     </annotations>
```

Compare that `<book>` element to the 2026-08-26 write further up this
document: `<book key="{ASIN}" type="AUDI" guid="_LATEST_">`. Three
differences: a real `guid` (`{acr}:{version}`, not the placeholder
`_LATEST_`), plus `version` and `format` attributes the earlier attempt
didn't send at all. Both `acr` and `version` come straight out of a
`POST content/{asin}/licenserequest` response, nested at
`content_license.content_metadata.content_reference.{acr,version}` -- a
response VoxCodex already fetches (`AudibleAPI.get_license`, requesting the
`content_reference` response group) for every play, so no new API call was
needed to get them.

**Proof this actually propagates**, not just an echo from the same
endpoint reading back its own write: a separate, genuinely different
endpoint -- `GET api.audible.<domain>/1.0/annotations/lastpositions`, the
*exact* call `fetch_remote_positions` in this codebase already makes --
returned the identical position and a fresh timestamp immediately after:

```json
{
    "asin_last_position_heard_annots": [
        {
            "asin": "B01L790CUU",
            "last_position_heard": {
                "last_updated": "2026-08-30 10:54:00.671",
                "position_ms": 221643,
                "status": "Exists"
            }
        }
    ]
}
```

(`10:54:00 UTC` = `18:54:00` local, i.e. one second after the Fiona POST
above completed.) The same effect showed up a third way, in the
`last_position_heard` field embedded directly in a fresh
`content/{asin}/licenserequest` response after an earlier write of
`begin="10033"` -- three independent read paths, all reflecting the same
write. The user separately confirmed the position matched on another real
Audible client, which is the only check no capture can substitute for.

So the 2026-08-26 conclusion ("this write path is real but isn't what
current clients read") was backwards: the endpoint was never the problem.
An unresolvable placeholder `guid` meant the write landed somewhere the
read side never looked -- not that the read side ignores this endpoint.

### Why the real app *also* writes to `PUT /1.0/stats/events` -- not a competing/transitional system

The same capture showed frequent `PUT api.audible.<domain>/1.0/stats/events`
calls carrying position-shaped fields too, which raised the obvious
question: is Audible mid-migration between two systems doing the same job?
No -- they're for different jobs, and a real client legitimately needs
both:

- **`stats/events`** is an analytics/telemetry log: discrete lifecycle
  events (`StartListening`, `MarkAsUnfinished`, `DownloadStart`/
  `DownloadComplete`) plus interval-shaped `"Listening"` events recording
  `event_start_position` -> `event_end_position` pairs, e.g. `213925 ->
  221579` ("listened continuously from X to Y"). That's session/engagement
  telemetry -- plausibly feeding royalty calculation (audiobook royalties
  are commonly paid per-minute-listened), usage analytics, or
  recommendations -- an append-only history of what happened, not a
  queryable "where am I now."
- **The Fiona sidecar's `last_heard`** is a single mutable record per
  `guid` answering exactly that: "where should any device resume this book
  from." That's the one feeding `annotations/lastpositions`.

VoxCodex only implements the second (`push_last_heard` /
`services.progress.push_position`) -- the first is Amazon-internal
telemetry this app has no reason to emit.

### Implementation

Shipped in `voxcodex/services/api.py` (`AudibleAPI.push_last_heard`,
extending `License` with `acr`/`content_version`),
`voxcodex/services/progress.py` (`push_position`, the best-effort wrapper),
`voxcodex/services/download.py` (voucher now also persists `acr`/
`content_version` so offline/downloaded plays can push too), and wired into
`voxcodex/screens/library.py` (`_open_player`/`_launch_player`/`_on_close`
thread the identifiers through and push once on player close). See those
modules' docstrings for the exact contract -- in particular, `push_position`
returns `False` and never raises when the identifiers aren't available,
since this app's own local resume point must never depend on Audible's
sync working.
