"""Daily collection: fetch every product's current price and pre-discount price from each store,
update data/current/<store>.csv and append changes to data/history/<store>.csv.

Usage: python -m tracker.collect [store_id ...]
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from .common import (CURRENT, CURRENT_FIELDS, HISTORY, HISTORY_FIELDS, RUNS, Http,
                     append_csv, load_stores, read_csv, today, write_csv)

MAX_PAGES = 400


def _f(x) -> float | None:
    try:
        v = float(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- fetchers
def fetch_shopify(http: Http, base: str) -> list[dict]:
    out = []
    first = base + "/products.json?limit=250&page=1"
    if not http.allowed(first):
        raise PermissionError("robots.txt disallows /products.json")
    for page in range(1, MAX_PAGES + 1):
        r = http.get(f"{base}/products.json?limit=250&page={page}")
        r.raise_for_status()
        prods = r.json().get("products", [])
        if not prods:
            break
        for p in prods:
            vs = [v for v in p.get("variants", []) if _f(v.get("price"))]
            if not vs:
                continue
            v = min(vs, key=lambda v: _f(v["price"]))
            out.append({
                "product_id": str(p["id"]),
                "title": p.get("title", ""),
                "url": f"{base}/products/{p.get('handle', '')}",
                "price": _f(v["price"]),
                "compare_at": _f(v.get("compare_at_price")),
            })
    return out


def fetch_woocommerce(http: Http, base: str) -> list[dict]:
    out = []
    api = base + "/wp-json/wc/store/v1/products"
    if not http.allowed(api):
        raise PermissionError("robots.txt disallows the WooCommerce Store API")
    for page in range(1, MAX_PAGES + 1):
        r = http.get(f"{api}?per_page=100&page={page}")
        if r.status_code == 400 and page > 1:  # past the last page
            break
        r.raise_for_status()
        prods = r.json()
        if not prods:
            break
        for p in prods:
            pr = p.get("prices") or {}
            unit = 10 ** int(pr.get("currency_minor_unit", 2))
            price = _f(pr.get("price"))
            reg = _f(pr.get("regular_price"))
            out.append({
                "product_id": str(p["id"]),
                "title": p.get("name", ""),
                "url": p.get("permalink", ""),
                "price": price / unit if price else None,
                "compare_at": reg / unit if reg else None,
            })
        total_pages = int(r.headers.get("X-WP-TotalPages", "0") or 0)
        if total_pages and page >= total_pages:
            break
    return out


MAGENTO_Q = """{products(filter:{price:{from:"0.01"}},pageSize:200,currentPage:%d){
 page_info{total_pages} items{ sku name url_key %s
 price_range{minimum_price{regular_price{value} final_price{value}}}}}}"""


def fetch_magento(http: Http, base: str) -> list[dict]:
    out = []
    url = base + "/graphql"
    if not http.allowed(url):
        raise PermissionError("robots.txt disallows /graphql")
    suffix_field = "url_suffix"
    page, total = 1, 1
    while page <= min(total, MAX_PAGES):
        r = http.post(url, json={"query": MAGENTO_Q % (page, suffix_field)})
        r.raise_for_status()
        j = r.json()
        if j.get("errors") and suffix_field:
            suffix_field = ""  # older Magento without url_suffix
            continue
        data = (j.get("data") or {}).get("products") or {}
        total = (data.get("page_info") or {}).get("total_pages") or 1
        for it in data.get("items") or []:
            m = (it.get("price_range") or {}).get("minimum_price") or {}
            fin = _f((m.get("final_price") or {}).get("value"))
            reg = _f((m.get("regular_price") or {}).get("value"))
            suffix = it.get("url_suffix") if it.get("url_suffix") is not None else ".html"
            out.append({
                "product_id": it.get("sku", ""),
                "title": it.get("name", ""),
                "url": f"{base}/{it.get('url_key', '')}{suffix}",
                "price": fin,
                "compare_at": reg,
            })
        page += 1
    return out


FETCHERS = {"shopify": fetch_shopify, "woocommerce": fetch_woocommerce, "magento": fetch_magento}


# ---------------------------------------------------------------- merge
def is_on_sale(price, compare_at) -> bool:
    return bool(price and compare_at and compare_at > price * 1.01)


def merge(store_id: str, fetched: list[dict], day: str) -> dict:
    """Update current state and history for one store. Returns summary numbers."""
    cur_path = CURRENT / f"{store_id}.csv"
    prev = {r["product_id"]: r for r in read_csv(cur_path)}
    changes = []
    seen = set()
    for p in fetched:
        pid = p["product_id"]
        if not pid or pid in seen or not p["price"]:
            continue
        seen.add(pid)
        price = round(p["price"], 2)
        cmp_ = round(p["compare_at"], 2) if p["compare_at"] else None
        sale = is_on_sale(price, cmp_)
        old = prev.get(pid)
        row = {
            "product_id": pid,
            "title": p["title"],
            "url": p["url"],
            "price": f"{price:.2f}",
            "compare_at": f"{cmp_:.2f}" if cmp_ else "",
            "on_sale": "1" if sale else "0",
            "discount_pct": str(round(100 * (1 - price / cmp_))) if sale else "",
            "first_seen": (old or {}).get("first_seen") or day,
            "last_seen": day,
            "sale_since": "",
            "last_full_price_seen": (old or {}).get("last_full_price_seen", ""),
        }
        if sale:
            continuing = old and old.get("on_sale") == "1" and old.get("sale_since")
            row["sale_since"] = old["sale_since"] if continuing else day
        else:
            row["last_full_price_seen"] = day
        if old is None or old.get("price") != row["price"] or old.get("compare_at") != row["compare_at"]:
            changes.append({"date": day, "product_id": pid, "price": row["price"], "compare_at": row["compare_at"]})
        prev[pid] = row
    rows = sorted(prev.values(), key=lambda r: r["product_id"])
    write_csv(cur_path, rows, CURRENT_FIELDS)
    append_csv(HISTORY / f"{store_id}.csv", changes, HISTORY_FIELDS)
    on_sale = [r for r in rows if r["last_seen"] == day and r["on_sale"] == "1"]
    return {"seen": len(seen), "on_sale": len(on_sale), "changes": len(changes)}


def run_store(store: dict, day: str) -> dict:
    t0 = time.time()
    res = {"id": store["id"], "platform": store["platform"]}
    try:
        fetched = FETCHERS[store["platform"]](Http(delay=1.0), store["base"].rstrip("/"))
        if not fetched:
            raise RuntimeError("no products returned")
        res.update(merge(store["id"], fetched, day), status="ok")
    except Exception as e:  # keep going with the other stores
        res.update(status="error", error=f"{type(e).__name__}: {e}"[:500])
        traceback.print_exc()
    res["seconds"] = round(time.time() - t0)
    print(json.dumps(res, ensure_ascii=False), flush=True)
    return res


def main(argv: list[str]) -> int:
    day = today()
    stores = [s for s in load_stores() if not argv or s["id"] in argv]
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda s: run_store(s, day), stores))
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / f"{day}.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(r["status"] == "ok" for r in results)
    print(f"{ok}/{len(results)} stores collected")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
