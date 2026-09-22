"""Black-box probe helpers for the library-progress sync investigation.

Run pieces of this via `python -i tools/whispersync_probe.py` (from the repo
root, with the dev venv active) or by importing and calling the functions.
Uses whatever auth file VoxCodex has already saved for you. Read-only unless
you call the push_* helpers explicitly.

Substitute EXAMPLE_ASIN_1/EXAMPLE_ASIN_2 below with two ASINs from your own
library before running -- these are just placeholders.
"""
from __future__ import annotations

import json
from datetime import datetime

import audible
from voxcodex import config

EXAMPLE_ASIN_1 = "B0XXXXXXXX"
EXAMPLE_ASIN_2 = "B0YYYYYYYY"

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
    for a in (EXAMPLE_ASIN_1, EXAMPLE_ASIN_2):
        p(f"library/{a}", lib_progress(a))
    p("lastpositions", lastpositions(EXAMPLE_ASIN_1, EXAMPLE_ASIN_2))


if __name__ == "__main__":
    snapshot("initial")
