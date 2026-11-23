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
import shutil
from pathlib import Path
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from .common import ARCHIVE_LOG, CURRENT, USER_AGENT, append_csv, load_stores, read_csv, today

def log_path(day: str):
    """Where this run appends its log. ARCHIVE_LOG_DIR (outside the repo) lets the workflow copy and
    commit the log every few minutes without racing the writer; it starts from the repo's copy."""
    d = os.environ.get("ARCHIVE_LOG_DIR")
    if not d:
        return ARCHIVE_LOG / f"{day}.csv"
    p = Path(d) / f"{day}.csv"
    src = ARCHIVE_LOG / f"{day}.csv"
    if not p.exists() and src.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, p)
    return p


LOG_FIELDS = ["date", "store", "product_id", "url", "sale_since", "result", "job_id", "submitted_at"]


def merge_logs(run_dir, repo_dir=ARCHIVE_LOG) -> int:
    """Append to the repo's logs the rows of this run's logs that are not there yet; returns how many.

    The repo's copy can hold rows this run never saw (another run pushed them after this run's
    checkout), so the run's file must never simply replace it. Existing rows are left untouched."""
    added = 0
    for src in sorted(Path(run_dir).glob("*.csv")):
        dst = Path(repo_dir) / src.name
        have = {tuple(r.get(f, "") for f in LOG_FIELDS) for r in read_csv(dst)}
        new = []
        for r in read_csv(src):
            k = tuple(r.get(f, "") for f in LOG_FIELDS)
            if k not in have:
                have.add(k)
                new.append(r)
        append_csv(dst, new, LOG_FIELDS)
        added += len(new)
    return added
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
        self._status_at = 0.0
        self._available = 0

    @property
    def authed(self) -> bool:
        return bool(self.key and self.secret)

    def _wait_capacity(self, max_wait: float = 900) -> None:
        """Authenticated only: wait until the account has a free capture slot (SPN2 /save/status/user)."""
        t_end = time.time() + max_wait
        while not self.stop.is_set() and time.time() < t_end:
            with self.lock:
                if time.time() - self._status_at > 3:
                    try:
                        j = self.s.get(SAVE + "/status/user", timeout=30).json()
                        self._available = int(j.get("available", 1))
                        if j.get("daily_captures_limit") and j.get("daily_captures", 0) >= j["daily_captures_limit"]:
                            self.stop.set()
                    except (requests.RequestException, ValueError, TypeError):
                        self._available = 1  # status endpoint unavailable: fall back to trying
                    self._status_at = time.time()
                if self._available > 0:
                    self._available -= 1
                    return
            time.sleep(4)

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
                    self._wait_capacity()
                    if self.stop.is_set():
                        return "skipped", ""
                    r = self.s.post(SAVE, data={"url": url, "if_not_archived_within": "2d", "skip_first_archive": "1"}, timeout=60)
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
                if "already" in msg or "within" in msg or "same snapshot" in msg:
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
    logfile = log_path(day)
    log_lock = threading.Lock()
    counts: dict[str, int] = {}
    fails = [0]  # consecutive failures; a long streak means the archive stopped accepting us

    def work(t):
        if time.time() > deadline:
            saver.stop.set()
        if saver.stop.is_set():
            return  # not logged, so the page stays due for the next run
        res, job = saver.save(t["url"])
        if res == "skipped":
            return
        row = dict(t, date=day, result=res, job_id=job, submitted_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"))
        with log_lock:
            append_csv(logfile, [row], LOG_FIELDS)
            counts[res.split(":")[0]] = counts.get(res.split(":")[0], 0) + 1
            fails[0] = 0 if res in ("submitted", "saved", "recent") else fails[0] + 1
            if fails[0] >= 25 and not saver.stop.is_set():
                print(f"stopping: {fails[0]} failures in a row (last: {res}); the rest stays due for the next run", flush=True)
                saver.stop.set()
            n = sum(counts.values())
            if n % 100 == 0:
                print(n, counts, flush=True)
        if saver.authed:
            time.sleep(1)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, batch))
    print("done:", counts, f"remaining today: {len(targets) - sum(v for k, v in counts.items() if k in ('submitted', 'saved', 'recent'))}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--merge-log":
        print("archive log: merged", merge_logs(sys.argv[2]), "new rows", flush=True)
        sys.exit(0)
    sys.exit(main())
