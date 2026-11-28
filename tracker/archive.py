"""Ask the Wayback Machine to save discounted product pages, so each one is archived every 3 days.

Today's share = products currently on sale whose URL falls in today's bucket (hash(url) % 3).
Products discounted for the longest time go first. Several runs per day continue where the
previous one stopped (URLs already logged today are skipped).

When the Wayback Machine answers that crawling one store's site is paused, that store's pages are
set aside for a while and the run goes on with the other stores; when the pause is over they take
their place in the order again (see HostPauses and submit_all).

The archive can also accept a request and only then find the site paused: the request gets a job
number, and the capture fails a moment later. So the run asks about each accepted request about a
minute afterwards (Saver.outcome) and treats such a late failure as the same pause. Until a capture
of a paused site is known to have been saved, that site gets one page at a time.

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
from urllib.parse import urlparse

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


HOST_PAUSED = "host is paused"  # in the Wayback Machine's reply "crawling this host is paused because ..."
HOST_PAUSE_MIN = 15
OUTCOME_AFTER = 45  # seconds after a request is accepted before asking what became of it
OUTCOME_AGAIN = 30  # seconds before asking again while the capture is still running
OUTCOME_TRIES = 4


class HostPauses:
    """Store sites the Wayback Machine has stopped crawling for the moment.

    The reply "crawling this host is paused" is about one site, not about our account. Its pages
    are left alone for HOST_PAUSE_MIN minutes (not submitted, not logged, so they stay due) while
    the other stores' pages go on. After that the site is on trial: one page at a time is sent
    until the archive has saved one, so a site that is still paused costs one request, not a burst."""

    def __init__(self, minutes: float = HOST_PAUSE_MIN):
        self.seconds = 60 * minutes
        self.until: dict[str, float] = {}
        self.trial: set[str] = set()
        self.lock = threading.Lock()

    def pause(self, url: str) -> None:
        with self.lock:
            self.until[urlparse(url).netloc] = time.time() + self.seconds
            self.trial.add(urlparse(url).netloc)

    def accepted(self, url: str) -> None:
        """The archive saved a page of this site, so the site is no longer on trial."""
        with self.lock:
            self.trial.discard(urlparse(url).netloc)

    def on_trial(self, url: str) -> bool:
        with self.lock:
            return urlparse(url).netloc in self.trial

    def paused(self, url: str) -> bool:
        with self.lock:
            return self.until.get(urlparse(url).netloc, 0) > time.time()

    def seconds_left(self, urls) -> float:
        """How long until the first of these pages' sites may be tried again (0 if one already may)."""
        with self.lock:
            return max(0.0, min((self.until.get(urlparse(u).netloc, 0) for u in urls), default=0) - time.time())


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
        self._told: set[str] = set()

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
                if msg[:60] not in self._told:
                    # the log keeps 60 characters; the job log gets the whole answer once
                    self._told.add(msg[:60])
                    print(f"archive answered for {url}: {msg or r.status_code}", flush=True)
                return f"error:{msg[:60] or r.status_code}", ""
            # anonymous: success is a redirect/200 to the snapshot
            if r.status_code in (200, 301, 302):
                return "saved", ""
            if r.status_code in (520, 523, 403):
                return f"error:http-{r.status_code}", ""
            last = f"http-{r.status_code}"
            time.sleep(15)
        return last, ""


    def outcome(self, job_id: str) -> str:
        """What became of an accepted request: 'saved', 'paused' (the store's site was paused after
        all, so nothing was saved), 'failed' (any other reason) or 'pending' (not known yet)."""
        try:
            j = self.s.get(f"{SAVE}/status/{job_id}", timeout=30).json()
        except (requests.RequestException, ValueError):
            return "pending"
        status = str(j.get("status") or "").lower() if isinstance(j, dict) else ""
        if status == "success":
            return "saved" if str(j.get("http_status") or "200")[:1] in "23" else "failed"
        if status == "error":
            why = f"{j.get('status_ext') or ''} {j.get('message') or ''}".lower()
            return "paused" if HOST_PAUSED in why else "failed"
        return "pending"


def submit_all(batch: list[dict], saver, logfile, day: str, deadline: float, workers: int, hosts: HostPauses | None = None) -> dict[str, int]:
    """Submit the pages in order and log every answer; returns how many of each result.

    Each worker takes the first page in the order whose site is not paused. So a paused site's
    pages are passed over, and once the pause is over they are next again (first a single page,
    then the rest), without waiting for the other stores' pages to finish. Whatever is not logged
    stays due for the next run.

    If the saver can tell what became of an accepted request (Saver.outcome), the workers also ask
    about each one a little later. A capture that failed because the site was paused pauses the
    site here too, and a site on trial waits for the outcome of its one page before the next one
    is sent. The log is not touched by this: the row of an accepted request stays "submitted"."""
    hosts = hosts or HostPauses()
    ask = getattr(saver, "outcome", None) if saver.authed else None
    watch: list[list] = []  # [when to ask, times asked, page, is this its site's trial page]
    outcomes: dict[str, dict[str, int]] = {}  # store -> outcome -> how many, for the job log
    lock = threading.Lock()  # guards the log, the counters and the list of pages left
    counts: dict[str, int] = {}
    fails = [0]  # consecutive failures; a long streak means the archive stopped accepting us
    pending = list(batch)
    trying: set[str] = set()  # sites on trial that have their one page out right now

    def take():
        """The next page to submit, or None when every page left belongs to a paused site."""
        with lock:
            for i, t in enumerate(pending):
                if hosts.paused(t["url"]):
                    continue
                if hosts.on_trial(t["url"]):
                    host = urlparse(t["url"]).netloc
                    if host in trying:
                        continue
                    trying.add(host)
                return pending.pop(i)
        return None

    def check_outcomes():
        """Ask about the accepted requests whose time has come (a few at most, then back to submitting)."""
        for _ in range(3):
            with lock:
                due = next((w for w in watch if w[0] <= time.time()), None)
                if due is None:
                    return
                watch.remove(due)
            _, asked, t, trial = due
            res = ask(t["job_id"])
            with lock:
                if res == "pending" and asked + 1 < OUTCOME_TRIES:
                    watch.append([time.time() + OUTCOME_AGAIN, asked + 1, t, trial])
                    continue
                by = outcomes.setdefault(t["store"], {})
                by[res] = by.get(res, 0) + 1
                if res == "paused":
                    if not hosts.paused(t["url"]):
                        print(f"{urlparse(t['url']).netloc}: a capture failed because crawling is paused; its pages wait {hosts.seconds / 60:.0f} minutes", flush=True)
                    hosts.pause(t["url"])
                elif res == "saved":
                    hosts.accepted(t["url"])
                if trial:
                    trying.discard(urlparse(t["url"]).netloc)

    def submit(t) -> bool:
        """Returns True when the page is its site's trial page and its outcome is still awaited."""
        res, job = saver.save(t["url"])
        if res == "skipped":
            return False  # not logged, so the page stays due for the next run
        held = False
        row = dict(t, date=day, result=res, job_id=job, submitted_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"))
        with lock:
            append_csv(logfile, [row], LOG_FIELDS)
            counts[res.split(":")[0]] = counts.get(res.split(":")[0], 0) + 1
            if HOST_PAUSED in res:
                # one site's problem: set the site aside, and do not count it towards giving up
                if not hosts.paused(t["url"]):
                    print(f"{urlparse(t['url']).netloc}: crawling paused by the Wayback Machine; its pages wait {hosts.seconds / 60:.0f} minutes", flush=True)
                hosts.pause(t["url"])
            else:
                fails[0] = 0 if res in ("submitted", "saved", "recent") else fails[0] + 1
                if ask and job:
                    # accepted is not saved yet: ask later, and keep a site on trial waiting for the answer
                    held = urlparse(t["url"]).netloc in trying
                    watch.append([time.time() + OUTCOME_AFTER, 0, dict(t, job_id=job), held])
                elif fails[0] == 0 and not ask:
                    hosts.accepted(t["url"])  # without outcomes to ask about, accepted is the best we know
            if fails[0] >= 25 and not saver.stop.is_set():
                print(f"stopping: {fails[0]} failures in a row (last: {res}); the rest stays due for the next run", flush=True)
                saver.stop.set()
            n = sum(counts.values())
            if n % 100 == 0:
                print(n, counts, flush=True)
        if saver.authed:
            time.sleep(1)
        return held

    def worker():
        while not saver.stop.is_set():
            if time.time() > deadline:
                saver.stop.set()
                return
            if ask:
                check_outcomes()
            t = take()
            if t is not None:
                held = False
                try:
                    held = submit(t)
                finally:
                    if not held:
                        with lock:
                            trying.discard(urlparse(t["url"]).netloc)
                continue
            with lock:
                left = [p["url"] for p in pending]
            if not left:
                return
            wait = hosts.seconds_left(left)
            if time.time() + wait > deadline:
                return  # the paused sites' pages stay due for the next run
            # 0 = a site on trial has its one page out right now; with outcomes to ask about, wake sooner
            time.sleep(min(wait, 10 if watch else 30) or 2)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for f in [ex.submit(worker) for _ in range(workers)]:
            f.result()
    if pending and not saver.stop.is_set():
        print(f"{len(pending)} pages of paused sites were not submitted; they stay due", flush=True)
    if outcomes:
        print("what became of the accepted requests we asked about (the rest were not asked before the run ended):", flush=True)
        for store, by in outcomes.items():
            print(f"  {store}: " + ", ".join(f"{k} {v}" for k, v in sorted(by.items())), flush=True)
    return counts


def main() -> int:
    day = today()
    targets = todays_targets(day)
    max_saves = int(os.environ.get("MAX_SAVES", "2500"))
    deadline = time.time() + 60 * float(os.environ.get("TIME_BUDGET_MIN", "330"))
    saver = Saver()
    workers = 6 if saver.authed else 3
    print(f"{day}: {len(targets)} pages due today; submitting up to {max_saves} ({'authenticated' if saver.authed else 'anonymous'}, {workers} workers)", flush=True)
    counts = submit_all(targets[:max_saves], saver, log_path(day), day, deadline, workers)
    print("done:", counts, f"remaining today: {len(targets) - sum(v for k, v in counts.items() if k in ('submitted', 'saved', 'recent'))}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--merge-log":
        print("archive log: merged", merge_logs(sys.argv[2]), "new rows", flush=True)
        sys.exit(0)
    sys.exit(main())
