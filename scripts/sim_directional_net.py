"""Simulate new directional_net rules against existing voter_feedback."""
import sqlite3, os

DB = os.path.join(os.path.dirname(__file__), "..", "data", "magitrader.db")
conn = sqlite3.connect(DB, timeout=5)
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT bot_id, timestamp,
        SUM(CASE WHEN voter_signal='buy'  THEN 1 ELSE 0 END) as nb,
        SUM(CASE WHEN voter_signal='sell' THEN 1 ELSE 0 END) as ns,
        COUNT(*) as n
    FROM voter_feedback
    GROUP BY bot_id, timestamp
    HAVING n > 1
    ORDER BY timestamp DESC LIMIT 5000
""").fetchall()

rules = {
    "directional_net_0.15 (high)":  0.15,
    "directional_net_0.20 (lag-h)": 0.20,
    "directional_net_0.25 (lag-m)": 0.25,
    "directional_net_0.30 (low)":   0.30,
    "directional_net_0.35 (lag-l)": 0.35,
}

print(f"\nSimulated on {len(rows):,} voting cycles from voter_feedback\n")
print(f"  {'Rule':<32}  {'Buy':>6}  {'Sell':>6}  {'Hold':>6}  Active%")
print("  " + "-" * 62)
for name, thresh in rules.items():
    r = {"buy": 0, "sell": 0, "hold": 0}
    for row in rows:
        nb, ns, n = row["nb"], row["ns"], row["n"]
        net = (nb - ns) / n if n > 0 else 0.0
        if net > thresh:
            r["buy"] += 1
        elif net < -thresh:
            r["sell"] += 1
        else:
            r["hold"] += 1
    total = sum(r.values())
    active_pct = (r["buy"] + r["sell"]) / total * 100
    print(f"  {name:<32}  {r['buy']:>6,}  {r['sell']:>6,}  {r['hold']:>6,}  {active_pct:>6.1f}%")

conn.close()
print()
