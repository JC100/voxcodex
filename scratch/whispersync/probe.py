"""Black-box probe helpers for the library-progress sync investigation.

Run pieces of this via `python -i probe.py` or by calling functions.
Uses the already-registered VoxCodex auth (audible.com.au). Read-only unless
you call the push_* helpers explicitly.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, "/home/jake/Work/audible-tui/.claude/worktrees/audible-tui-build")

import audible
from voxcodex import config

SUBTLE = "B01L790CUU"   # The Subtle Art of Not Giving a F*ck (Purchase rights, finished)
ALGO = "B07DGFS4LM"     # Once Upon an Algorithm (finished)

auth = audible.Authenticator.from_file(config.AUTH_FILE)
client = audible.Client(auth=auth, timeout=30)

FULL_LIB_RG = (
    "contributors, customer_rights, media, product_attrs, product_desc, "
    "product_extended_attrs, series, is_finished, is_downloaded, "
    "listening_status, percent_complete, product_details, provided_review, "
    "relationships, review_attrs, categories, badge_types"
)


def p(label, obj):
    print(f"\n===== {label} =====")
    print(json.dumps(obj, indent=2, default=str))


def lib_item(asin, rg=FULL_LIB_RG):
    r = client.get(f"library/{asin}", response_groups=rg)
    return r


def lib_progress(asin):
    r = client.get(
        f"library/{asin}",
        response_groups="is_finished, listening_status, percent_complete, product_details",
    )
    it = r.get("item", r)
    return {
        "asin": asin,
        "title": (it.get("title") or "")[:40],
        "percent_complete": it.get("percent_complete"),
        "is_finished": it.get("is_finished"),
        "listening_status": it.get("listening_status"),
        "runtime_min": it.get("runtime_length_min"),
    }


def lastpositions(*asins):
    r = client.get("annotations/lastpositions", asins=",".join(asins))
    return r


def stats_aggregates(**kw):
    return client.get("stats/aggregates", **kw)


def stats_status(**kw):
    return client.get("stats/status", **kw)


def snapshot(tag=""):
    print(f"\n######## SNAPSHOT {tag} {datetime.now().isoformat()} ########")
    for a in (SUBTLE, ALGO):
        p(f"library/{a}", lib_progress(a))
    p("lastpositions", lastpositions(SUBTLE, ALGO))


if __name__ == "__main__":
    snapshot("initial")
