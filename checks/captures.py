"""What became of the pages we asked the Wayback Machine to save?

"submitted" in data/archive/<date>.csv means the archive accepted the request and gave it a job
number; the capture itself happens afterwards and can fail. This check asks the archive for the
outcome of a fixed sample of those jobs and counts the outcomes per store and per day.

Read-only: prints tables to the job log (and to the GitHub job summary); writes nothing to data/.
It talks only to web.archive.org, never to the stores, and needs the same IA_ACCESS_KEY /
IA_SECRET_KEY as the archive run (the archive tells the outcome of a job only to the account that
asked for it). Run it from the manual workflow "check capture outcomes" or locally:
    python -m checks.captures [per_store_per_day] [days]

How to read the result
  saved          the capture finished and the store answered 200 (or the archive did not say what
                 the store answered)
  saved:<code>   the capture finished, but what was saved is the store's answer <code>
                 (for example 429 or 404), not the product page
  pending        the archive has not finished the capture yet
  error:<why>    the capture failed; <why> is the archive's own short reason
  unknown        the archive no longer knows the job (too old) or gave an answer we cannot read
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

import requests

from tracker.common import ARCHIVE_LOG, USER_AGENT, read_csv

STATUS = "https://web.archive.org/save/status/"
DELAY = 2.0  # seconds between requests to web.archive.org


def outcome(j: dict) -> str:
    """A short label for the archive's answer about one job (see the module text)."""
    status = str(j.get("status") or "").lower()
    if status == "success":
        code = str(j.get("http_status") or "")
        return "saved" if code in ("", "200") else f"saved:{code}"
    if status == "pending":
        return "pending"
    if status == "error":
        why = str(j.get("status_ext") or j.get("exception") or j.get("message") or "no reason given")
        return why if why.startswith("error:") else f"error:{why[:60]}"
    return "unknown"


def pick_jobs(days: list[str], per_store: int) -> list[dict]:
    """Up to per_store accepted requests per store and day, the same ones on every run
    (chosen by a hash of the job number)."""
    out = []
    for day in days:
        by_store: dict[str, list[dict]] = {}
        for r in read_csv(ARCHIVE_LOG / f"{day}.csv"):
            if r["result"] == "submitted" and r.get("job_id"):
                by_store.setdefault(r["store"], []).append(r)
        for store in sorted(by_store):
            rows = sorted(by_store[store], key=lambda r: hashlib.md5(r["job_id"].encode()).hexdigest())
            out += rows[:per_store]
    return out


def table(counts: dict[str, dict[str, int]], first: str) -> list[str]:
    """Markdown table: one row per key of counts, one column per outcome seen anywhere."""
    cols = sorted({k for c in counts.values() for k in c}, key=lambda k: (k != "saved", k))
    lines = [f"| {first} | asked | " + " | ".join(cols) + " |", "|---|---|" + "---|" * len(cols)]
    for key, c in counts.items():
        lines.append(f"| {key} | {sum(c.values())} | " + " | ".join(str(c.get(k, 0)) for k in cols) + " |")
    return lines


def main(argv: list[str]) -> int:
    per_store = int(argv[0]) if argv else 10
    n_days = int(argv[1]) if len(argv) > 1 else 3
    key, secret = os.environ.get("IA_ACCESS_KEY"), os.environ.get("IA_SECRET_KEY")
    if not (key and secret):
        print("IA_ACCESS_KEY / IA_SECRET_KEY are not set; the archive only tells the outcome of a job to its owner")
        return 1
    days = sorted(f.stem for f in ARCHIVE_LOG.glob("*.csv"))[-n_days:]
    jobs = pick_jobs(days, per_store)
    print(f"{len(jobs)} accepted requests to ask about, from the logs of {', '.join(days)}", flush=True)
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json", "Authorization": f"LOW {key}:{secret}"})
    by_store: dict[str, dict[str, int]] = {}
    by_day: dict[str, dict[str, int]] = {}
    shown: set[str] = set()
    details = []
    for t in jobs:
        try:
            r = s.get(STATUS + t["job_id"], timeout=60)
            j = r.json()
            res = outcome(j) if isinstance(j, dict) else "unknown"
        except (requests.RequestException, ValueError) as e:
            j, res = {}, f"failed:{type(e).__name__}"
        by_store.setdefault(t["store"], {}).setdefault(res, 0)
        by_store[t["store"]][res] += 1
        by_day.setdefault(t["date"], {}).setdefault(res, 0)
        by_day[t["date"]][res] += 1
        line = f"| {t['date']} | {t['store']} | {t['submitted_at']} | {res} | {t['url']} |"
        print(line, flush=True)
        details.append(line)
        if res not in shown and isinstance(j, dict):
            # the archive's whole answer, once per kind of outcome, without the long lists
            shown.add(res)
            small = {k: v for k, v in j.items() if k not in ("resources", "outlinks", "counters")}
            print(f"  full answer for '{res}': {json.dumps(small, ensure_ascii=False)[:600]}", flush=True)
        time.sleep(DELAY)
    text = "\n".join(["## Outcome of accepted archive requests", "", "### Per store", ""] + table(by_store, "store")
                     + ["", "### Per day of the request", ""] + table(by_day, "day")
                     + ["", "### Every request asked about", "", "| day | store | submitted at (UTC) | outcome | page |", "|---|---|---|---|---|"] + details) + "\n"
    print("\n".join(table(by_store, "store") + [""] + table(by_day, "day")), flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
