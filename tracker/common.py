"""Shared helpers: paths, HTTP session, robots.txt check, CSV I/O."""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import time
import urllib.robotparser
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CURRENT = DATA / "current"
HISTORY = DATA / "history"
RUNS = DATA / "runs"
ARCHIVE_LOG = DATA / "archive"

USER_AGENT = (
    "israel-price-tracker/1.0 (+https://github.com/poopkinel/israel-price-tracker; "
    "research on long-running discounts; one request per second per site)"
)

CURRENT_FIELDS = [
    "product_id", "title", "url", "price", "compare_at", "on_sale", "discount_pct",
    "first_seen", "last_seen", "sale_since", "last_full_price_seen",
]
HISTORY_FIELDS = ["date", "product_id", "price", "compare_at"]


def today() -> str:
    """Date in Israel time, so a run just after midnight UTC counts for the right day."""
    il = dt.timezone(dt.timedelta(hours=int(os.environ.get("TZ_OFFSET_HOURS", "2"))))
    return dt.datetime.now(il).date().isoformat()


def load_stores() -> list[dict]:
    with open(ROOT / "stores.json", encoding="utf-8") as f:
        return json.load(f)


class Http:
    """requests.Session with a polite per-host delay and robots.txt checks."""

    def __init__(self, delay: float = 1.0, timeout: float = 30.0):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "he-IL,he;q=0.9,en;q=0.8"})
        self.delay = delay
        self.timeout = timeout
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        host = urlparse(url).scheme + "://" + urlparse(url).netloc
        if host not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.s.get(host + "/robots.txt", timeout=self.timeout)
                if r.status_code >= 400:
                    rp = None  # no robots.txt -> everything allowed
                else:
                    rp.parse(r.text.splitlines())
            except requests.RequestException:
                rp = None
            self._robots[host] = rp
        rp = self._robots[host]
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    def _wait(self, url: str) -> None:
        host = urlparse(url).netloc
        gap = time.monotonic() - self._last.get(host, 0)
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last[host] = time.monotonic()

    def request(self, method: str, url: str, retries: int = 3, **kw) -> requests.Response:
        kw.setdefault("timeout", self.timeout)
        for attempt in range(retries):
            self._wait(url)
            try:
                r = self.s.request(method, url, **kw)
            except requests.RequestException:
                if attempt == retries - 1:
                    raise
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code in (429, 502, 503, 504) and attempt < retries - 1:
                time.sleep(int(r.headers.get("Retry-After", 10 * (attempt + 1))))
                continue
            return r
        return r  # pragma: no cover

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def append_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)
