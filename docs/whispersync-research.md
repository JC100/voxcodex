# Cross-device listening-position sync: research notes (2026-08-26)

## Status: not implemented. Local-only progress tracking is the current, intentional design.

This app tracks listening position locally (`ProgressStore` in
`audible_tui/services/progress.py`) and does a best-effort *read* of
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

Implemented in `audible_tui/services/progress.py` (`push_last_heard`,
`fetch_last_heard`) and wired into `audible_tui/screens/library.py`
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
