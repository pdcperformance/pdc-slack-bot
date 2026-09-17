#!/usr/bin/env python3
"""
PDC daily ad check -> Slack.

Pulls yesterday's Meta (Facebook) ad-set stats for the PDC campaign and posts a
short summary to a Slack channel via an Incoming Webhook.

No third-party packages required (Python standard library only).

Environment variables (set these as GitHub Actions secrets):
  META_ACCESS_TOKEN   - a Meta "System User" token with the ads_read permission
  SLACK_WEBHOOK_URL   - a Slack Incoming Webhook URL for the target channel
  CAMPAIGN_ID         - (optional) defaults to the PDC "1st campaign" id
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
CAMPAIGN_ID = os.environ.get("CAMPAIGN_ID", "120246423368000680")
DATA_FILE = os.environ.get("DATA_FILE", "data.json")

# The two ad sets we compare, in display order (A first, then B).
# Reel A = the original device reel; Reel B = the slot now running the carousel.
AD_SETS = [
    ("120246423368010680", "Reel A · device reel"),
    ("120246980935840680", "Reel B · carousel"),
]
ORDER = [aid for aid, _ in AD_SETS]
LABELS = dict(AD_SETS)

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
    params = {
        "level": "adset",
        "fields": "adset_id,adset_name,spend,reach,actions",
        "date_preset": "yesterday",
        "access_token": TOKEN,
        "limit": 100,
    }
    url = f"{API}/{CAMPAIGN_ID}/insights?" + urllib.parse.urlencode(params)
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


def summarize(row):
    aid = row.get("adset_id")
    spend = float(row.get("spend", 0) or 0)
    reach = int(row.get("reach", 0) or 0)
    carts = add_to_carts(row.get("actions"))
    cpc = (spend / carts) if carts else None
    return {
        "label": LABELS.get(aid, row.get("adset_name", aid)),
        "carts": carts,
        "spend": spend,
        "reach": reach,
        "cpc": cpc,
    }


def build_text(rows):
    parts = ["*PDC daily ad check — yesterday*"]
    stats = [summarize(r) for r in rows]
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
                f"_Cheaper per add-to-cart: {win['label'].split(' · ')[0]} "
                f"(${win['cpc']:.2f} vs ${lose['cpc']:.2f})._"
            )
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


def update_history(rows):
    """Append yesterday's A/B numbers to data.json (deduped by date) for the graph."""
    by_id = {r.get("adset_id"): r for r in rows}
    a = by_id.get("120246423368010680")
    b = by_id.get("120246980935840680")
    if not a or not b:
        return  # need both reels to record a comparison point
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

    rows = [r for r in js.get("data", []) if r.get("adset_id") in ORDER]
    rows.sort(key=lambda r: ORDER.index(r["adset_id"]))

    if not rows:
        post_slack(":warning: PDC ad check ran but found no data for the two ad sets yesterday "
                   "(both may have been paused or out of funds).")
        return

    update_history(rows)
    post_slack(build_text(rows))
    print("Posted to Slack.")


if __name__ == "__main__":
    main()
