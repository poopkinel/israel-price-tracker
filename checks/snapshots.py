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
  none     no good saved copy found although we submitted the page; the last column then lists
           whatever copies exist (date:HTTP status) and what the archive says about our last request
  A "?" after the result: the "before" number is on the page but not next to a currency sign or a
  price label, so it may be something else.
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


NEAR_PRICE = re.compile(r"₪|&#8362;|ILS|NIS|price|compare", re.I)


def find_number(html: str, value: str) -> tuple[str, bool]:
    """(up to 60 characters around a place the price appears as a whole number, whether a currency
    sign or the word price/compare is next to it). Prefers such a place; ('', False) if none at all."""
    weak = ""
    for form in number_forms(value):
        for m in re.finditer(r"(?<![\w.,-])" + re.escape(form) + r"(?![\w]|[.,]\d)", html):
            ctx = " ".join(html[max(0, m.start() - 30):m.end() + 30].split())
            if NEAR_PRICE.search(ctx):
                return ctx, True
            weak = weak or ctx
    return weak, False


def verdict(html: str, price: str, before: str) -> tuple[str, str]:
    """('both' | 'price' | 'before' | 'neither', context of the "before" match).

    'both?' / 'before?' when the "before" number is on the page but not next to a currency sign
    or a price label, so it may be something else (a size, a style rule)."""
    (p, _), (b, strong) = find_number(html, price), find_number(html, before)
    res = "both" if p and b else "price" if p else "before" if b else "neither"
    return res + ("?" if b and not strong else ""), b


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
    submitted: dict[str, tuple[str, str]] = {}  # url -> (day, job id) of the last accepted request
    for f in sorted(ARCHIVE_LOG.glob("*.csv")):
        for r in read_csv(f):
            if r["result"] in ("submitted", "saved", "recent"):
                submitted[r["url"]] = (r["date"], r.get("job_id", ""))
    out = []
    for s in load_stores():
        rows = [r for r in read_csv(CURRENT / f"{s['id']}.csv")
                if r["on_sale"] == "1" and r["sale_since"] == first_day and r["last_seen"] == last_day
                and r["url"] in submitted]
        rows.sort(key=lambda r: hashlib.md5(r["url"].encode()).hexdigest())
        out += [dict(r, store=s["id"], submitted=submitted[r["url"]][0], job_id=submitted[r["url"]][1]) for r in rows[:per_store]]
    return out


def latest_snapshot(session, url: str, since: str) -> str:
    """Timestamp (YYYYMMDDhhmmss) of the newest good saved copy since that day, or ''."""
    r = session.get(CDX, params={"url": url, "output": "json", "fl": "timestamp", "from": since.replace("-", ""),
                                 "filter": "statuscode:200", "limit": "-1"}, timeout=60)
    r.raise_for_status()
    rows = r.json() if r.text.strip() else []
    return rows[-1][0] if len(rows) > 1 else ""


def why_none(session, t: dict) -> str:
    """For a page with no good copy: the copies that do exist, and the state of our last request."""
    out = []
    try:
        r = session.get(CDX, params={"url": t["url"], "output": "json", "fl": "timestamp,statuscode", "limit": "-3"}, timeout=60)
        rows = r.json()[1:] if r.ok and r.text.strip() else []
        out.append("copies: " + (" ".join(f"{ts[:8]}:{code}" for ts, code in rows) or "none at all"))
        time.sleep(DELAY)
        if t.get("job_id"):
            j = session.get(f"https://web.archive.org/save/status/{t['job_id']}", headers={"Accept": "application/json"}, timeout=60).json()
            out.append(f"last request ({t['submitted']}): " + " ".join(str(j.get(k, "")) for k in ("status", "status_ext", "message") if j.get(k))[:120])
            time.sleep(DELAY)
    except (requests.RequestException, ValueError) as e:
        out.append(f"lookup failed: {type(e).__name__}")
    return "; ".join(out)


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
            else:
                ctx = why_none(s, t)
        except (requests.RequestException, ValueError) as e:
            res = f"failed:{type(e).__name__}"
        totals.setdefault(t["store"], {}).setdefault(res.split(":")[0], 0)
        totals[t["store"]][res.split(":")[0]] += 1
        line = f"| {t['store']} | {t['url']} | {ts[:8]} | {price} | {before} | {res} | {ctx.replace('|', '/')[:200]} |"
        print(line, flush=True)
        lines.append(line)
    head = ["| store | page | saved on | price | before | result | where the before price appears, or why there is no copy |", "|---|---|---|---|---|---|---|"]
    summary = ["| store | " + " | ".join(k for k in ("both", "both?", "price", "before", "before?", "neither", "none", "no-data", "failed")) + " |", "|---|---|---|---|---|---|---|---|---|---|"]
    for store, c in totals.items():
        summary.append(f"| {store} | " + " | ".join(str(c.get(k, 0)) for k in ("both", "both?", "price", "before", "before?", "neither", "none", "no-data", "failed")) + " |")
    text = "\n".join(["## Archived snapshots against the data", ""] + summary + [""] + head + lines) + "\n"
    print("\n".join(summary), flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
