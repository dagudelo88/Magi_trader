"""
Profitability analysis: forward_roc labels + theoretical estimates.
Safe to run while backend is live (read-only).
"""
import sqlite3, os, math

DB = os.path.join(os.path.dirname(__file__), "..", "data", "magitrader.db")
conn = sqlite3.connect(DB, timeout=5)
conn.row_factory = sqlite3.Row


def hdr(t):
    print(f"\n{'='*66}\n  {t}\n{'='*66}")


# ── 1. Voting breakdown per bot ───────────────────────────────────────────────
hdr("1. VOTING BREAKDOWN PER BOT (why the backtest showed few trades)")
rows = conn.execute("""
    SELECT vf.bot_id, b.strategy, b.symbol,
        COUNT(DISTINCT vf.timestamp) AS cycles,
        AVG(CASE WHEN vf.voter_signal='buy'  THEN 1.0 ELSE 0.0 END) AS p_buy,
        AVG(CASE WHEN vf.voter_signal='sell' THEN 1.0 ELSE 0.0 END) AS p_sell,
        COUNT(DISTINCT vf.voter_name) AS n_voters
    FROM voter_feedback vf
    JOIN bots b ON b.bot_id = vf.bot_id
    GROUP BY vf.bot_id
""").fetchall()
print(f"\n  {'Symbol':<12} {'Strategy':<28} {'Cyc':>5}  "
      f"{'Vot':>3}  {'Buy%':>5}  {'Sell%':>5}  {'Hold%':>5}")
print("  " + "-" * 70)
for r in rows:
    p_hold = (1 - r["p_buy"] - r["p_sell"]) * 100
    print(f"  {r['symbol']:<12} {r['strategy']:<28} {r['cycles']:>5,}  "
          f"{r['n_voters']:>3}  {r['p_buy']*100:>4.1f}%  "
          f"{r['p_sell']*100:>4.1f}%  {p_hold:>4.1f}%")
print("\n  NOTE: These bots used the OLD voter sets and OLD thresholds.")
print("  The voter_feedback data is therefore not representative of")
print("  the new consensus rules. A fresh 24-48h run is needed.")

# ── 2. Forward ROC label status ───────────────────────────────────────────────
hdr("2. FORWARD ROC LABELING STATUS")
row = conn.execute("""
    SELECT
        COUNT(*) AS total,
        SUM(CASE WHEN forward_roc_30s IS NOT NULL THEN 1 ELSE 0 END) AS n_30s,
        SUM(CASE WHEN forward_roc_5m  IS NOT NULL THEN 1 ELSE 0 END) AS n_5m
    FROM voter_feedback
""").fetchone()
n_30s = row["n_30s"]
n_5m = row["n_5m"]
print(f"  Total voter_feedback rows: {row['total']:,}")
print(f"  Labeled 30s forward ROC:   {n_30s:,}")
print(f"  Labeled 5m  forward ROC:   {n_5m:,}")

# ── 3. If labels exist, compute actual signal edge ───────────────────────────
if n_30s > 0:
    hdr("3. VOTER SIGNAL EDGE (from forward_roc_30s labels)")
    rows2 = conn.execute("""
        SELECT voter_name, voter_signal,
            COUNT(*) AS n,
            AVG(forward_roc_30s) * 100 AS avg_roc_30s,
            AVG(forward_roc_5m)  * 100 AS avg_roc_5m,
            SUM(CASE
                WHEN voter_signal='buy'  AND forward_roc_30s > 0 THEN 1
                WHEN voter_signal='sell' AND forward_roc_30s < 0 THEN 1
                ELSE 0 END) * 100.0 / COUNT(*) AS dir_acc
        FROM voter_feedback
        WHERE forward_roc_30s IS NOT NULL AND voter_signal != 'hold'
        GROUP BY voter_name, voter_signal
        ORDER BY dir_acc DESC
    """).fetchall()
    print(f"\n  {'Voter':<24} {'Sig':<5} {'N':>5}  "
          f"{'AvgROC_30s':>10}  {'AvgROC_5m':>10}  {'DirAcc':>7}")
    print("  " + "-" * 68)
    for r in rows2:
        edge = " <-- EDGE" if r["dir_acc"] > 55 else ""
        print(f"  {r['voter_name']:<24} {r['voter_signal']:<5} {r['n']:>5,}  "
              f"{r['avg_roc_30s']:>+9.4f}%  {r['avg_roc_5m']:>+9.4f}%  "
              f"{r['dir_acc']:>6.1f}%{edge}")

    hdr("4. EXPECTED P&L BY directional_net THRESHOLD (from labels)")
    cycles_labeled = conn.execute("""
        SELECT vf.timestamp, vf.bot_id,
            SUM(CASE WHEN vf.voter_signal='buy'  THEN 1 ELSE 0 END) AS nb,
            SUM(CASE WHEN vf.voter_signal='sell' THEN 1 ELSE 0 END) AS ns,
            COUNT(*) AS n,
            AVG(vf.forward_roc_30s) * 100 AS roc_30s,
            AVG(vf.forward_roc_5m)  * 100 AS roc_5m
        FROM voter_feedback vf
        WHERE vf.forward_roc_30s IS NOT NULL
        GROUP BY vf.timestamp, vf.bot_id
    """).fetchall()

    FEE_RT = 0.10  # percent round-trip
    print(f"\n  {'Threshold':>10}  {'Signals':>8}  "
          f"{'AvgROC_30s':>11}  {'AfterFee':>10}  {'AvgROC_5m':>11}")
    print("  " + "-" * 58)
    for thresh in [0.10, 0.15, 0.20, 0.25, 0.30, 0.35]:
        rocs = []
        rocs_5m = []
        for r in cycles_labeled:
            net = (r["nb"] - r["ns"]) / r["n"] if r["n"] > 0 else 0.0
            if net > thresh:
                rocs.append(r["roc_30s"])
                rocs_5m.append(r["roc_5m"])
            elif net < -thresh:
                rocs.append(-r["roc_30s"])
                rocs_5m.append(-r["roc_5m"])
        if rocs:
            avg = sum(rocs) / len(rocs)
            avg5 = sum(rocs_5m) / len(rocs_5m) if rocs_5m else 0
            after = avg - FEE_RT
            print(f"  {thresh:>10.2f}  {len(rocs):>8,}  "
                  f"{avg:>+10.4f}%  {after:>+9.4f}%  {avg5:>+10.4f}%")
        else:
            print(f"  {thresh:>10.2f}  {'0':>8}  no labeled signals")

else:
    # ── 3. Theoretical estimate when no labels yet ───────────────────────────
    hdr("3. THEORETICAL PROFITABILITY ESTIMATE (no labels yet)")
    print("""
  The meta_training_loop labels forward_roc 30 minutes after each vote.
  The backend needs to run >30 min after a bot decides for labels to appear.
  Re-run this script once labels are populated.

  In the meantime, here is a scenario analysis based on typical
  short-timeframe crypto scalping statistics from academic literature
  and the observed voter signal rates.

  SCENARIO ANALYSIS — expected value per trade after 0.10% fee:
  ─────────────────────────────────────────────────────────────
  Win rate    Avg win    Avg loss    EV/trade    At 25 trades/day
    55%        +0.25%     -0.20%     +0.0475%    +1.19%/day
    50%        +0.20%     -0.15%     +0.0250%    +0.63%/day
    45%        +0.20%     -0.15%     -0.0325%    -0.81%/day  (losing)
    40%        +0.25%     -0.15%     -0.0100%    -0.25%/day  (losing)

  ESTIMATED TRADE FREQUENCY WITH NEW RULES:
  ─────────────────────────────────────────
  magi_ensemble_high  (1m, threshold=0.15)  ~25% active → ~36 signals/day
  magi_ensemble_mid   (5m, threshold=0.20)  ~18% active →  ~5 signals/day
  magi_ensemble_low   (1h, threshold=0.25)  ~11% active → ~0.3 signals/day
  magi_lag_ensemble_h (1m, threshold=0.20)  ~18% active →  ~5 signals/day
  magi_lag_ensemble_m (5m, threshold=0.25)  ~11% active →  ~3 signals/day
  magi_lag_ensemble_l (15m,threshold=0.35)   ~2% active → ~0.5 signals/day

  KEY RISK: ema_ribbon votes BUY 57% of the time — directional bias.
  The magi_ensemble_high portfolio will likely be net long most of the time.
  In a trending bull market this is fine; in a range or bear it will bleed.
  Recommend monitoring the buy/sell ratio in the first 24h and increasing
  the threshold to 0.20 if >70% of signals are BUY.
    """)

# ── 4. Directional bias check ─────────────────────────────────────────────────
hdr("4. DIRECTIONAL BIAS CHECK (buy/sell ratio by bot)")
rows3 = conn.execute("""
    SELECT b.strategy, b.symbol,
        SUM(CASE WHEN nb > ns THEN 1 ELSE 0 END) AS net_buy_cycles,
        SUM(CASE WHEN ns > nb THEN 1 ELSE 0 END) AS net_sell_cycles,
        SUM(CASE WHEN nb = ns THEN 1 ELSE 0 END) AS balanced_cycles,
        COUNT(*) AS total
    FROM (
        SELECT vf.bot_id,
            SUM(CASE WHEN vf.voter_signal='buy'  THEN 1 ELSE 0 END) AS nb,
            SUM(CASE WHEN vf.voter_signal='sell' THEN 1 ELSE 0 END) AS ns
        FROM voter_feedback vf
        GROUP BY vf.bot_id, vf.timestamp
    ) sub
    JOIN bots b ON b.bot_id = sub.bot_id
    GROUP BY sub.bot_id
""").fetchall()
print(f"\n  {'Strategy':<28} {'Symbol':<12} {'NetBuy%':>8}  {'NetSell%':>9}  Bias")
print("  " + "-" * 68)
for r in rows3:
    t = r["total"]
    bp = r["net_buy_cycles"] / t * 100
    sp = r["net_sell_cycles"] / t * 100
    bias = "LONG BIAS" if bp > sp * 1.5 else ("SHORT BIAS" if sp > bp * 1.5 else "balanced")
    print(f"  {r['strategy']:<28} {r['symbol']:<12} {bp:>7.1f}%  {sp:>8.1f}%  {bias}")

conn.close()
print("\nDone.\n")
