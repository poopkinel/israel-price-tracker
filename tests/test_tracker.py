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
        self.assertIn("| S | c | ✅ | 1 | 1 | 100% |", text)


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


if __name__ == "__main__":
    unittest.main()
