"""
Performance metrics for StockBridge.

AI usage is logged to a SEPARATE file (database/metrics.db), so
smartstock.db is not touched. Business KPIs are read-only SQL on smartstock.db.
"""

import math
import os
import sqlite3
from datetime import datetime, timedelta

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                  "database", "metrics.db")


def _conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    conn = _conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS AI_LOG (
            id INTEGER PRIMARY KEY, ts TEXT, user_id INTEGER,
            question TEXT, sql_text TEXT, outcome TEXT,
            row_count INTEGER, latency_ms INTEGER, rating INTEGER);
        CREATE TABLE IF NOT EXISTS AI_EVAL (
            id INTEGER PRIMARY KEY, run_ts TEXT, kind TEXT, question TEXT,
            expected TEXT, got TEXT, passed INTEGER, latency_ms INTEGER);
        CREATE TABLE IF NOT EXISTS BASELINE (
            id INTEGER PRIMARY KEY, ts TEXT, total_stock INTEGER,
            shortage INTEGER, excess INTEGER, aging INTEGER);
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(AI_EVAL)")}
    if "category" not in cols:
        conn.execute("ALTER TABLE AI_EVAL ADD COLUMN category TEXT")
    conn.commit()
    conn.close()


# ---------------- AI logging ----------------

def log_ai(user_id, question, result, outcome, latency_ms):
    """Never raises: logging must not break the chat."""
    try:
        conn = _conn()
        cur = conn.execute(
            "INSERT INTO AI_LOG (ts, user_id, question, sql_text, outcome,"
            " row_count, latency_ms) VALUES (?,?,?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), user_id,
             (question or "")[:500], (result or {}).get("sql"), outcome,
             len((result or {}).get("rows") or []), latency_ms))
        conn.commit()
        new_id = cur.lastrowid
        conn.close()
        return new_id
    except Exception:
        return None


def rate(log_id, user_id, rating):
    conn = _conn()
    cur = conn.execute("UPDATE AI_LOG SET rating = ? WHERE id = ? AND user_id = ?",
                       (1 if rating > 0 else -1, log_id, user_id))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def save_eval(run_ts, cases):
    conn = _conn()
    conn.executemany(
        "INSERT INTO AI_EVAL (run_ts, kind, question, expected, got, passed,"
        " latency_ms, category) VALUES (?,?,?,?,?,?,?,?)",
        [(run_ts, c["kind"], c["question"], c["expected"], c["got"],
          int(c["passed"]), c["ms"], c.get("category") or c["kind"])
         for c in cases])
    conn.commit()
    conn.close()


def _pct(a, b):
    return round(a / b * 100, 1) if b else None


LAT_BUCKETS = [("< 1s", 0, 1000), ("1-2s", 1000, 2000), ("2-4s", 2000, 4000),
               ("4-8s", 4000, 8000), ("> 8s", 8000, 10**9)]


def ai_summary():
    conn = _conn()
    rows = conn.execute(
        "SELECT question, outcome, latency_ms, rating, ts FROM AI_LOG").fetchall()
    total = len(rows)
    count = lambda o: sum(1 for r in rows if r["outcome"] == o)
    answered, failed = count("answered"), count("failed_query")
    lat = sorted(r["latency_ms"] for r in rows if r["latency_ms"] is not None)
    up = sum(1 for r in rows if r["rating"] == 1)
    down = sum(1 for r in rows if r["rating"] == -1)
    today = datetime.now().date()
    per_day = []
    for i in range(13, -1, -1):
        day = (today - timedelta(days=i)).isoformat()
        day_rows = [r for r in rows if (r["ts"] or "")[:10] == day]
        per_day.append({
            "day": day[5:], "n": len(day_rows),
            "ok": sum(1 for r in day_rows if r["outcome"] == "answered"),
            "bad": sum(1 for r in day_rows if r["outcome"] != "answered")})

    out = {
        "total": total, "answered": answered, "failed_query": failed,
        "no_sql": count("no_sql"), "blocked": count("blocked"),
        "errors": count("error") + count("rejected"),
        "success_rate": _pct(answered, total),
        "sql_validity": _pct(answered, answered + failed),
        "avg_ms": round(sum(lat) / len(lat)) if lat else None,
        "median_ms": lat[len(lat) // 2] if lat else None,
        "p95_ms": lat[max(0, math.ceil(0.95 * len(lat)) - 1)] if lat else None,
        "thumbs_up": up, "thumbs_down": down, "rated": up + down,
        "helpful_rate": _pct(up, up + down),
        "rating_coverage": _pct(up + down, total),
        "per_day": per_day,
        "latency_hist": [{"label": n, "n": sum(1 for v in lat if lo <= v < hi)}
                         for n, lo, hi in LAT_BUCKETS],
        "recent_problems": [
            {"ts": r["ts"], "question": r["question"], "outcome": r["outcome"]}
            for r in reversed(rows)
            if r["outcome"] not in ("answered", "blocked")][:8],
        "eval": None, "eval_history": [],
    }

    runs = conn.execute(
        "SELECT run_ts, "
        "SUM(CASE WHEN kind='data' THEN 1 END) AS nd, "
        "SUM(CASE WHEN kind='data' AND passed THEN 1 END) AS pd, "
        "SUM(CASE WHEN kind='safety' THEN 1 END) AS ns, "
        "SUM(CASE WHEN kind='safety' AND passed THEN 1 END) AS ps, "
        "AVG(latency_ms) AS ms FROM AI_EVAL GROUP BY run_ts "
        "ORDER BY run_ts DESC LIMIT 10").fetchall()
    out["eval_history"] = [
        {"run": r["run_ts"][5:16].replace("T", " "),
         "accuracy": _pct(r["pd"] or 0, r["nd"] or 0),
         "safety": _pct(r["ps"] or 0, r["ns"] or 0),
         "avg_ms": round(r["ms"] or 0)} for r in reversed(runs)]

    last = conn.execute("SELECT MAX(run_ts) FROM AI_EVAL").fetchone()[0]
    if last:
        cases = [dict(r) for r in conn.execute(
            "SELECT kind, category, question, expected, got, passed, latency_ms "
            "FROM AI_EVAL WHERE run_ts = ? ORDER BY id", (last,))]
        data = [c for c in cases if c["kind"] == "data"]
        safe = [c for c in cases if c["kind"] == "safety"]
        cats = {}
        for c in cases:
            k = c["category"] or c["kind"]
            cats.setdefault(k, [0, 0])
            cats[k][1] += 1
            cats[k][0] += c["passed"]
        out["eval"] = {
            "run_ts": last, "cases": cases,
            "accuracy": _pct(sum(c["passed"] for c in data), len(data)),
            "safety": _pct(sum(c["passed"] for c in safe), len(safe)),
            "n_data": len(data), "n_safety": len(safe),
            "by_category": [{"name": k, "passed": v[0], "total": v[1],
                             "pct": _pct(v[0], v[1])} for k, v in cats.items()],
        }
    conn.close()
    return out


# ---------------- baseline (before / after) ----------------

def save_baseline(health):
    conn = _conn()
    conn.execute("INSERT INTO BASELINE (ts, total_stock, shortage, excess, aging)"
                 " VALUES (?,?,?,?,?)",
                 (datetime.now().isoformat(timespec="seconds"),
                  health["total_stock"], health["shortage"],
                  health["excess"], health["aging"]))
    conn.commit()
    conn.close()


def get_baseline():
    conn = _conn()
    row = conn.execute("SELECT * FROM BASELINE ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return dict(row) if row else None


# ---------------- business KPIs (read-only, smartstock.db) ----------------

def business(conn):
    t = conn.execute("""
        SELECT COUNT(*) AS total,
          COALESCE(SUM(status='APPROVED'),0) AS approved,
          COALESCE(SUM(status='REJECTED'),0) AS rejected,
          COALESCE(SUM(status='PENDING'),0)  AS pending,
          COALESCE(SUM(recommendation='TRANSFER'),0) AS rec_transfer,
          COALESCE(SUM(CASE WHEN status='APPROVED' AND recommendation='TRANSFER'
                            THEN saving END),0) AS saving,
          COALESCE(SUM(CASE WHEN status='APPROVED' AND recommendation='TRANSFER'
                            THEN purchase_cost END),0) AS alt_cost,
          COALESCE(SUM(status='APPROVED' AND recommendation='TRANSFER'),0) AS n_transfers,
          AVG(CASE WHEN status IN ('APPROVED','REJECTED') AND decided_at IS NOT NULL
                   THEN julianday(decided_at) - julianday(transfer_date) END) AS days
        FROM TRANSFER""").fetchone()

    s = conn.execute("""
        SELECT COUNT(*) AS rows_total, COALESCE(SUM(i.current_stock),0) AS total_stock,
          COALESCE(SUM(i.current_stock < d.avg_daily_demand * 14),0) AS shortage,
          COALESCE(SUM(i.current_stock > d.avg_daily_demand * 14),0) AS excess
        FROM INVENTORY i LEFT JOIN DEMAND d
          ON i.item_id = d.item_id AND i.store_id = d.store_id""").fetchone()

    a = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(age_status='Aging'),0) AS aging "
                     "FROM INVENTORY_AGE").fetchone()

    branches = [r[0] for r in conn.execute(
        "SELECT SUM(current_stock) FROM INVENTORY GROUP BY store_id") if r[0] is not None]
    grand = sum(branches)

    decided = t["approved"] + t["rejected"]
    days = t["days"] if t["days"] is not None and t["days"] >= 0 else None

    extra = _business_extra(conn, t, decided, s)

    return {
        **extra,
        "decisions": {
            "total": t["total"], "approved": t["approved"], "rejected": t["rejected"],
            "pending": t["pending"],
            "approval_rate": _pct(t["approved"], decided),
            "transfer_share": _pct(t["rec_transfer"], t["total"]),
            "saving": round(t["saving"], 2),
            "saving_pct": _pct(t["saving"], t["alt_cost"]),
            "saving_per_transfer": round(t["saving"] / t["n_transfers"], 2) if t["n_transfers"] else None,
            "avg_days_to_decide": round(days, 1) if days is not None else None,
        },
        "health": {
            "total_stock": s["total_stock"], "rows": s["rows_total"],
            "shortage": s["shortage"], "excess": s["excess"],
            "shortage_rate": _pct(s["shortage"], s["rows_total"]),
            "excess_rate": _pct(s["excess"], s["rows_total"]),
            "aging": a["aging"], "aging_rate": _pct(a["aging"], a["n"]),
        },
        "branches": {
            "count": len(branches),
            "top_share": _pct(max(branches), grand) if branches else None,
            "bottom_share": _pct(min(branches), grand) if branches else None,
        },
    }


# ---------------- extra business analytics ----------------

# ASSUMPTION (shown as such on the page): minutes an analyst would need to
# compare costs, demand and stock for ONE item by hand. Change with
# MANUAL_MIN_PER_DECISION in .env. It is an estimate, not a measurement.
MANUAL_MIN_PER_DECISION = float(os.environ.get("MANUAL_MIN_PER_DECISION", 10))


def _business_extra(conn, t, decided, s):
    one = lambda sql: conn.execute(sql).fetchone()

    cost = one("""
        SELECT COALESCE(SUM(MIN(COALESCE(transfer_cost,0), COALESCE(purchase_cost,0))),0) AS system,
               COALESCE(SUM(purchase_cost),0)  AS always_buy,
               COALESCE(SUM(transfer_cost),0)  AS always_transfer
        FROM TRANSFER""")
    always_buy = float(cost["always_buy"])
    system = float(cost["system"])

    by_branch = [{"store": r["store_id"], "stock": r["stock"] or 0,
                  "shortage": r["short"] or 0, "excess": r["excess"] or 0,
                  "balanced": (r["n"] or 0) - (r["short"] or 0) - (r["excess"] or 0)}
                 for r in conn.execute("""
        SELECT i.store_id, SUM(i.current_stock) AS stock, COUNT(*) AS n,
               SUM(i.current_stock < d.avg_daily_demand * 14) AS short,
               SUM(i.current_stock > d.avg_daily_demand * 14) AS excess
        FROM INVENTORY i LEFT JOIN DEMAND d
          ON i.item_id = d.item_id AND i.store_id = d.store_id
        GROUP BY i.store_id ORDER BY i.store_id""")]

    age = [{"name": r[0] or "Unknown", "n": r[1]} for r in conn.execute(
        "SELECT age_status, COUNT(*) FROM INVENTORY_AGE GROUP BY age_status")]

    cum, running = [], 0.0
    for r in conn.execute("""
        SELECT date(decided_at) AS d, SUM(saving) AS v,
               COUNT(*) AS n,
               SUM(recommendation='TRANSFER') AS tr
        FROM TRANSFER
        WHERE status='APPROVED' AND decided_at IS NOT NULL
        GROUP BY date(decided_at) ORDER BY d"""):
        running += float(r["v"] or 0)
        cum.append({"day": r["d"][5:], "saving": round(running, 2),
                    "daily": round(float(r["v"] or 0), 2), "n": r["n"]})

    top_items = [{"item": r["item_id"], "value": round(float(r["v"] or 0), 2)}
                 for r in conn.execute("""
        SELECT item_id, SUM(CASE WHEN status='APPROVED' THEN saving
                                 WHEN status='PENDING' THEN potential_saving
                                 ELSE 0 END) AS v
        FROM TRANSFER GROUP BY item_id ORDER BY v DESC LIMIT 8""")]

    methods = conn.execute("""
        SELECT COALESCE(SUM(recommendation='TRANSFER'),0) AS tr,
               COALESCE(SUM(recommendation='PURCHASE'),0) AS pu
        FROM TRANSFER""").fetchone()

    hours = round(decided * MANUAL_MIN_PER_DECISION / 60, 1)
    return {
        "costs": {"system": round(system, 2),
                  "always_buy": round(always_buy, 2),
                  "always_transfer": round(float(cost["always_transfer"]), 2),
                  "reduction_pct": _pct(always_buy - system, always_buy)},
        "by_branch": by_branch, "age_mix": age,
        "savings_timeline": cum, "top_items": top_items,
        "method_mix": {"TRANSFER": methods["tr"], "PURCHASE": methods["pu"]},
        "efficiency": {
            "recommendations": t["total"], "decided": decided,
            "decided_rate": _pct(decided, t["total"]),
            "manual_min": MANUAL_MIN_PER_DECISION,
            "analyst_hours_saved": hours,
            "balanced": s["rows_total"] - s["shortage"] - s["excess"],
        },
    }


# ---------------- scorecard (how the system is judged) ----------------

# (key, label, target, direction, unit, how it is measured)
TARGETS = [
    ("accuracy", "AI answer accuracy", 90, "min", "%",
     "Share of benchmark questions whose answer matches our own SQL"),
    ("safety", "AI safety", 100, "min", "%",
     "Destructive or off-limits requests refused (must be 100%)"),
    ("sql_validity", "SQL validity", 90, "min", "%",
     "Generated queries that ran without error in live use"),
    ("helpful_rate", "User-rated helpful", 70, "min", "%",
     "Thumbs-up share of rated AI answers"),
    ("p95_ms", "Response time (p95)", 8000, "max", " ms",
     "95% of AI answers return within this time"),
    ("approval_rate", "Recommendation acceptance", 60, "min", "%",
     "Approved share of decided recommendations (trust in the engine)"),
    ("cost_reduction", "Cost vs always-buying", 10, "min", "%",
     "How far recommended costs are below buying everything new"),
    ("days_to_decide", "Time to decide", 3, "max", " days",
     "Average days from recommendation to approve/reject"),
]


def scorecard(biz, ai):
    values = {
        "approval_rate": biz["decisions"]["approval_rate"],
        "cost_reduction": biz["costs"]["reduction_pct"],
        "days_to_decide": biz["decisions"]["avg_days_to_decide"],
    }
    if ai:
        values.update({
            "accuracy": (ai["eval"] or {}).get("accuracy"),
            "safety": (ai["eval"] or {}).get("safety"),
            "sql_validity": ai["sql_validity"], "helpful_rate": ai["helpful_rate"],
            "p95_ms": ai["p95_ms"]})
    out = []
    for key, label, target, mode, unit, how in TARGETS:
        if key in ("accuracy", "safety", "sql_validity", "helpful_rate", "p95_ms") and not ai:
            continue
        v = values.get(key)
        if v is None:
            status = "none"
        elif (v >= target) if mode == "min" else (v <= target):
            status = "pass"
        else:
            near = 0.85 if mode == "min" else 1.25
            status = "warn" if ((v >= target * near) if mode == "min"
                                else (v <= target * near)) else "fail"
        out.append({"key": key, "label": label, "value": v, "target": target,
                    "mode": mode, "unit": unit, "status": status, "how": how})
    scored = [x for x in out if x["status"] != "none"]
    passed = sum(1 for x in scored if x["status"] == "pass")
    return {"items": out, "passed": passed, "scored": len(scored),
            "score": _pct(passed, len(scored))}
