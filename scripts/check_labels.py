import sqlite3, json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

conn = sqlite3.connect(os.path.join(os.path.dirname(__file__), "..", "data", "magitrader.db"))
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Actual voter_feedback columns
print("=== voter_feedback ACTUAL COLUMNS ===")
cur.execute("PRAGMA table_info(voter_feedback)")
for r in cur.fetchall():
    print(f"  {r['cid']:>2}  {r['name']:<30}  {r['type']:<12}  nn={r['notnull']}  default={r['dflt_value']}")

# Sample row (lag voter)
print("\n=== SAMPLE voter_feedback ROW (btc_lead_detector) ===")
cur.execute("SELECT * FROM voter_feedback WHERE voter_name='btc_lead_detector' ORDER BY rowid DESC LIMIT 1")
r = cur.fetchone()
if r:
    for k in r.keys():
        v = r[k]
        if k == "features_snapshot" and v:
            try:
                v = list(json.loads(v).keys())[:5]
            except Exception:
                pass
        print(f"  {k}: {v}")
else:
    print("  (no rows)")

# Labeling status
print("\n=== forward_roc LABELING STATUS ===")
cur.execute("""
    SELECT COUNT(*) as total,
           SUM(CASE WHEN forward_roc_30s IS NOT NULL THEN 1 ELSE 0 END) as lbl30,
           SUM(CASE WHEN forward_roc_5m IS NOT NULL THEN 1 ELSE 0 END) as lbl5m
    FROM voter_feedback
""")
r = cur.fetchone()
print(f"  Total rows:     {r['total']}")
print(f"  labeled_30s:    {r['lbl30']}")
print(f"  labeled_5m:     {r['lbl5m']}")
pct = (r['lbl30'] / r['total'] * 100) if r['total'] > 0 else 0
print(f"  Label coverage: {pct:.1f}%")

# bot_decisions confidence
print("\n=== bot_decisions CONFIDENCE DISTRIBUTION ===")
cur.execute("""
    SELECT action,
           COUNT(*) AS n,
           MIN(confidence) AS min_conf,
           AVG(confidence) AS avg_conf,
           MAX(confidence) AS max_conf
    FROM bot_decisions
    WHERE action != 'HOLD'
    GROUP BY action
""")
for r in cur.fetchall():
    print(f"  {r['action']:6s}  n={r['n']:5d}  min={r['min_conf']}  avg={r['avg_conf']:.4f}  max={r['max_conf']}")

# Check meta_training_loop ran
print("\n=== MetaMagi last training check (label_voter_feedback_forward_roc) ===")
cur.execute("""
    SELECT COUNT(*) FROM voter_feedback
    WHERE created_at IS NOT NULL
""" if "created_at" in [r["name"] for r in conn.execute("PRAGMA table_info(voter_feedback)").fetchall()] else
    "SELECT 0"
)
print(f"  (see label coverage above — 0% means meta_training_loop not yet labeled rows)")

# Check how far back the oldest unlabeled row is
cur.execute("""
    SELECT MIN(rowid), MAX(rowid) FROM voter_feedback
    WHERE forward_roc_30s IS NULL
""")
r = cur.fetchone()
print(f"\n  Unlabeled rowid range: {r[0]} to {r[1]}")
cur.execute("SELECT COUNT(*) FROM voter_feedback WHERE forward_roc_30s IS NULL")
print(f"  Unlabeled count: {cur.fetchone()[0]}")

conn.close()
