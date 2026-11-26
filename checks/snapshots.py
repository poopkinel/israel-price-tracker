"""Compare what our data says with what a buyer saw: look up archived copies of long-discounted
product pages in the Wayback Machine and check whether the price and the "before" price recorded
for that day appear on the saved page.

Read-only: prints a table to the job log (and to the GitHub job summary); writes nothing to data/.
It talks only to web.archive.org, never to the stores. Run it from the manual workflow
"check archived snapshots" or locally:  python -m checks.snapshots [per_store]

How to read the result
  both     the price and the "before" price of that day are both on the saved page
  price    the price is there, the "before" price is not (the buyer may not have been shown it)
  before   the "before" price is there, the price is not
  neither  neither number is on the page (page built by scripts, or the data differs from the page)
  none     no saved copy found (the submission may have failed on the archive's side)
A number is searched as text in the page source, in the usual ways of writing it (1,299 / 1299.00 /
129900 agorot). A match is evidence, not proof: short prices can also belong to another product on
the same page. The context printed next to each "before" match is there for a human to judge.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import sys
import time

import requests

from tracker.common import ARCHIVE_LOG, CURRENT, HISTORY, RUNS, USER_AGENT, load_stores, read_csv

CDX = "https://web.archive.org/cdx/search/cdx"
DELAY = 4.0  # seconds between requests to web.archive.org


def number_forms(value: str) -> list[str]:
    """The ways a price such as '1299.00' is written in a page: 1299, 1,299, 1299.00, 1,299.00, 129900."""
    v = round(float(value), 2)
    whole, cents = int(v), int(round(v * 100)) % 100
    w = [str(whole)] + ([f"{whole:,}"] if whole >= 1000 else [])
    forms = [f"{x}.{cents:02d}" for x in w]
    if cents == 0:
        forms += w
    elif cents % 10 == 0:
        forms += [f"{x}.{cents // 10}" for x in w]
    forms.append(str(int(round(v * 100))))  # agorot, as Shopify writes prices inside its scripts
    return forms


def find_number(html: str, value: str) -> str:
    """Up to 60 characters around the first place the price appears as a whole number, else ''."""
    for form in number_forms(value):
        m = re.search(r"(?<![\d.,])" + re.escape(form) + r"(?![\d]|[.,]\d)", html)
        if m:
            return " ".join(html[max(0, m.start() - 30):m.end() + 30].split())
    return ""


def verdict(html: str, price: str, before: str) -> tuple[str, str]:
    """('both' | 'price' | 'before' | 'neither', context of the "before" match)."""
    p, b = find_number(html, price), find_number(html, before)
    return ("both" if p and b else "price" if p else "before" if b else "neither"), b


def price_on(history: list[dict], product_id: str, day: str) -> tuple[str, str]:
    """(price, before price) our data held for this product on that day; ('', '') if none yet."""
    out = ("", "")
    for r in history:  # history is appended in date order
        if r["product_id"] == product_id and r["date"] <= day:
            out = (r["price"], r["compare_at"])
    return out


def pick_sample(per_store: int, first_day: str, last_day: str) -> list[dict]:
    """Per store, the products on sale since the first day of tracking and still seen on the last
    one, whose page we submitted at least once.

    The choice is fixed (by a hash of the URL), so two runs check the same pages."""
    submitted: dict[str, str] = {}
    for f in sorted(ARCHIVE_LOG.glob("*.csv")):
        for r in read_csv(f):
            if r["result"] in ("submitted", "saved", "recent"):
                submitted[r["url"]] = r["date"]
    out = []
    for s in load_stores():
        rows = [r for r in read_csv(CURRENT / f"{s['id']}.csv")
                if r["on_sale"] == "1" and r["sale_since"] == first_day and r["last_seen"] == last_day
                and r["url"] in submitted]
        rows.sort(key=lambda r: hashlib.md5(r["url"].encode()).hexdigest())
        out += [dict(r, store=s["id"], submitted=submitted[r["url"]]) for r in rows[:per_store]]
    return out


def latest_snapshot(session, url: str, since: str) -> str:
    """Timestamp (YYYYMMDDhhmmss) of the newest good saved copy since that day, or ''."""
    r = session.get(CDX, params={"url": url, "output": "json", "fl": "timestamp", "from": since.replace("-", ""),
                                 "filter": "statuscode:200", "limit": "-1"}, timeout=60)
    r.raise_for_status()
    rows = r.json() if r.text.strip() else []
    return rows[-1][0] if len(rows) > 1 else ""


def main(argv: list[str]) -> int:
    per_store = int(argv[0]) if argv else 4
    days = sorted(f.stem for f in RUNS.glob("*.json"))  # the days a collection ran
    first_day = days[0]
    sample = pick_sample(per_store, first_day, days[-1])
    print(f"{len(sample)} pages to check: on sale every day from {first_day} to {days[-1]}", flush=True)
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    histories: dict[str, list[dict]] = {}
    lines, totals = [], {}
    for t in sample:
        res, ctx, ts, price, before = "none", "", "", "", ""
        try:
            ts = latest_snapshot(s, t["url"], first_day)
            time.sleep(DELAY)
            if ts:
                day = dt.datetime.strptime(ts, "%Y%m%d%H%M%S").date().isoformat()
                hist = histories.setdefault(t["store"], read_csv(HISTORY / f"{t['store']}.csv"))
                price, before = price_on(hist, t["product_id"], day)
                page = s.get(f"https://web.archive.org/web/{ts}id_/{t['url']}", timeout=90)
                time.sleep(DELAY)
                page.raise_for_status()
                res, ctx = verdict(page.text, price, before) if price and before else ("no-data", "")
        except (requests.RequestException, ValueError) as e:
            res = f"failed:{type(e).__name__}"
        totals.setdefault(t["store"], {}).setdefault(res.split(":")[0], 0)
        totals[t["store"]][res.split(":")[0]] += 1
        line = f"| {t['store']} | {t['url']} | {ts[:8]} | {price} | {before} | {res} | {ctx.replace('|', '/')[:90]} |"
        print(line, flush=True)
        lines.append(line)
    head = ["| store | page | saved on | price | before | result | where the before price appears |", "|---|---|---|---|---|---|---|"]
    summary = ["| store | " + " | ".join(k for k in ("both", "price", "before", "neither", "none", "no-data", "failed")) + " |", "|---|---|---|---|---|---|---|---|"]
    for store, c in totals.items():
        summary.append(f"| {store} | " + " | ".join(str(c.get(k, 0)) for k in ("both", "price", "before", "neither", "none", "no-data", "failed")) + " |")
    text = "\n".join(["## Archived snapshots against the data", ""] + summary + [""] + head + lines) + "\n"
    print("\n".join(summary), flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
