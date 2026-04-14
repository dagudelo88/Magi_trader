"""
Statistical analysis of voter_feedback to understand voting behavior.
Fast read-only. Safe to run while backend is live.
"""
import sqlite3, json, time, os, sys

DB = os.path.join(os.path.dirname(__file__), "..", "data", "magitrader.db")
conn = sqlite3.connect(DB, timeout=5)
conn.row_factory = sqlite3.Row

def q(sql, *a):
    c = conn.cursor(); c.execute(sql, a); return c.fetchall()

def hdr(t): print(f"\n{'='*60}\n  {t}\n{'='*60}")

LAG = {"btc_lead_detector","roc_divergence","lag_correlation","ratio_mean_reversion"}

# ── 1. Overall signal distribution per voter ──────────────────────────────────
hdr("1. SIGNAL DISTRIBUTION PER VOTER")
rows = q("""
    SELECT voter_name,
        COUNT(*) AS total,
        SUM(CASE WHEN voter_signal='buy'  THEN 1 ELSE 0 END) AS n_buy,
        SUM(CASE WHEN voter_signal='sell' THEN 1 ELSE 0 END) AS n_sell,
        SUM(CASE WHEN voter_signal='hold' THEN 1 ELSE 0 END) AS n_hold
    FROM voter_feedback
    GROUP BY voter_name ORDER BY n_buy DESC
""")
print(f"\n  {'Voter':<24} {'Total':>6}  {'Buy%':>6}  {'Sell%':>6}  {'Hold%':>6}  Type")
print("  " + "-" * 62)
for r in rows:
    t = r["total"]
    bp = r["n_buy"]/t*100; sp = r["n_sell"]/t*100; hp = r["n_hold"]/t*100
    tag = "[lag]" if r["voter_name"] in LAG else "     "
    print(f"  {r['voter_name']:<24} {t:>6,}  {bp:>5.1f}%  {sp:>5.1f}%  {hp:>5.1f}%  {tag}")

# ── 2. Ensemble-level consensus analysis ────────────────────────────────────
hdr("2. ENSEMBLE SIGNAL DISTRIBUTION (what the bot actually decided)")
rows = q("""
    SELECT ensemble_signal, COUNT(*) AS n,
        COUNT(DISTINCT bot_id) AS bots
    FROM voter_feedback
    GROUP BY ensemble_signal ORDER BY n DESC
""")
total_vf = sum(r["n"] for r in rows)
for r in rows:
    pct = r["n"]/total_vf*100
    print(f"  {r['ensemble_signal']:<6}  {r['n']:>7,}  ({pct:.1f}%)")

# ── 3. Consensus score distribution ──────────────────────────────────────────
hdr("3. CONSENSUS SCORE DISTRIBUTION (when ensemble said BUY or SELL)")
rows = q("""
    SELECT
        CASE
            WHEN consensus_score < 0.50 THEN '<0.50'
            WHEN consensus_score < 0.55 THEN '0.50-0.55'
            WHEN consensus_score < 0.60 THEN '0.55-0.60'
            WHEN consensus_score < 0.65 THEN '0.60-0.65'
            WHEN consensus_score < 0.70 THEN '0.65-0.70'
            WHEN consensus_score < 0.80 THEN '0.70-0.80'
            ELSE '>=0.80'
        END AS bucket,
        COUNT(*) AS n,
        SUM(CASE WHEN ensemble_signal != 'hold' THEN 1 ELSE 0 END) as acted
    FROM voter_feedback
    WHERE consensus_score IS NOT NULL
    GROUP BY bucket ORDER BY bucket
""")
print(f"\n  {'Score Band':<12} {'Total':>8}  {'Non-Hold':>9}")
print("  " + "-" * 33)
for r in rows:
    print(f"  {r['bucket']:<12} {r['n']:>8,}  {r['acted']:>9,}")

# ── 4. How often do voters AGREE? (per cycle, same bot) ──────────────────────
hdr("4. VOTER AGREEMENT RATE PER CYCLE")
rows = q("""
    SELECT bot_id, timestamp,
        COUNT(*) AS n_voters,
        SUM(CASE WHEN voter_signal='buy' THEN 1 ELSE 0 END) as n_buy,
        SUM(CASE WHEN voter_signal='sell' THEN 1 ELSE 0 END) as n_sell,
        SUM(CASE WHEN voter_signal='hold' THEN 1 ELSE 0 END) as n_hold,
        ensemble_signal
    FROM voter_feedback
    GROUP BY bot_id, timestamp
    HAVING n_voters > 1
    ORDER BY timestamp DESC
    LIMIT 5000
""")
unanimous_hold = sum(1 for r in rows if r["n_hold"] == r["n_voters"])
any_nonhold    = sum(1 for r in rows if r["n_buy"]+r["n_sell"] > 0)
buy_majority   = sum(1 for r in rows if r["n_buy"] > r["n_voters"]/2)
sell_majority  = sum(1 for r in rows if r["n_sell"] > r["n_voters"]/2)
any_buy_vote   = sum(1 for r in rows if r["n_buy"] >= 1)
any_sell_vote  = sum(1 for r in rows if r["n_sell"] >= 1)
print(f"  Sample size: {len(rows):,} cycles")
print(f"  All-hold (0 active votes): {unanimous_hold:,}  ({unanimous_hold/len(rows)*100:.1f}%)")
print(f"  ≥1 buy vote:              {any_buy_vote:,}  ({any_buy_vote/len(rows)*100:.1f}%)")
print(f"  ≥1 sell vote:             {any_sell_vote:,}  ({any_sell_vote/len(rows)*100:.1f}%)")
print(f"  Buy majority (>50%):      {buy_majority:,}  ({buy_majority/len(rows)*100:.1f}%)")
print(f"  Sell majority (>50%):     {sell_majority:,}  ({sell_majority/len(rows)*100:.1f}%)")
print(f"  Acted (ensemble non-hold):{any_nonhold:,}  ({any_nonhold/len(rows)*100:.1f}%)")

# ── 5. What if we used looser rules? ─────────────────────────────────────────
hdr("5. SIMULATION: HOW MANY TRADES UNDER DIFFERENT RULES?")
# Rules to simulate on the same dataset
rule_results = {}
for r in rows:
    n = r["n_voters"]; nb = r["n_buy"]; ns = r["n_sell"]
    score_buy  = nb/n if n>0 else 0
    score_sell = ns/n if n>0 else 0
    net = (nb - ns) / n if n > 0 else 0

    rules = {
        "majority_55 (current)":  "buy" if score_buy>0.55 else ("sell" if score_sell>0.55 else "hold"),
        "majority_50":            "buy" if score_buy>0.50 else ("sell" if score_sell>0.50 else "hold"),
        "any_1_voter":            "buy" if nb>=1 else ("sell" if ns>=1 else "hold"),
        "any_2_voters":           "buy" if nb>=2 else ("sell" if ns>=2 else "hold"),
        "net_score_0.2":          "buy" if net>0.2  else ("sell" if net<-0.2  else "hold"),
        "net_score_0.1":          "buy" if net>0.1  else ("sell" if net<-0.1  else "hold"),
        "no_opposition (buy+nosell)": "buy" if nb>=1 and ns==0 else ("sell" if ns>=1 and nb==0 else "hold"),
    }
    for name, sig in rules.items():
        if name not in rule_results:
            rule_results[name] = {"buy":0,"sell":0,"hold":0}
        rule_results[name][sig] += 1

print(f"\n  {'Rule':<34} {'Buy':>6}  {'Sell':>6}  {'Hold':>6}  {'Active%':>8}")
print("  " + "-" * 65)
for name, counts in rule_results.items():
    total = sum(counts.values())
    active = counts["buy"] + counts["sell"]
    print(f"  {name:<34} {counts['buy']:>6,}  {counts['sell']:>6,}  {counts['hold']:>6,}  {active/total*100:>7.1f}%")

# ── 6. Lag voters specifically ────────────────────────────────────────────────
hdr("6. LAG VOTER AGREEMENT (all 4 lag voters per cycle)")
lag_cycles = q("""
    SELECT timestamp,
        SUM(CASE WHEN voter_signal='buy'  THEN 1 ELSE 0 END) as n_buy,
        SUM(CASE WHEN voter_signal='sell' THEN 1 ELSE 0 END) as n_sell,
        SUM(CASE WHEN voter_signal='hold' THEN 1 ELSE 0 END) as n_hold,
        COUNT(*) as n_voters,
        AVG(consensus_score) as avg_score,
        ensemble_signal
    FROM voter_feedback
    WHERE voter_name IN ('btc_lead_detector','roc_divergence','lag_correlation','ratio_mean_reversion')
    GROUP BY bot_id, timestamp
    HAVING n_voters >= 3
    ORDER BY timestamp DESC LIMIT 3000
""")
if lag_cycles:
    all_hold = sum(1 for r in lag_cycles if r["n_hold"]==r["n_voters"])
    any_agree = sum(1 for r in lag_cycles if r["n_buy"]>=2 or r["n_sell"]>=2)
    all_agree = sum(1 for r in lag_cycles if r["n_buy"]==r["n_voters"] or r["n_sell"]==r["n_voters"])
    acted = sum(1 for r in lag_cycles if r["ensemble_signal"]!="hold")
    n = len(lag_cycles)
    print(f"  Lag cycles sampled:       {n:,}")
    print(f"  All voters = hold:        {all_hold:,}  ({all_hold/n*100:.1f}%)")
    print(f"  ≥2 lag voters agree:      {any_agree:,}  ({any_agree/n*100:.1f}%)")
    print(f"  All lag voters agree:     {all_agree:,}  ({all_agree/n*100:.1f}%)")
    print(f"  Ensemble actually acted:  {acted:,}  ({acted/n*100:.1f}%)")

conn.close()
print("\nDone.\n")
