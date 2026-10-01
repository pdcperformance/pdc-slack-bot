#!/usr/bin/env python3
"""
PDC daily ad check -> Slack.

Pulls yesterday's Meta (Facebook) stats for the two ADS inside the live PDC
ad set and posts a short A/B summary to a Slack channel via an Incoming Webhook.

(Earlier this compared two separate ad SETS. The campaign was consolidated into
one ad set that A/B-tests two ads, so we now compare the two ads inside it.)

No third-party packages required (Python standard library only).

Environment variables (set these as GitHub Actions secrets):
  META_ACCESS_TOKEN   - a Meta "System User" token with the ads_read permission
  SLACK_WEBHOOK_URL   - a Slack Incoming Webhook URL for the target channel
  LIVE_ADSET          - (optional) defaults to the live "Add to cart" ad set id
"""

import os
import sys
import json
import urllib.request
import urllib.parse
import urllib.error

API_VERSION = "v21.0"
API = f"https://graph.facebook.com/{API_VERSION}"

TOKEN = os.environ.get("META_ACCESS_TOKEN")
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL")
# The single live ad set (optimized for Add to cart) that holds the two A/B ads.
LIVE_ADSET = os.environ.get("LIVE_ADSET", "120247691773520680")
DATA_FILE = os.environ.get("DATA_FILE", "data.json")

# Inside the live ad set we A/B two ads. Each is pinned to a graph slot (a / b)
# by a lowercase substring of its ad name, so the slots stay consistent even if
# an ad is renamed with a suffix like " - Copy". If an ad's name stops matching,
# the leftover ad still fills the empty slot (see assign_slots), so the graph
# keeps updating regardless.
SLOT_MATCH = [
    ("a", "dual", "Dual Sensor"),    # (slot, name substring to match, display label)
    ("b", "rom",  "rom and more"),
]
SLOT_LABELS = {slot: label for slot, _needle, label in SLOT_MATCH}

# Meta returns "add to cart" under one of several action_type names depending on
# how the pixel/event is set up. We pick the best single match (never sum, to
# avoid double-counting an aggregate like omni_* on top of a specific type).
ATC_PRIORITY = [
    "offsite_conversion.fct.add_to_cart",
    "onsite_web_add_to_cart",
    "web_add_to_cart",
    "omni_add_to_cart",
    "add_to_cart",
]


def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "pdc-slack-bot"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def get_insights():
    # Ask the live ad set for yesterday's numbers broken out per ad.
    params = {
        "level": "ad",
        "fields": "ad_id,ad_name,adset_id,spend,reach,actions",
        "date_preset": "yesterday",
        "access_token": TOKEN,
        "limit": 100,
    }
    url = f"{API}/{LIVE_ADSET}/insights?" + urllib.parse.urlencode(params)
    return http_get_json(url)


def add_to_carts(actions):
    if not actions:
        return 0
    by_type = {}
    for a in actions:
        t = a.get("action_type", "")
        try:
            by_type[t] = by_type.get(t, 0) + int(float(a.get("value", 0)))
        except (TypeError, ValueError):
            pass
    for t in ATC_PRIORITY:
        if t in by_type:
            return by_type[t]
    for t, v in by_type.items():          # last resort: any *add_to_cart* type
        if "add_to_cart" in t:
            return v
    return 0


def assign_slots(rows):
    """Map the ad rows to graph slots {'a': row|None, 'b': row|None} by ad name,
    falling back to filling empty slots with any leftover ads (stable by ad_id)."""
    slots = {"a": None, "b": None}
    remaining = list(rows)
    for slot, needle, _label in SLOT_MATCH:
        for r in list(remaining):
            if needle in (r.get("ad_name") or "").lower():
                slots[slot] = r
                remaining.remove(r)
                break
    remaining.sort(key=lambda r: r.get("ad_id", ""))
    for slot in ("a", "b"):
        if slots[slot] is None and remaining:
            slots[slot] = remaining.pop(0)
    return slots


def summarize(row, label):
    spend = float(row.get("spend", 0) or 0)
    reach = int(row.get("reach", 0) or 0)
    carts = add_to_carts(row.get("actions"))
    cpc = (spend / carts) if carts else None
    return {"label": label, "carts": carts, "spend": spend, "reach": reach, "cpc": cpc}


def build_text(slots):
    parts = ["*PDC daily ad check — yesterday*"]
    stats = [summarize(slots[s], SLOT_LABELS[s]) for s in ("a", "b") if slots.get(s)]
    for s in stats:
        cpc = f"${s['cpc']:.2f}/cart" if s["cpc"] is not None else "—"
        parts.append(
            f"*{s['label']}* — {s['carts']} add-to-carts · {cpc} "
            f"· ${s['spend']:.2f} spent · {s['reach']:,} reach"
        )
    # who was cheaper per add-to-cart
    priced = [s for s in stats if s["cpc"] is not None]
    if len(priced) == 2:
        win = min(priced, key=lambda s: s["cpc"])
        lose = max(priced, key=lambda s: s["cpc"])
        if win["cpc"] != lose["cpc"]:
            parts.append(
                f"_Cheaper per add-to-cart: {win['label']} "
                f"(${win['cpc']:.2f} vs ${lose['cpc']:.2f})._"
            )
    parts.append("<https://pdcperformance.github.io/pdc-slack-bot/|\U0001F4C8 See the full history graph>")
    return "\n".join(parts)


def post_slack(text):
    if not SLACK_WEBHOOK:
        print("SLACK_WEBHOOK_URL not set; would have posted:\n" + text)
        return
    data = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        SLACK_WEBHOOK, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def update_history(slots):
    """Append yesterday's A/B numbers to data.json (deduped by date) for the graph."""
    a = slots.get("a")
    b = slots.get("b")
    if not a or not b:
        return  # need both ads to record a comparison point
    date = a.get("date_start") or b.get("date_start")
    if not date:
        return
    rec = {
        "date": date,
        "a_carts": add_to_carts(a.get("actions")),
        "b_carts": add_to_carts(b.get("actions")),
        "a_spend": round(float(a.get("spend", 0) or 0), 2),
        "b_spend": round(float(b.get("spend", 0) or 0), 2),
    }
    try:
        with open(DATA_FILE) as f:
            hist = json.load(f)
    except (FileNotFoundError, ValueError):
        hist = []
    hist = [h for h in hist if h.get("date") != date]  # replace same-day if re-run
    hist.append(rec)
    hist.sort(key=lambda h: h.get("date", ""))
    with open(DATA_FILE, "w") as f:
        json.dump(hist, f, indent=2)
    print(f"History updated: {date} (now {len(hist)} days)")


def main():
    if not TOKEN:
        print("ERROR: META_ACCESS_TOKEN is not set", file=sys.stderr)
        sys.exit(2)
    try:
        js = get_insights()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:400]
        post_slack(f":warning: PDC ad check couldn't read the Meta API (HTTP {e.code}). {body}")
        print(body, file=sys.stderr)
        sys.exit(1)

    rows = js.get("data", [])  # already scoped to the live ad set by the URL
    slots = assign_slots(rows)

    if not slots["a"] and not slots["b"]:
        post_slack(":warning: PDC ad check ran but found no ad data for the live ad set yesterday "
                   "(it may still be in review, paused, or had no spend).")
        return

    update_history(slots)
    post_slack(build_text(slots))
    print("Posted to Slack.")


if __name__ == "__main__":
    main()
