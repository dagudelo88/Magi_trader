"""
Fast read-only DB health check.
Opens a SHORT-LIVED connection, runs lightweight SELECTs only, closes immediately.
Safe to run while the backend is live.

Run from E:/Programacion/magit/:
    python scripts/verify_db.py
"""
import sqlite3
import json
import time
import datetime
import sys
import os

DB = os.path.join(os.path.dirname(__file__), "..", "data", "magitrader.db")

def _q(conn, sql, *args):
    cur = conn.cursor()
    cur.execute(sql, args)
    return cur.fetchall()

def ok(m):   print(f"  [OK]  {m}")
def warn(m): print(f"  [!!]  {m}")
def err(m):  print(f"  [ERR] {m}")
def hdr(t):  print(f"\n{'='*56}\n  {t}\n{'='*56}")

# Single connection, closed at the end — never held open for slow work.
conn = sqlite3.connect(DB, timeout=5)
conn.row_factory = sqlite3.Row

hdr("TABLE COUNTS")
for table in ["market_ticks", "voter_feedback", "bot_decisions",
              "bots", "bot_orders", "bot_logs"]:
    n = _q(conn, f"SELECT COUNT(*) FROM {table}")[0][0]
    sym = "OK " if n > 0 else "!!"
    print(f"  [{sym}] {table:<20s}: {n:,}")

hdr("MARKET_TICKS — FRESHNESS (WAL mode)")
jm = _q(conn, "PRAGMA journal_mode")[0][0]
(ok if jm == "wal" else warn)(f"journal_mode = {jm}")

rows = _q(conn, """
    SELECT target_asset,
           COUNT(*) AS n,
           MAX(timestamp) AS last_ts,
           SUM(CASE WHEN btc_roc_1s IS NULL THEN 1 ELSE 0 END) AS null_roc
    FROM market_ticks
    GROUP BY target_asset ORDER BY target_asset
""")
TS_SCALE = 1000  # stored as ms
now_ts = time.time()
print(f"\n  {'Asset':<14} {'Ticks':>8}  {'NullROC':>8}  LastSeen  Age(s)")
print("  " + "-" * 55)
for r in rows:
    last_sec = r["last_ts"] / TS_SCALE
    age = now_ts - last_sec
    last = datetime.datetime.fromtimestamp(last_sec).strftime("%H:%M:%S")
    flag = " <-- STALE!" if age > 15 else ""
    print(f"  {r['target_asset']:<14} {r['n']:>8,}  {r['null_roc']:>8}  {last}  {age:4.1f}s{flag}")

hdr("VOTER_FEEDBACK — MetaMagi Training Data")
rows = _q(conn, """
    SELECT voter_name,
           COUNT(*) AS n,
           SUM(CASE WHEN forward_roc_30s IS NOT NULL THEN 1 ELSE 0 END) AS lbl30,
           SUM(CASE WHEN features_snapshot IS NOT NULL THEN 1 ELSE 0 END) AS has_snap
    FROM voter_feedback
    GROUP BY voter_name ORDER BY n DESC
""")
LAG_VOTERS = {"btc_lead_detector","roc_divergence","lag_correlation","ratio_mean_reversion"}
total = sum(r["n"] for r in rows)
labeled = sum(r["lbl30"] for r in rows)
snapped = sum(r["has_snap"] for r in rows)
print(f"  Total rows: {total:,}  |  labeled: {labeled:,}  |  with_snapshot: {snapped:,}")
print(f"\n  {'Voter':<24} {'Total':>7}  {'Labeled':>8}  {'HasSnap':>8}")
print("  " + "-" * 52)
for r in rows:
    tag = " [lag]" if r["voter_name"] in LAG_VOTERS else ""
    print(f"  {r['voter_name']:<24} {r['n']:>7,}  {r['lbl30']:>8,}  {r['has_snap']:>8,}{tag}")

hdr("ACTIVE BOTS")
bots = _q(conn, "SELECT name, symbol, strategy, status, execution_mode FROM bots ORDER BY created_at DESC")
for b in bots:
    sym = "[RUN]" if b["status"] == "running" else f"[{b['status'].upper()[:4]}]"
    print(f"  {sym} {b['name'][:24]:<24}  {b['symbol']:<12}  {b['strategy']:<28}  {b['execution_mode']}")

conn.close()  # Always close immediately — never hold open
print(f"\n{'='*56}\n  Done.\n{'='*56}\n")
