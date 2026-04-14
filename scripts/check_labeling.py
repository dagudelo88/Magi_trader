"""
Manually run label_voter_feedback_forward_roc and diagnose why it might
not be labeling rows.
"""
import sqlite3, time, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from database import label_voter_feedback_forward_roc, get_db_connection

conn = get_db_connection()
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# What's in voter_feedback?
cur.execute("SELECT MIN(timestamp), MAX(timestamp), COUNT(*) FROM voter_feedback")
r = cur.fetchone()
now_ms = int(time.time() * 1000)
oldest_ms = r[0] or 0
newest_ms = r[1] or 0
oldest_age_min = (now_ms - oldest_ms) / 60000
newest_age_min = (now_ms - newest_ms) / 60000
print(f"voter_feedback: {r[2]} rows")
print(f"  Oldest row age: {oldest_age_min:.1f} min ago")
print(f"  Newest row age: {newest_age_min:.1f} min ago")

# How many would the labeling pick up with default lookback_minutes=120?
cutoff_ms = int((time.time() - 120 * 60) * 1000)
cur.execute("""
    SELECT COUNT(*) FROM voter_feedback
    WHERE forward_roc_30s IS NULL AND timestamp >= ?
""", (cutoff_ms,))
in_window = cur.fetchone()[0]
print(f"\n  Rows within last 120-min window (unlabeled): {in_window}")

cur.execute("SELECT COUNT(*) FROM voter_feedback WHERE timestamp < ?", (cutoff_ms,))
outside = cur.fetchone()[0]
print(f"  Rows OUTSIDE 120-min window (never labelled): {outside}")

# Try a test row — does market_ticks have the data needed?
cur.execute("""
    SELECT feedback_id, timestamp, target_asset
    FROM voter_feedback
    WHERE forward_roc_30s IS NULL AND timestamp >= ?
    ORDER BY timestamp ASC LIMIT 1
""", (cutoff_ms,))
sample = cur.fetchone()
if sample:
    fid, ts, asset = sample['feedback_id'], sample['timestamp'], sample['target_asset']
    print(f"\nSample unlabeled row: feedback_id={fid}  ts={ts}  asset={asset}")

    # Can we find base price?
    cur.execute("""
        SELECT target_price, timestamp FROM market_ticks
        WHERE target_asset = ? AND timestamp <= ?
        ORDER BY timestamp DESC LIMIT 1
    """, (asset, ts))
    bp = cur.fetchone()
    print(f"  Base price tick:   {dict(bp) if bp else 'NOT FOUND'}")

    # Can we find 30s forward?
    cur.execute("""
        SELECT target_price, timestamp FROM market_ticks
        WHERE target_asset = ? AND timestamp >= ?
        ORDER BY timestamp ASC LIMIT 1
    """, (asset, ts + 30_000))
    p30 = cur.fetchone()
    print(f"  +30s price tick:   {dict(p30) if p30 else 'NOT FOUND (too recent?)'}")

    # Can we find 5m forward?
    cur.execute("""
        SELECT target_price, timestamp FROM market_ticks
        WHERE target_asset = ? AND timestamp >= ?
        ORDER BY timestamp ASC LIMIT 1
    """, (asset, ts + 300_000))
    p5m = cur.fetchone()
    print(f"  +5m price tick:    {dict(p5m) if p5m else 'NOT FOUND (too recent?)'}")

# Run the labeling now with extended lookback
print("\n=== Running label_voter_feedback_forward_roc(lookback_minutes=2880) ===")
n = label_voter_feedback_forward_roc(lookback_minutes=2880)  # 48 hours
print(f"  Labeled {n} rows")

# Check result
cur.execute("""
    SELECT COUNT(*) as total,
           SUM(CASE WHEN forward_roc_30s IS NOT NULL THEN 1 ELSE 0 END) as lbl30,
           SUM(CASE WHEN forward_roc_5m IS NOT NULL THEN 1 ELSE 0 END) as lbl5m
    FROM voter_feedback
""")
r = cur.fetchone()
print(f"  After labeling: total={r['total']}  labeled_30s={r['lbl30']}  labeled_5m={r['lbl5m']}")

conn.close()
