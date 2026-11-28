"""Offline tests with fake store responses. Run: python -m unittest discover tests"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tracker import archive, collect, common, report


class FakeResp:
    def __init__(self, data, status=200, headers=None):
        self._d, self.status_code, self.headers = data, status, headers or {}

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes

    def allowed(self, url):
        return True

    def get(self, url, **kw):
        for k, v in self.routes.items():
            if k in url:
                return v(url) if callable(v) else v
        return FakeResp([], 404)

    def post(self, url, json=None, **kw):
        return self.routes["graphql"](json["query"])


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name in ("CURRENT", "HISTORY", "RUNS", "ARCHIVE_LOG"):
            p = self.tmp / name.lower()
            for mod in (common, collect, archive, report):
                if hasattr(mod, name):
                    mock.patch.object(mod, name, p).start()
        mock.patch.object(report, "ROOT", self.tmp).start()

    def tearDown(self):
        mock.patch.stopall()

    def test_shopify_fetch(self):
        page1 = {"products": [
            {"id": 1, "title": "A", "handle": "a", "variants": [{"price": "100.00", "compare_at_price": "200.00"}, {"price": "120.00", "compare_at_price": None}]},
            {"id": 2, "title": "B", "handle": "b", "variants": [{"price": "50.00", "compare_at_price": None}]}]}
        http = FakeHttp({"page=1": FakeResp(page1), "page=2": FakeResp({"products": []})})
        out = collect.fetch_shopify(http, "https://x.co.il")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0], {"product_id": "1", "title": "A", "url": "https://x.co.il/products/a", "price": 100.0, "compare_at": 200.0})
        self.assertIsNone(out[1]["compare_at"])

    def test_woocommerce_fetch(self):
        p = [{"id": 7, "name": "W", "permalink": "https://w/p/7", "prices": {"price": "8990", "regular_price": "12990", "currency_minor_unit": 2}}]
        http = FakeHttp({"page=1": FakeResp(p, headers={"X-WP-TotalPages": "1"})})
        out = collect.fetch_woocommerce(http, "https://w")
        self.assertEqual(out[0]["price"], 89.9)
        self.assertEqual(out[0]["compare_at"], 129.9)

    def test_magento_fetch_with_suffix_fallback(self):
        calls = []

        def gql(q):
            calls.append(q)
            if "url_suffix" in q:
                return FakeResp({"errors": [{"message": "Cannot query field url_suffix"}]})
            return FakeResp({"data": {"products": {"page_info": {"total_pages": 1}, "items": [
                {"sku": "S1", "name": "M", "url_key": "m",
                 "price_range": {"minimum_price": {"regular_price": {"value": 670}, "final_price": {"value": 339}}}}]}}})
        out = collect.fetch_magento(FakeHttp({"graphql": gql}), "https://m")
        self.assertEqual(out[0]["url"], "https://m/m.html")
        self.assertEqual((out[0]["price"], out[0]["compare_at"]), (339, 670))
        self.assertEqual(len(calls), 2)

    def test_sale_streak_and_history(self):
        p = lambda price, cmp_: [{"product_id": "1", "title": "A", "url": "https://x/a", "price": price, "compare_at": cmp_}]
        collect.merge("s", p(100, 200), "2026-01-01")
        collect.merge("s", p(100, 200), "2026-01-02")   # same -> no history row
        collect.merge("s", p(90, 200), "2026-01-05")    # price change, still on sale
        cur = common.read_csv(collect.CURRENT / "s.csv")[0]
        self.assertEqual(cur["sale_since"], "2026-01-01")
        self.assertEqual(cur["last_full_price_seen"], "")
        self.assertEqual(cur["discount_pct"], "55")
        hist = common.read_csv(collect.HISTORY / "s.csv")
        self.assertEqual([h["date"] for h in hist], ["2026-01-01", "2026-01-05"])
        collect.merge("s", p(200, 200), "2026-01-06")   # back to full price
        collect.merge("s", p(150, 200), "2026-01-07")   # new sale -> streak restarts
        cur = common.read_csv(collect.CURRENT / "s.csv")[0]
        self.assertEqual(cur["sale_since"], "2026-01-07")
        self.assertEqual(cur["last_full_price_seen"], "2026-01-06")

    def test_missing_product_keeps_state(self):
        collect.merge("s", [{"product_id": "1", "title": "A", "url": "u", "price": 1, "compare_at": 2}], "2026-01-01")
        collect.merge("s", [{"product_id": "2", "title": "B", "url": "v", "price": 1, "compare_at": None}], "2026-01-02")
        rows = {r["product_id"]: r for r in common.read_csv(collect.CURRENT / "s.csv")}
        self.assertEqual(rows["1"]["last_seen"], "2026-01-01")
        self.assertEqual(rows["1"]["sale_since"], "2026-01-01")

    def test_archive_buckets_cover_every_product_in_3_days(self):
        prods = [{"product_id": str(i), "title": "", "url": f"https://x/p{i}", "price": 10, "compare_at": 20} for i in range(300)]
        stores = [{"id": "s", "name": "S", "category": "c", "platform": "shopify", "base": "https://x"}]
        with mock.patch.object(archive, "load_stores", return_value=stores):
            seen = []
            for d in ("2026-01-01", "2026-01-02", "2026-01-03"):
                collect.merge("s", prods, d)
                seen += [t["url"] for t in archive.todays_targets(d)]
        self.assertEqual(sorted(seen), sorted(p["url"] for p in prods))  # each exactly once
        self.assertEqual(len(seen), len(set(seen)))

    def test_archive_skips_done_today(self):
        stores = [{"id": "s", "name": "S", "category": "c", "platform": "shopify", "base": "https://x"}]
        prods = [{"product_id": str(i), "title": "", "url": f"https://x/p{i}", "price": 10, "compare_at": 20} for i in range(30)]
        collect.merge("s", prods, "2026-01-01")
        with mock.patch.object(archive, "load_stores", return_value=stores):
            t = archive.todays_targets("2026-01-01")
            common.append_csv(archive.ARCHIVE_LOG / "2026-01-01.csv", [dict(t[0], date="2026-01-01", result="submitted", job_id="j", submitted_at="")], archive.LOG_FIELDS)
            t2 = archive.todays_targets("2026-01-01")
        self.assertEqual(len(t2), len(t) - 1)

    def test_report_runs(self):
        stores = [{"id": "s", "name": "S", "category": "c", "platform": "shopify", "base": "https://x", "note": "n"}]
        collect.merge("s", [{"product_id": "1", "title": "A", "url": "u", "price": 1, "compare_at": 2}], common.today())
        collect.RUNS.mkdir(parents=True, exist_ok=True)
        (collect.RUNS / f"{common.today()}.json").write_text(json.dumps([{"id": "s", "status": "ok"}]))
        with mock.patch.object(report, "load_stores", return_value=stores):
            report.main()
        text = (self.tmp / "REPORT.md").read_text(encoding="utf-8")
        self.assertIn("| S | c | ✅ | 1 | 1 | 100% | 1 | 0 | 0 | 0 | 0 |", text)  # on sale since the first day


class TodaysTargetsTest(unittest.TestCase):
    """Which pages are due for archiving today, and in what order."""

    DAY = "2026-11-24"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.current, self.log = self.tmp / "current", self.tmp / "archive"
        mock.patch.object(archive, "CURRENT", self.current).start()
        mock.patch.object(archive, "ARCHIVE_LOG", self.log).start()
        mock.patch.object(archive, "load_stores", lambda: [{"id": "a"}, {"id": "b"}]).start()
        self.addCleanup(mock.patch.stopall)
        import datetime as dt
        self.today_bucket = dt.date.fromisoformat(self.DAY).toordinal() % 3

    def url(self, store, n, due=True):
        """A product URL whose bucket is (or is not) today's."""
        i = 0
        while True:
            u = f"https://{store}.co.il/p/{n}-{i}"
            if (archive.bucket(u) == self.today_bucket) == due:
                return u
            i += 1

    def row(self, pid, url, on_sale="1", last_seen=None, sale_since="2026-11-01"):
        return {"product_id": pid, "title": "t", "url": url, "price": "80.00", "compare_at": "100.00",
                "on_sale": on_sale, "discount_pct": "20", "first_seen": "2026-10-27",
                "last_seen": last_seen or self.DAY, "sale_since": sale_since if on_sale == "1" else "",
                "last_full_price_seen": ""}

    def write(self, store, rows):
        common.write_csv(self.current / f"{store}.csv", rows, common.CURRENT_FIELDS)

    def test_only_on_sale_products_in_todays_bucket(self):
        due, other_day, full_price = self.url("a", 1), self.url("a", 2, due=False), self.url("a", 3)
        self.write("a", [self.row("1", due), self.row("2", other_day), self.row("3", full_price, on_sale="0"),
                         self.row("4", "")])
        out = archive.todays_targets(self.DAY)
        self.assertEqual(out, [{"store": "a", "product_id": "1", "url": due, "sale_since": "2026-11-01"}])

    def test_every_url_is_due_exactly_one_day_in_three(self):
        urls = [f"https://a.co.il/p/{i}" for i in range(60)]
        seen = []
        for day in ("2026-11-24", "2026-11-25", "2026-11-26"):
            self.write("a", [self.row(str(i), u, last_seen=day) for i, u in enumerate(urls)])
            seen += [t["url"] for t in archive.todays_targets(day)]
        self.assertEqual(sorted(seen), sorted(urls))

    def test_products_not_seen_for_two_days_are_skipped(self):
        yesterday, stale = self.url("a", 1), self.url("a", 2)
        self.write("a", [self.row("1", yesterday, last_seen="2026-11-23"), self.row("2", stale, last_seen="2026-11-22")])
        self.assertEqual([t["product_id"] for t in archive.todays_targets(self.DAY)], ["1"])

    def test_longest_discounts_first_across_stores(self):
        self.write("a", [self.row("1", self.url("a", 1), sale_since="2026-11-20"),
                         self.row("2", self.url("a", 2), sale_since="2026-10-27")])
        self.write("b", [self.row("3", self.url("b", 3), sale_since="2026-11-05")])
        self.assertEqual([t["product_id"] for t in archive.todays_targets(self.DAY)], ["2", "3", "1"])

    def test_pages_already_handled_today_are_skipped_but_errors_stay_due(self):
        urls = {k: self.url("a", k) for k in ("submitted", "recent", "saved", "error:paused", "rate-limited")}
        self.write("a", [self.row(str(i), u) for i, u in enumerate(urls.values())])
        common.append_csv(self.log / f"{self.DAY}.csv",
                          [{"date": self.DAY, "store": "a", "product_id": str(i), "url": u, "sale_since": "2026-11-01",
                            "result": res, "job_id": "", "submitted_at": ""} for i, (res, u) in enumerate(urls.items())],
                          archive.LOG_FIELDS)
        # yesterday's log must not count for today
        common.append_csv(self.log / "2026-11-23.csv",
                          [{"date": "2026-11-23", "store": "a", "product_id": "3", "url": urls["error:paused"],
                            "sale_since": "2026-11-01", "result": "submitted", "job_id": "", "submitted_at": ""}],
                          archive.LOG_FIELDS)
        left = archive.todays_targets(self.DAY)
        self.assertEqual(sorted(t["url"] for t in left), sorted([urls["error:paused"], urls["rate-limited"]]))


class MergeLogsTest(unittest.TestCase):
    def row(self, url, at):
        return {"date": "2026-11-22", "store": "fox", "product_id": "1", "url": url, "sale_since": "2026-11-01",
                "result": "submitted", "job_id": "j" + at, "submitted_at": at}

    def test_keeps_rows_pushed_by_another_run_and_adds_only_new_ones(self):
        with tempfile.TemporaryDirectory() as d:
            repo, run = Path(d) / "repo", Path(d) / "run"
            a, b, c = self.row("u/a", "t1"), self.row("u/b", "t2"), self.row("u/c", "t3")
            # the run started from a checkout that had only row a; meanwhile another run pushed b
            common.append_csv(repo / "2026-11-22.csv", [a, b], archive.LOG_FIELDS)
            common.append_csv(run / "2026-11-22.csv", [a, c], archive.LOG_FIELDS)
            before = (repo / "2026-11-22.csv").read_bytes()
            self.assertEqual(archive.merge_logs(run, repo), 1)
            after = (repo / "2026-11-22.csv").read_bytes()
            self.assertTrue(after.startswith(before))  # existing rows untouched
            self.assertEqual([r["url"] for r in common.read_csv(repo / "2026-11-22.csv")], ["u/a", "u/b", "u/c"])
            self.assertEqual(archive.merge_logs(run, repo), 0)  # repeating the sync adds nothing
            self.assertEqual((repo / "2026-11-22.csv").read_bytes(), after)

    def test_creates_the_log_when_the_repo_has_none(self):
        with tempfile.TemporaryDirectory() as d:
            repo, run = Path(d) / "repo", Path(d) / "run"
            common.append_csv(run / "2026-11-23.csv", [self.row("u/a", "t1")], archive.LOG_FIELDS)
            self.assertEqual(archive.merge_logs(run, repo), 1)
            self.assertEqual(len(common.read_csv(repo / "2026-11-23.csv")), 1)


class SaverTest(unittest.TestCase):
    def test_authed_responses(self):
        with mock.patch.dict("os.environ", {"IA_ACCESS_KEY": "a", "IA_SECRET_KEY": "b"}):
            s = archive.Saver()
        seq = [FakeResp({"message": "You have already reached the limit of active sessions"}),
               FakeResp({"url": "u", "job_id": "spn2-1"})]
        with mock.patch.object(s.s, "post", side_effect=seq), mock.patch.object(archive.time, "sleep"), \
             mock.patch.object(s, "_throttle"):
            self.assertEqual(s.save("https://x/a"), ("submitted", "spn2-1"))
        with mock.patch.object(s.s, "post", return_value=FakeResp({"message": "You cannot make more than 100000 captures per day"})), \
             mock.patch.object(s, "_throttle"):
            self.assertEqual(s.save("https://x/b"), ("daily-limit", ""))
        self.assertTrue(s.stop.is_set())
        s.stop.clear()
        msg = {"message": "The same snapshot had been made 69 hours, 47 minutes ago. You can make new capture of this URL after 72 hours."}
        with mock.patch.object(s.s, "post", return_value=FakeResp(msg)), mock.patch.object(s, "_throttle"):
            self.assertEqual(s.save("https://x/c"), ("recent", ""))


    def test_full_error_answer_goes_to_the_job_log_once(self):
        with mock.patch.dict("os.environ", {"IA_ACCESS_KEY": "a", "IA_SECRET_KEY": "b"}):
            s = archive.Saver()
        text = "Please try again in ~1 min. Crawling this host is paused because of a long reason that the log cuts off."
        with mock.patch.object(s.s, "post", return_value=FakeResp({"message": text})), mock.patch.object(s, "_throttle"), \
                mock.patch.object(s, "_wait_capacity"), mock.patch("builtins.print") as out:
            first = s.save("https://x/a")
            self.assertEqual(s.save("https://x/b"), first)
        self.assertEqual(first, ("error:" + text.lower()[:60], ""))
        self.assertEqual(out.call_count, 1)
        self.assertIn("cuts off", out.call_args[0][0])


class SaverCapacityTest(unittest.TestCase):
    def test_waits_for_free_slot_and_stops_at_daily_limit(self):
        with mock.patch.dict("os.environ", {"IA_ACCESS_KEY": "a", "IA_SECRET_KEY": "b"}):
            s = archive.Saver()
        statuses = [FakeResp({"available": 0}), FakeResp({"available": 2, "daily_captures": 5, "daily_captures_limit": 100000})]
        with mock.patch.object(s.s, "get", side_effect=statuses), mock.patch.object(archive.time, "sleep"), \
             mock.patch.object(archive.time, "time", side_effect=__import__("itertools").count(0, 5)):
            s._wait_capacity()
        self.assertFalse(s.stop.is_set())
        s._status_at = 0
        with mock.patch.object(s.s, "get", return_value=FakeResp({"available": 3, "daily_captures": 100000, "daily_captures_limit": 100000})):
            s._wait_capacity()
        self.assertTrue(s.stop.is_set())

    def test_log_path_starts_from_repo_copy(self):
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(archive, "ARCHIVE_LOG", tmp / "repo"):
            common.append_csv(tmp / "repo" / "2026-01-01.csv", [{"date": "2026-01-01", "url": "u", "result": "submitted"}], archive.LOG_FIELDS)
            with mock.patch.dict("os.environ", {"ARCHIVE_LOG_DIR": str(tmp / "run")}):
                p = archive.log_path("2026-01-01")
        self.assertEqual(p, tmp / "run" / "2026-01-01.csv")
        self.assertEqual(len(common.read_csv(p)), 1)


class FakeSaver:
    """Answers like archive.Saver.save, from a function of (url, how many times it was asked)."""
    authed = False

    def __init__(self, answer):
        self.stop = archive.threading.Event()
        self.answer, self.asked = answer, []

    def save(self, url):
        self.asked.append(url)
        return self.answer(url, self.asked.count(url))


PAUSED = "error:please try again in ~1 min. crawling this host is paused bec"


class PausedHostTest(unittest.TestCase):
    """The Wayback Machine sometimes stops crawling one store's site; the run must go on with the others."""

    def setUp(self):
        self.log = Path(tempfile.mkdtemp()) / "2026-01-01.csv"
        self.batch = [{"store": "a", "product_id": str(i), "url": f"https://a.co.il/p{i}", "sale_since": "2025-12-01"} for i in range(40)]
        self.batch += [{"store": "b", "product_id": str(i), "url": f"https://b.co.il/p{i}", "sale_since": "2025-12-02"} for i in range(5)]

    def run_batch(self, saver, deadline=float("inf")):
        with mock.patch.object(archive.time, "sleep"):
            return archive.submit_all(self.batch, saver, self.log, "2026-01-01", deadline, workers=1)

    def test_other_stores_go_on_while_one_site_is_paused(self):
        saver = FakeSaver(lambda url, n: (PAUSED, "") if "a.co.il" in url else ("submitted", "j"))
        counts = self.run_batch(saver, deadline=archive.time.time() + 60)
        self.assertFalse(saver.stop.is_set())  # 40 pages of a paused site did not end the run
        self.assertEqual(counts, {"error": 1, "submitted": 5})
        rows = common.read_csv(self.log)
        self.assertEqual([r["store"] for r in rows], ["a"] + ["b"] * 5)  # the paused site's other pages are not logged
        self.assertEqual(len(saver.asked), 6)  # and were not sent to the Wayback Machine either

    def run_with_clock(self, saver, seconds_per_page=20, minutes=float("inf")):
        """Runs the batch on a fake clock: every request takes seconds_per_page, sleeping moves the clock."""
        now = [1000.0]
        answer = saver.answer

        def timed(url, n):
            now[0] += seconds_per_page
            return answer(url, n)

        saver.answer = timed
        with mock.patch.object(archive.time, "time", lambda: now[0]), \
                mock.patch.object(archive.time, "sleep", lambda s: now.__setitem__(0, now[0] + s)):
            return archive.submit_all(self.batch, saver, self.log, "2026-01-01", 1000.0 + 60 * minutes, workers=1)

    def test_paused_pages_are_tried_again_after_the_pause(self):
        saver = FakeSaver(lambda url, n: (PAUSED, "") if url.endswith("a.co.il/p0") and n == 1 else ("submitted", "j"))
        counts = self.run_with_clock(saver)
        self.assertEqual(counts, {"error": 1, "submitted": 44})
        rows = common.read_csv(self.log)
        self.assertEqual(sum(r["result"] == "submitted" for r in rows), 44)
        # the page that got the error stays due: todays_targets only skips submitted pages
        self.assertNotIn("https://a.co.il/p0", {r["url"] for r in rows if r["result"] == "submitted"})

    def test_paused_site_rejoins_before_the_other_stores_finish(self):
        # 300 pages of store b take 100 minutes; store a's pause lasts 15
        self.batch += [{"store": "b", "product_id": f"x{i}", "url": f"https://b.co.il/x{i}", "sale_since": "2025-12-03"} for i in range(300)]
        saver = FakeSaver(lambda url, n: (PAUSED, "") if url.endswith("a.co.il/p0") else ("submitted", "j"))
        self.run_with_clock(saver)
        stores = [r["store"] for r in common.read_csv(self.log) if r["result"] == "submitted"]
        self.assertEqual(stores.count("a"), 39)
        last_a = max(i for i, s in enumerate(stores) if s == "a")
        self.assertLess(last_a, 150)  # not after all 305 pages of b, as before

    def test_site_still_paused_costs_one_request_per_retry(self):
        self.batch += [{"store": "b", "product_id": f"x{i}", "url": f"https://b.co.il/x{i}", "sale_since": "2025-12-03"} for i in range(300)]
        saver = FakeSaver(lambda url, n: (PAUSED, "") if "a.co.il" in url else ("submitted", "j"))
        counts = self.run_with_clock(saver, minutes=110)
        asked_a = [u for u in saver.asked if "a.co.il" in u]
        self.assertEqual(counts["submitted"], 305)
        self.assertEqual(len(asked_a), len(set(asked_a)))  # an error is not repeated for the same page in this run
        self.assertGreaterEqual(len(asked_a), 4)  # tried again about every 15 minutes over 100 minutes
        self.assertLessEqual(len(asked_a), 9)  # one page each time, not the whole store

    def test_run_still_stops_when_every_answer_is_an_error(self):
        saver = FakeSaver(lambda url, n: ("error:http-502", ""))
        counts = self.run_batch(saver)
        self.assertTrue(saver.stop.is_set())
        self.assertEqual(counts, {"error": 25})

    def test_host_pause_expires(self):
        hosts = archive.HostPauses(minutes=15)
        with mock.patch.object(archive.time, "time", return_value=1000.0):
            hosts.pause("https://a.co.il/p1")
            self.assertTrue(hosts.paused("https://a.co.il/p2"))
            self.assertFalse(hosts.paused("https://b.co.il/p1"))
            self.assertEqual(hosts.seconds_left(["https://a.co.il/p3"]), 900.0)
        with mock.patch.object(archive.time, "time", return_value=1901.0):
            self.assertFalse(hosts.paused("https://a.co.il/p2"))
            self.assertEqual(hosts.seconds_left(["https://a.co.il/p3"]), 0.0)


class SnapshotCheckTest(unittest.TestCase):
    """checks/snapshots.py: the parts that need no network."""

    def test_price_is_found_however_it_is_written(self):
        from checks import snapshots
        for html in ("<s>4,258.00 ₪</s>", "was 4258 nis", '"compare_at_price":425800,', "<b>4258.00</b>"):
            self.assertTrue(snapshots.find_number(html, "4258.00")[0], html)
        self.assertTrue(snapshots.find_number("now 29.9!", "29.90")[0])

    def test_price_inside_a_longer_number_is_not_a_match(self):
        from checks import snapshots
        for html in ("sku 142580", "14258", "4258.50", "4,258,000", "id=9425800", "b7b8-4258-9cb5", "max-width: 4258px"):
            self.assertEqual(snapshots.find_number(html, "4258.00")[0], "", html)

    def test_number_next_to_a_currency_sign_is_preferred_and_a_bare_one_is_marked(self):
        from checks import snapshots
        html = "font-weight:300;" + " " * 80 + "<del>&#8362;300</del>" + " " * 80 + "<b>99 ₪</b>"
        ctx, strong = snapshots.find_number(html, "300.00")
        self.assertTrue(strong)
        self.assertIn("&#8362;300", ctx)
        self.assertEqual(snapshots.verdict(html, "99.00", "300.00")[0], "both")
        self.assertEqual(snapshots.verdict("font-weight:300;" + " " * 80 + "<b>99 ₪</b>", "99.00", "300.00")[0], "both?")

    def test_verdict_names_which_prices_are_on_the_page(self):
        from checks import snapshots
        self.assertEqual(snapshots.verdict("<s>64.90 ₪</s> <b>19.90 ₪</b>", "19.90", "64.90")[0], "both")
        self.assertEqual(snapshots.verdict("<b>19.90</b>", "19.90", "64.90")[0], "price")
        self.assertEqual(snapshots.verdict("<s>₪64.90</s>", "19.90", "64.90")[0], "before")
        self.assertEqual(snapshots.verdict("<div id=app></div>", "19.90", "64.90")[0], "neither")

    def test_price_on_a_day_is_the_last_change_up_to_that_day(self):
        from checks import snapshots
        hist = [{"date": "2026-10-27", "product_id": "a", "price": "10.00", "compare_at": "20.00"},
                {"date": "2026-11-05", "product_id": "b", "price": "1.00", "compare_at": ""},
                {"date": "2026-11-10", "product_id": "a", "price": "12.00", "compare_at": "20.00"}]
        self.assertEqual(snapshots.price_on(hist, "a", "2026-11-09"), ("10.00", "20.00"))
        self.assertEqual(snapshots.price_on(hist, "a", "2026-11-10"), ("12.00", "20.00"))
        self.assertEqual(snapshots.price_on(hist, "a", "2026-10-26"), ("", ""))

    def test_sample_is_long_discounts_with_a_submitted_page(self):
        from checks import snapshots
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            row = dict(title="", price="5.00", compare_at="9.00", on_sale="1", discount_pct="44",
                       first_seen="2026-10-27", last_seen="2026-11-25", sale_since="2026-10-27", last_full_price_seen="")
            cur = [dict(row, product_id="1", url="https://s/1"),
                   dict(row, product_id="2", url="https://s/2", sale_since="2026-11-16"),   # restarted
                   dict(row, product_id="3", url="https://s/3"),                             # never submitted
                   dict(row, product_id="4", url="https://s/4", last_seen="2026-11-09"),     # gone
                   dict(row, product_id="5", url="https://s/5")]
            common.write_csv(d / "cur" / "s.csv", cur, common.CURRENT_FIELDS)
            log = [dict(date="2026-11-20", store="s", product_id=p, url=f"https://s/{p}", sale_since="", result=r,
                        job_id="", submitted_at="") for p, r in (("1", "submitted"), ("2", "submitted"), ("3", "error:x"),
                                                                 ("4", "submitted"), ("5", "submitted"))]
            common.write_csv(d / "log" / "2026-11-20.csv", log, archive.LOG_FIELDS)
            with mock.patch.object(snapshots, "CURRENT", d / "cur"), mock.patch.object(snapshots, "ARCHIVE_LOG", d / "log"), \
                    mock.patch.object(snapshots, "load_stores", lambda: [{"id": "s"}]):
                got = snapshots.pick_sample(5, "2026-10-27", "2026-11-25")
                self.assertEqual(sorted(t["product_id"] for t in got), ["1", "5"])
                self.assertEqual(len(snapshots.pick_sample(1, "2026-10-27", "2026-11-25")), 1)


class CaptureCheckTest(unittest.TestCase):
    """checks/captures.py: the parts that need no network."""

    def test_outcome_names_what_the_archive_answered(self):
        from checks import captures
        self.assertEqual(captures.outcome({"status": "success", "http_status": 200}), "saved")
        self.assertEqual(captures.outcome({"status": "success"}), "saved")
        self.assertEqual(captures.outcome({"status": "success", "http_status": 429}), "saved:429")
        self.assertEqual(captures.outcome({"status": "pending", "resources": []}), "pending")
        self.assertEqual(captures.outcome({"status": "error", "status_ext": "error:service-unavailable", "message": "x"}), "error:service-unavailable")
        self.assertEqual(captures.outcome({"status": "error", "exception": "Timeout"}), "error:Timeout")
        self.assertEqual(captures.outcome({"status": "error"}), "error:no reason given")
        self.assertEqual(captures.outcome({"message": "job not found"}), "unknown")
        self.assertEqual(captures.outcome({}), "unknown")

    def test_jobs_are_accepted_requests_and_the_same_ones_every_time(self):
        from checks import captures
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            row = dict(date="2026-11-27", product_id="1", sale_since="2026-10-27", submitted_at="2026-11-27T01:00:00")
            rows = [dict(row, store="a", url=f"https://a/{i}", result="submitted", job_id=f"spn2-{i}") for i in range(5)]
            rows += [dict(row, store="a", url="https://a/e", result="error:host is paused", job_id=""),
                     dict(row, store="a", url="https://a/r", result="recent", job_id=""),
                     dict(row, store="b", url="https://b/1", result="submitted", job_id="spn2-b")]
            common.append_csv(d / "2026-11-27.csv", rows, archive.LOG_FIELDS)
            common.append_csv(d / "2026-11-28.csv", [dict(row, date="2026-11-28", store="a", url="https://a/9", result="submitted", job_id="spn2-9")], archive.LOG_FIELDS)
            with mock.patch.object(captures, "ARCHIVE_LOG", d):
                got = captures.pick_jobs(["2026-11-27", "2026-11-28", "2026-11-29"], 2)
                again = captures.pick_jobs(["2026-11-27", "2026-11-28", "2026-11-29"], 2)
        self.assertEqual(got, again)
        self.assertEqual([(r["date"], r["store"]) for r in got], [("2026-11-27", "a")] * 2 + [("2026-11-27", "b"), ("2026-11-28", "a")])
        self.assertTrue(all(r["result"] == "submitted" and r["job_id"] for r in got))

    def test_table_has_a_column_for_every_outcome_seen(self):
        from checks import captures
        lines = captures.table({"fox": {"saved": 3, "error:x": 1}, "betili": {"saved": 4}}, "store")
        self.assertEqual(lines[0], "| store | asked | saved | error:x |")
        self.assertEqual(lines[2:], ["| fox | 4 | 3 | 1 |", "| betili | 4 | 4 | 0 |"])


if __name__ == "__main__":
    unittest.main()
