"""Write REPORT.md: per-store snapshot of discounts and how long they have lasted."""
from __future__ import annotations

import datetime as dt
import json

from .common import ARCHIVE_LOG, CURRENT, ROOT, RUNS, load_stores, read_csv, today


def days_between(a: str, b: str) -> int:
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def main() -> None:
    day = today()
    runs = {}
    rp = RUNS / f"{day}.json"
    if rp.exists():
        runs = {r["id"]: r for r in json.loads(rp.read_text(encoding="utf-8"))}
    first_day = min((p.stem for p in RUNS.glob("*.json")), default=day)
    tracked_days = days_between(first_day, day)

    lines = [
        "# דוח מעקב הנחות",
        "",
        f"עודכן: {day} · המעקב התחיל: {first_day} ({tracked_days} ימים)",
        "",
        "רצף הנחה נספר מתחילת המעקב, כך שבשבועות הראשונים הוא לא יכול לעבור את מספר ימי המעקב.",
        "'מעולם לא במחיר מלא' = מוצר שנמצא במעקב 30 יום לפחות ולא נראה אפילו פעם אחת בלי הנחה.",
        "",
        "| חנות | תחום | סטטוס היום | מוצרים | במבצע | % במבצע | הנחה 30+ ימים | 60+ | 90+ | מעולם לא במחיר מלא |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    tot = {"seen": 0, "sale": 0, "d30": 0, "d60": 0, "d90": 0, "never": 0}
    for s in load_stores():
        rows = [r for r in read_csv(CURRENT / f"{s['id']}.csv") if r["last_seen"] == day]
        sale = [r for r in rows if r["on_sale"] == "1"]
        streak = [days_between(r["sale_since"], day) for r in sale if r["sale_since"]]
        never = [r for r in sale if not r["last_full_price_seen"] and days_between(r["first_seen"], day) >= 30]
        st = runs.get(s["id"], {})
        status = "✅" if st.get("status") == "ok" else ("❌ " + st.get("error", "")[:40] if st else "—")
        n = {"seen": len(rows), "sale": len(sale), "d30": sum(d >= 30 for d in streak),
             "d60": sum(d >= 60 for d in streak), "d90": sum(d >= 90 for d in streak), "never": len(never)}
        for k in tot:
            tot[k] += n[k]
        pct = f"{100 * n['sale'] / n['seen']:.0f}%" if n["seen"] else "—"
        lines.append(f"| {s['name']} | {s['category']} | {status} | {n['seen']} | {n['sale']} | {pct} | {n['d30']} | {n['d60']} | {n['d90']} | {n['never']} |")
    pct = f"{100 * tot['sale'] / tot['seen']:.0f}%" if tot["seen"] else "—"
    lines.append(f"| **סה\"כ** | | | **{tot['seen']}** | **{tot['sale']}** | **{pct}** | **{tot['d30']}** | **{tot['d60']}** | **{tot['d90']}** | **{tot['never']}** |")

    logs = sorted(ARCHIVE_LOG.glob("*.csv"))[-3:]
    if logs:
        lines += ["", "## שמירה בארכיון (3 הימים האחרונים)", "", "| תאריך | תוצאות |", "|---|---|"]
        for lp in logs:
            c: dict[str, int] = {}
            for r in read_csv(lp):
                k = r["result"].split(":")[0]
                c[k] = c.get(k, 0) + 1
            lines.append(f"| {lp.stem} | " + ", ".join(f"{k}: {v}" for k, v in sorted(c.items())) + " |")
    notes = [f"- **{s['name']}**: {s['note']}" for s in load_stores() if s.get("note")]
    if notes:
        lines += ["", "## הערות על חנויות", ""] + notes
    (ROOT / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:12]))


if __name__ == "__main__":
    main()
