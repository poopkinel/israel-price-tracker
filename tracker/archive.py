"""Ask the Wayback Machine to save discounted product pages, so each one is archived every 3 days.

Today's share = products currently on sale whose URL falls in today's bucket (hash(url) % 3).
Products discounted for the longest time go first. Several runs per day continue where the
previous one stopped (URLs already logged today are skipped).

Environment:
  IA_ACCESS_KEY / IA_SECRET_KEY  archive.org S3 keys (optional; higher limits when set)
  MAX_SAVES                      max submissions this run (default 2500)
  TIME_BUDGET_MIN                stop after this many minutes (default 330)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from .common import ARCHIVE_LOG, CURRENT, USER_AGENT, append_csv, load_stores, read_csv, today

LOG_FIELDS = ["date", "store", "product_id", "url", "sale_since", "result", "job_id", "submitted_at"]
SAVE = "https://web.archive.org/save"


def bucket(url: str) -> int:
    return int(hashlib.md5(url.encode()).hexdigest(), 16) % 3


def todays_targets(day: str) -> list[dict]:
    b = dt.date.fromisoformat(day).toordinal() % 3
    done = {r["url"] for r in read_csv(ARCHIVE_LOG / f"{day}.csv") if r["result"] in ("submitted", "saved", "recent")}
    out = []
    for s in load_stores():
        for r in read_csv(CURRENT / f"{s['id']}.csv"):
            if r["on_sale"] != "1" or not r["url"] or r["url"] in done:
                continue
            # only products seen on sale in the last 2 days (skip ones that disappeared)
            if (dt.date.fromisoformat(day) - dt.date.fromisoformat(r["last_seen"])).days > 1:
                continue
            if bucket(r["url"]) != b:
                continue
            out.append({"store": s["id"], "product_id": r["product_id"], "url": r["url"], "sale_since": r["sale_since"]})
    out.sort(key=lambda r: r["sale_since"] or "9999")
    return out


class Saver:
    def __init__(self):
        self.key = os.environ.get("IA_ACCESS_KEY")
        self.secret = os.environ.get("IA_SECRET_KEY")
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        if self.key and self.secret:
            self.s.headers.update({"Accept": "application/json", "Authorization": f"LOW {self.key}:{self.secret}"})
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.pause_until = 0.0

    @property
    def authed(self) -> bool:
        return bool(self.key and self.secret)

    def _throttle(self):
        while True:
            with self.lock:
                wait = self.pause_until - time.time()
            if wait <= 0:
                return
            time.sleep(min(wait, 10))

    def _pause(self, seconds: float):
        with self.lock:
            self.pause_until = max(self.pause_until, time.time() + seconds)

    def save(self, url: str) -> tuple[str, str]:
        """Returns (result, job_id)."""
        last = "error"
        for attempt in range(4):
            if self.stop.is_set():
                return "skipped", ""
            self._throttle()
            try:
                if self.authed:
                    r = self.s.post(SAVE, data={"url": url, "if_not_archived_within": "3d", "skip_first_archive": "1"}, timeout=60)
                else:
                    r = self.s.get(f"{SAVE}/{url}", timeout=180)
            except requests.RequestException as e:
                time.sleep(15 * (attempt + 1))
                last = f"error:{type(e).__name__}"
                continue
            if r.status_code == 429:
                self._pause(60 * (attempt + 1))
                last = "rate-limited"
                continue
            if self.authed:
                try:
                    j = r.json()
                except ValueError:
                    last = f"http-{r.status_code}"
                    time.sleep(10)
                    continue
                if j.get("job_id"):
                    return "submitted", j["job_id"]
                msg = (j.get("message") or j.get("status_ext") or "").lower()
                if ("daily" in msg and "limit" in msg) or "per day" in msg:
                    self.stop.set()
                    return "daily-limit", ""
                if "active" in msg or "concurrent" in msg or "session" in msg:
                    self._pause(30)
                    last = "busy"
                    continue
                if "already" in msg or "within" in msg:
                    return "recent", ""
                return f"error:{msg[:60] or r.status_code}", ""
            # anonymous: success is a redirect/200 to the snapshot
            if r.status_code in (200, 301, 302):
                return "saved", ""
            if r.status_code in (520, 523, 403):
                return f"error:http-{r.status_code}", ""
            last = f"http-{r.status_code}"
            time.sleep(15)
        return last, ""


def main() -> int:
    day = today()
    targets = todays_targets(day)
    max_saves = int(os.environ.get("MAX_SAVES", "2500"))
    deadline = time.time() + 60 * float(os.environ.get("TIME_BUDGET_MIN", "330"))
    saver = Saver()
    workers = 6 if saver.authed else 3
    print(f"{day}: {len(targets)} pages due today; submitting up to {max_saves} ({'authenticated' if saver.authed else 'anonymous'}, {workers} workers)", flush=True)
    batch = targets[:max_saves]
    log_lock = threading.Lock()
    counts: dict[str, int] = {}

    def work(t):
        if time.time() > deadline:
            saver.stop.set()
        res, job = saver.save(t["url"])
        row = dict(t, date=day, result=res, job_id=job, submitted_at=dt.datetime.utcnow().isoformat(timespec="seconds"))
        with log_lock:
            append_csv(ARCHIVE_LOG / f"{day}.csv", [row], LOG_FIELDS)
            counts[res.split(":")[0]] = counts.get(res.split(":")[0], 0) + 1
            n = sum(counts.values())
            if n % 100 == 0:
                print(n, counts, flush=True)
        if saver.authed:
            time.sleep(2)  # keep a steady pace well under the concurrency cap

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, batch))
    print("done:", counts, f"remaining today: {len(targets) - sum(v for k, v in counts.items() if k in ('submitted', 'saved', 'recent'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
