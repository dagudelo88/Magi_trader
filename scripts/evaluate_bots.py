"""
Bot evaluation report — reads the DB and prints a health summary.

Usage:
    python scripts/evaluate_bots.py                # all bots, last 1h
    python scripts/evaluate_bots.py --hours 4      # last 4 hours
    python scripts/evaluate_bots.py --hours 0.5    # last 30 min
    python scripts/evaluate_bots.py --bot <id>     # single bot (prefix OK)
    python scripts/evaluate_bots.py --verbose      # include per-voter breakdown
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from database import DB_PATH

# ── helpers ──────────────────────────────────────────────────────────────────

def _pct(n: int, total: int) -> str:
    return f"{n / total * 100:.1f}%" if total else "n/a"

def _ts(epoch_ms: int | None) -> str:
    if epoch_ms is None:
        return "—"
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%H:%M:%S UTC")

def _ago(epoch_ms: int | None) -> str:
    if epoch_ms is None:
        return "—"
    secs = int(datetime.now(tz=timezone.utc).timestamp() * 1000 - epoch_ms) // 1000
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s ago"
    return f"{secs // 3600}h {(secs % 3600) // 60}m ago"

def _bar(value: float, width: int = 18) -> str:
    filled = round(max(0.0, min(1.0, value)) * width)
    return "█" * filled + "░" * (width - filled)

def _activity_label(hold_pct: float) -> str:
    if hold_pct > 0.97:
        return "FLAT     -- almost all HOLD, voters may be too passive for this window"
    if hold_pct > 0.90:
        return "LOW      -- few signals, consider lowering threshold or wider window"
    if hold_pct > 0.70:
        return "MODERATE -- typical for 5m/1h bots"
    return "ACTIVE   -- healthy trade signal rate"

# ── queries ───────────────────────────────────────────────────────────────────

def fetch_bots(conn: sqlite3.Connection, bot_prefix: str | None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM bots" + (" WHERE bot_id LIKE ?" if bot_prefix else "") + " ORDER BY created_at"
    return conn.execute(sql, (f"{bot_prefix}%",) if bot_prefix else ()).fetchall()


def fetch_decisions(conn: sqlite3.Connection, bot_id: str, since_ms: int) -> list[sqlite3.Row]:
    """
    bot_decisions has no own timestamp — join market_ticks via tick_id.
    action is uppercase: BUY / SELL / HOLD.
    """
    return conn.execute(
        """
        SELECT  d.action,
                d.confidence,
                t.timestamp  AS tick_ts
        FROM    bot_decisions d
        JOIN    market_ticks  t ON t.tick_id = d.tick_id
        WHERE   d.bot_id = ?
          AND   t.timestamp >= ?
        ORDER   BY t.timestamp DESC
        """,
        (bot_id, since_ms),
    ).fetchall()


def fetch_voter_summary(
    conn: sqlite3.Connection, bot_id: str, since_ms: int
) -> dict[str, dict[str, int]]:
    """Per-voter signal counts from voter_feedback (timestamp is in ms)."""
    rows = conn.execute(
        """
        SELECT  voter_name, voter_signal, COUNT(*) AS n
        FROM    voter_feedback
        WHERE   bot_id = ? AND timestamp >= ?
        GROUP   BY voter_name, voter_signal
        """,
        (bot_id, since_ms),
    ).fetchall()
    summary: dict[str, dict[str, int]] = defaultdict(lambda: {"buy": 0, "sell": 0, "hold": 0})
    for r in rows:
        summary[r["voter_name"]][r["voter_signal"]] = r["n"]
    return dict(summary)


def fetch_tick_summary(conn: sqlite3.Connection, since_ms: int) -> dict[str, int]:
    """Tick count per asset in the window."""
    rows = conn.execute(
        "SELECT target_asset, COUNT(*) AS n FROM market_ticks WHERE timestamp >= ? GROUP BY target_asset",
        (since_ms,),
    ).fetchall()
    return {r["target_asset"]: r["n"] for r in rows}


# ── report ────────────────────────────────────────────────────────────────────

def report_bot(
    bot: sqlite3.Row,
    decisions: list[sqlite3.Row],
    voter_summary: dict[str, dict[str, int]],
    verbose: bool,
    hours: float,
) -> None:
    params = json.loads(bot["strategy_params_json"] or "{}")
    voters: list[str] = params.get("voters", [])
    mode: str = params.get("consensus_mode", "?")
    threshold: float = params.get("consensus_threshold", 0)

    total = len(decisions)
    counts: dict[str, int] = {"BUY": 0, "SELL": 0, "HOLD": 0}
    confidences: list[float] = []
    last_ts: int | None = None
    last_action = "—"

    for d in decisions:
        action = (d["action"] or "HOLD").upper()
        counts[action] = counts.get(action, 0) + 1
        if d["confidence"] is not None:
            confidences.append(float(d["confidence"]))
        if last_ts is None:
            last_ts = d["tick_ts"]
            last_action = action

    trade_count = counts["BUY"] + counts["SELL"]
    avg_conf = f"{sum(confidences) / len(confidences):.3f}" if confidences else "n/a"
    hold_pct = counts["HOLD"] / total if total else 1.0

    print(f"\n  {'─' * 72}")
    print(f"  {bot['name']}  [{bot['status'].upper()}]")
    print(f"  ID       : {bot['bot_id']}")
    print(f"  Strategy : {bot['strategy']}  |  Symbol: {bot['symbol']}")
    print(f"  Consensus: {mode}  threshold={threshold}  |  Voters ({len(voters)}): {', '.join(voters)}")

    if total == 0:
        print(f"  [No decisions in last {hours:.0f}h window]")
        return

    window_h = f"{hours:.0f}h" if hours >= 1 else f"{int(hours * 60)}m"
    print(f"\n  Decisions in last {window_h}  ({total} total)")
    print(f"    {'BUY ':<5} {counts['BUY']:>5}  {_bar(counts['BUY'] / total)} {_pct(counts['BUY'], total):>6}")
    print(f"    {'SELL':<5} {counts['SELL']:>5}  {_bar(counts['SELL'] / total)} {_pct(counts['SELL'], total):>6}")
    print(f"    {'HOLD':<5} {counts['HOLD']:>5}  {_bar(counts['HOLD'] / total)} {_pct(counts['HOLD'], total):>6}")

    print(f"\n  Trades fired : {trade_count}/{total} = {_pct(trade_count, total)}")
    print(f"  Avg confidence (non-hold): {avg_conf}")
    print(f"  Last decision: {last_action} at {_ts(last_ts)} ({_ago(last_ts)})")
    print(f"  Activity     : {_activity_label(hold_pct)}")

    if verbose and voter_summary:
        print(f"\n  Voter breakdown:")
        print(f"    {'Voter':<28} {'BUY':>5} {'SELL':>5} {'HOLD':>6}  {'Active%':>7}")
        print(f"    {'─' * 58}")
        for voter in voters:
            vc = voter_summary.get(voter, {"buy": 0, "sell": 0, "hold": 0})
            vtotal = sum(vc.values())
            non_hold = vc["buy"] + vc["sell"]
            nh_pct = _pct(non_hold, vtotal)
            flag = " <-- passive" if vtotal > 0 and non_hold / vtotal < 0.05 else ""
            print(f"    {voter:<28} {vc['buy']:>5} {vc['sell']:>5} {vc['hold']:>6}  {nh_pct:>7}{flag}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate MagiTrader bots.")
    parser.add_argument("--hours", type=float, default=1.0, help="Look-back window in hours (default: 1)")
    parser.add_argument("--bot",   type=str,   default=None, help="Bot ID prefix filter")
    parser.add_argument("--verbose", action="store_true", help="Show per-voter signal breakdown")
    args = parser.parse_args()

    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    since_ms = int(now_ms - args.hours * 3600 * 1000)
    since_label = datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    bots = fetch_bots(conn, args.bot)
    tick_summary = fetch_tick_summary(conn, since_ms)
    window_label = f"{args.hours:.0f}h" if args.hours >= 1 else f"{int(args.hours * 60)}m"

    print(f"\n{'=' * 74}")
    print(f"  MagiTrader Evaluation  |  Window: last {window_label}  (since {since_label})")
    print(f"{'=' * 74}")

    total_ticks = sum(tick_summary.values())
    tick_window_secs = args.hours * 3600
    print(f"\n  Market data  ({total_ticks:,} ticks in window)")
    for asset, n in sorted(tick_summary.items()):
        rate = f"{n / tick_window_secs:.2f}/s"
        bar = _bar(min(n / tick_window_secs / 2.0, 1.0), 12)
        print(f"    {asset:<14} {n:>7,} ticks  {bar}  {rate}")

    if not bots:
        print("\n  No bots found.\n")
        conn.close()
        return

    print(f"\n  {len(bots)} bots found")
    for bot in bots:
        decisions = fetch_decisions(conn, bot["bot_id"], since_ms)
        voter_summary = fetch_voter_summary(conn, bot["bot_id"], since_ms) if args.verbose else {}
        report_bot(bot, decisions, voter_summary, args.verbose, args.hours)

    print(f"\n{'=' * 74}")
    print(f"  Tip: add --verbose for per-voter signal breakdown")
    print(f"       use --hours 4 for a wider window\n")

    conn.close()


if __name__ == "__main__":
    main()
