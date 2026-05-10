"""
Per-bot strategy report: settings, risk, FIFO PnL / ROI, decisions, and voter context.

Reads SQLite (same DB as the app). Does not call the exchange unless --fetch-mark.

Usage:
    python scripts/bot_strategy_report.py --ui
        # TUI: each [ Run report ] also writes reports/strategy_report_<stub>.pdf,
        # reports/strategy_report_<stub>.txt, and _for_llm.json
    python scripts/bot_strategy_report.py --list
    python scripts/bot_strategy_report.py --bot ab12cd34
    python scripts/bot_strategy_report.py --bot ab12 --hours 24
    python scripts/bot_strategy_report.py --bot ab12 --hours 168 --out reports/bot_ab12.txt
    python scripts/bot_strategy_report.py --bot ab12 --json > report.json
    python scripts/bot_strategy_report.py --bot ab12 --bundle-dir reports

PnL / ROI always uses all filled orders in the DB (full FIFO). The --hours window
applies to decisions, voter_feedback, and optional forward-ROC stats only.
For gated order-window PnL experiments, use --orders-since-hours (see note in report).
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from database import DB_PATH  # noqa: E402
from trading.bot_performance import (  # noqa: E402
    compute_closed_trades,
    compute_strategy_performance,
)
from trading.risk_settings import get_effective_bot_risk_settings  # noqa: E402
from trading.strategy_budget import initial_budget_from_strategy_params_json  # noqa: E402


def _pct(n: int, total: int) -> str:
    return f"{n / total * 100:.1f}%" if total else "n/a"


def _ts_ms(epoch_ms: int | None) -> str:
    if epoch_ms is None:
        return "-"
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def _activity_label(hold_pct: float) -> str:
    if hold_pct > 0.97:
        return "Almost all HOLD - voters may be too passive for this window"
    if hold_pct > 0.90:
        return "Low signal rate - consider consensus threshold or timeframes"
    if hold_pct > 0.70:
        return "Moderate activity - typical for many bots"
    return "Active - higher trade-signal rate"


# --- Terminal UI (arrow keys; falls back to line input if stdin is not a TTY) ---

WINDOW_HOURS_PRESETS: list[float] = [1.0, 6.0, 12.0, 24.0, 72.0, 168.0, 720.0]
ORDERS_WINDOW_PRESETS: list[float | None] = [None, 1.0, 6.0, 24.0, 168.0]


def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False


def _read_key_interactive() -> str:
    """Return logical key: up, down, left, right, enter, escape, space, quit."""
    if sys.platform == "win32":
        import msvcrt

        c = msvcrt.getwch()
        if c in ("\x00", "\xe0"):  # arrow / function prefix on Windows
            c2 = msvcrt.getwch()
            if c2 == "H":
                return "up"
            if c2 == "P":
                return "down"
            if c2 == "K":
                return "left"
            if c2 == "M":
                return "right"
            return f"unknown-{c2!r}"
        if c in ("\r", "\n"):
            return "enter"
        if c == "\x1b":
            return "escape"
        if c in ("q", "Q"):
            return "quit"
        if c == " ":
            return "space"
        return c

    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        select.select([sys.stdin], [], [])
        ch = sys.stdin.read(1)
        if ch == "\n" or ch == "\r":
            return "enter"
        if ch == "\x1b":
            if not select.select([sys.stdin], [], [], 0.05)[0]:
                return "escape"
            ch2 = sys.stdin.read(1)
            if ch2 == "[":
                ch3 = sys.stdin.read(1)
                if ch3 == "A":
                    return "up"
                if ch3 == "B":
                    return "down"
                if ch3 == "C":
                    return "right"
                if ch3 == "D":
                    return "left"
            return "escape"
        if ch in ("q", "Q"):
            return "quit"
        if ch == " ":
            return "space"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _tui_clear() -> None:
    if sys.platform == "win32":
        os.system("cls")
    else:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


def _fetch_bots_sorted_by_name(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM bots
        ORDER BY LOWER(TRIM(COALESCE(name, ''))), bot_id
        """
    )
    return cur.fetchall()


def _bot_picker_labels(rows: list[sqlite3.Row]) -> list[str]:
    names = [(r["name"] or "").strip() or "(unnamed)" for r in rows]
    c = Counter(names)
    lines: list[str] = []
    for r, nm in zip(rows, names, strict=True):
        sym = r["symbol"] or ""
        st = r["status"] or ""
        if c[nm] > 1:
            bid = r["bot_id"]
            short = f"{bid[:8]}..." if len(bid) > 8 else bid
            lines.append(f"{nm}  |  {sym}  |  {st}  |  id {short}")
        else:
            lines.append(f"{nm}  |  {sym}  |  {st}")
    return lines


def _prompt_line(prompt: str, default: str = "") -> str:
    sys.stdout.write(f"{prompt}")
    sys.stdout.flush()
    try:
        line = sys.stdin.readline()
    except EOFError:
        line = ""
    return (line.strip() if line else "") or default


def _tui_pick_bot(conn: sqlite3.Connection) -> sqlite3.Row | Literal["quit"] | None:
    rows = _fetch_bots_sorted_by_name(conn)
    if not rows:
        print("\nNo bots in the database.\n")
        return None
    labels = _bot_picker_labels(rows)
    title = "Select a bot (name / symbol / status)"
    help_line = (
        "Up/Down  move   Enter  select   Q  quit"
        if _stdin_is_tty()
        else "Enter a number 1-%d (empty=cancel)" % len(labels)
    )

    if not _stdin_is_tty():
        _tui_clear()
        print(title)
        print("-" * 72)
        for i, lab in enumerate(labels):
            print(f"  {i + 1}. {lab}")
        print("-" * 72)
        raw = _prompt_line(f"Choice [1-{len(labels)}]: ")
        if not raw:
            return None
        try:
            n = int(raw)
        except ValueError:
            return None
        if 1 <= n <= len(rows):
            return rows[n - 1]
        return None

    idx = 0
    while True:
        _tui_clear()
        print(title)
        print(help_line)
        print("-" * 72)
        for i, lab in enumerate(labels):
            mark = ">" if i == idx else " "
            print(f"{mark} {i + 1}. {lab}")
        print("-" * 72)

        key = _read_key_interactive()
        if key == "quit" or key == "escape":
            return "quit"
        if key == "up":
            idx = (idx - 1) % len(labels)
        elif key == "down":
            idx = (idx + 1) % len(labels)
        elif key == "enter":
            return rows[idx]


@dataclass
class _TuiReportConfig:
    hours: float
    orders_since_hours: float | None
    fetch_mark: bool
    out: str | None
    as_json: bool


def _safe_report_filename_stub(bot: sqlite3.Row) -> str:
    name = (bot["name"] or "bot").strip()
    stub = re.sub(r"[^\w\-.]+", "_", name, flags=re.UNICODE).strip("_")[:48]
    if not stub:
        stub = str(bot["bot_id"])[:12]
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M")
    return f"{stub}_{ts}"


def _tui_configure_options(bot: sqlite3.Row, preset: _TuiReportConfig) -> Literal["back"] | _TuiReportConfig | None:
    """Options menu: back = pick another bot, None = quit app."""
    def nearest_idx(presets: list[float], value: float) -> int:
        if not presets:
            return 0
        best = 0
        best_d = abs(presets[0] - value)
        for i, p in enumerate(presets):
            d = abs(p - value)
            if d < best_d:
                best_d = d
                best = i
        return best

    hours_i = nearest_idx(WINDOW_HOURS_PRESETS, preset.hours)
    try:
        orders_i = ORDERS_WINDOW_PRESETS.index(preset.orders_since_hours)
    except ValueError:
        orders_i = 0

    fetch = preset.fetch_mark
    out_mode = 1 if preset.out else 0
    fmt_json = preset.as_json
    cursor = 0
    # rows 0-5 settings, 6 run, 7 another bot, 8 quit
    n_rows = 9

    if not _stdin_is_tty():
        _tui_clear()
        print(f"Report options for: {(bot['name'] or bot['bot_id'])!r}\n")
        for i, h in enumerate(WINDOW_HOURS_PRESETS):
            print(f"  {i + 1}. {h:g} h window")
        hi = _prompt_line(f"Decision window [default nearest {preset.hours:g}h = {hours_i + 1}]: ")
        if hi.strip().isdigit():
            hours_i = max(0, min(len(WINDOW_HOURS_PRESETS) - 1, int(hi) - 1))
        print("\nOrders-only FIFO slice (informational):")
        for i, oh in enumerate(ORDERS_WINDOW_PRESETS):
            lab = "off" if oh is None else f"{oh:g} h"
            print(f"  {i + 1}. {lab}")
        oi = _prompt_line(f"Orders window [1-{len(ORDERS_WINDOW_PRESETS)}, default {orders_i + 1}]: ")
        if oi.strip().isdigit():
            orders_i = max(
                0,
                min(len(ORDERS_WINDOW_PRESETS) - 1, int(oi) - 1),
            )
        fm = _prompt_line("Fetch mark price from exchange? y/N: ").lower()
        fetch = fm in ("y", "yes")
        om = _prompt_line("Write to file instead of console? y/N: ").lower()
        out_mode = 1 if om in ("y", "yes") else 0
        j = _prompt_line("JSON output? y/N: ").lower()
        fmt_json = j in ("y", "yes")
        path: str | None = None
        if out_mode:
            stub = _safe_report_filename_stub(bot)
            ext = "json" if fmt_json else "txt"
            default_path = os.path.join("reports", f"strategy_report_{stub}.{ext}")
            p = _prompt_line(f"Output path [{default_path}]: ")
            path = p or default_path
        return _TuiReportConfig(
            hours=WINDOW_HOURS_PRESETS[hours_i],
            orders_since_hours=ORDERS_WINDOW_PRESETS[orders_i],
            fetch_mark=fetch,
            out=path,
            as_json=fmt_json,
        )

    help_line = (
        "Up/Down  move row   Left/Right  change value   "
        "Enter on 'Run'  generate   Esc / Q  quit"
    )

    while True:
        _tui_clear()
        bn = bot["name"] or "(unnamed)"
        print(f"Report options  --  {bn}  ({bot['symbol']})")
        print(help_line)
        print("-" * 72)

        h_val = f"{WINDOW_HOURS_PRESETS[hours_i]:g} h"
        o_val = (
            "off"
            if ORDERS_WINDOW_PRESETS[orders_i] is None
            else f"{ORDERS_WINDOW_PRESETS[orders_i]!s} h (informational FIFO slice)"
        )
        f_val = "yes" if fetch else "no"
        out_val = "save to file" if out_mode else "print to console"
        fmt_val = "JSON" if fmt_json else "text"
        stub = _safe_report_filename_stub(bot)
        ext = "json" if fmt_json else "txt"
        default_path = os.path.join("reports", f"strategy_report_{stub}.{ext}")
        path_line = default_path if out_mode else "(stdout)"

        print(
            "  Also writes: reports/strategy_report_<stub>.pdf + .txt + _for_llm.json"
        )

        lines = [
            f"Decision / voter window     :  {h_val}",
            f"Orders-only window (extra)  :  {o_val}",
            f"Fetch mark (unrealized)     :  {f_val}",
            f"Output destination          :  {out_val}",
            f"Output path (if file)       :  {path_line}",
            f"Format                      :  {fmt_val}",
            "[ Run report ]",
            "[ Pick another bot ]",
            "[ Quit ]",
        ]

        for i, line in enumerate(lines):
            mark = ">" if i == cursor else " "
            print(f"{mark} {line}")

        print("-" * 72)

        key = _read_key_interactive()
        if key == "quit":
            return None
        if key == "escape":
            return None
        if key == "up":
            cursor = (cursor - 1) % n_rows
        elif key == "down":
            cursor = (cursor + 1) % n_rows
        elif key in ("left", "right"):
            delta = -1 if key == "left" else 1
            if cursor == 0:
                hours_i = (hours_i + delta) % len(WINDOW_HOURS_PRESETS)
            elif cursor == 1:
                orders_i = (orders_i + delta) % len(ORDERS_WINDOW_PRESETS)
            elif cursor == 2:
                fetch = not fetch
            elif cursor == 3:
                out_mode = (out_mode + delta) % 2
            elif cursor == 4:
                pass
            elif cursor == 5:
                fmt_json = not fmt_json
        elif key == "space":
            if cursor == 2:
                fetch = not fetch
            elif cursor == 3:
                out_mode = 1 - out_mode
            elif cursor == 5:
                fmt_json = not fmt_json
        elif key == "enter":
            if cursor == 6:
                path = os.path.normpath(default_path) if out_mode else None
                return _TuiReportConfig(
                    hours=WINDOW_HOURS_PRESETS[hours_i],
                    orders_since_hours=ORDERS_WINDOW_PRESETS[orders_i],
                    fetch_mark=fetch,
                    out=path,
                    as_json=fmt_json,
                )
            if cursor == 7:
                return "back"
            if cursor == 8:
                return None


def _resolve_bot_row(conn: sqlite3.Connection, token: str) -> sqlite3.Row:
    t = token.strip()
    if not t:
        raise SystemExit("Empty --bot token")
    cur = conn.cursor()
    cur.execute("SELECT * FROM bots WHERE bot_id = ?", (t,))
    row = cur.fetchone()
    if row:
        return row
    cur.execute(
        """
        SELECT * FROM bots
        WHERE LOWER(TRIM(COALESCE(name, ''))) = LOWER(TRIM(?))
        ORDER BY bot_id
        """,
        (t,),
    )
    name_rows = cur.fetchall()
    if len(name_rows) == 1:
        return name_rows[0]
    if len(name_rows) > 1:
        raise SystemExit(
            f"Multiple bots named {t!r} - use bot_id:\n"
            + "\n".join(f"  {r['bot_id']}  {r['name']}" for r in name_rows[:20])
        )
    cur.execute(
        "SELECT * FROM bots WHERE bot_id LIKE ? ORDER BY created_at",
        (f"{t}%",),
    )
    rows = cur.fetchall()
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise SystemExit(f"No bot matches {t!r}")
    raise SystemExit(
        f"Ambiguous bot prefix {t!r}: {len(rows)} matches - use full bot_id:\n"
        + "\n".join(f"  {r['bot_id']}  {r['name']}" for r in rows[:15])
        + ("\n  ..." if len(rows) > 15 else "")
    )


def _fetch_orders_chronological(
    conn: sqlite3.Cursor,
    bot_id: str,
    since_ms: int | None,
) -> list[dict[str, Any]]:
    if since_ms is None:
        conn.execute(
            """
            SELECT order_row_id, exchange_order_id, side, amount, cost, average,
                   filled, created_at, symbol
            FROM bot_orders
            WHERE bot_id = ?
            ORDER BY created_at ASC, order_row_id ASC
            """,
            (bot_id,),
        )
    else:
        conn.execute(
            """
            SELECT order_row_id, exchange_order_id, side, amount, cost, average,
                   filled, created_at, symbol
            FROM bot_orders
            WHERE bot_id = ? AND created_at >= ?
            ORDER BY created_at ASC, order_row_id ASC
            """,
            (bot_id, since_ms),
        )
    return [dict(r) for r in conn.fetchall()]


def _fetch_decisions_window(
    conn: sqlite3.Cursor, bot_id: str, since_ms: int
) -> list[sqlite3.Row]:
    conn.execute(
        """
        SELECT d.action, d.confidence,
               COALESCE(d.created_at, t.timestamp) AS ts
        FROM bot_decisions d
        LEFT JOIN market_ticks t ON t.tick_id = d.tick_id
        WHERE d.bot_id = ?
          AND COALESCE(d.created_at, t.timestamp) IS NOT NULL
          AND COALESCE(d.created_at, t.timestamp) >= ?
        ORDER BY ts DESC
        """,
        (bot_id, since_ms),
    )
    return conn.fetchall()


def _fetch_voter_counts(
    conn: sqlite3.Cursor, bot_id: str, since_ms: int
) -> dict[str, dict[str, int]]:
    conn.execute(
        """
        SELECT voter_name, voter_signal, COUNT(*) AS n
        FROM voter_feedback
        WHERE bot_id = ? AND timestamp >= ?
        GROUP BY voter_name, voter_signal
        """,
        (bot_id, since_ms),
    )
    summary: dict[str, dict[str, int]] = defaultdict(
        lambda: {"buy": 0, "sell": 0, "hold": 0}
    )
    for r in conn.fetchall():
        summary[r["voter_name"]][r["voter_signal"]] = r["n"]
    return dict(summary)


def _fetch_feedback_signal_quality(
    conn: sqlite3.Cursor, bot_id: str, since_ms: int
) -> dict[str, Any]:
    """Mean forward ROC where labeled, grouped by ensemble_signal."""
    conn.execute(
        """
        SELECT ensemble_signal,
               COUNT(*) AS n,
               AVG(forward_roc_30s) AS m30,
               AVG(forward_roc_5m) AS m5m
        FROM voter_feedback
        WHERE bot_id = ?
          AND timestamp >= ?
          AND forward_roc_5m IS NOT NULL
        GROUP BY ensemble_signal
        """,
        (bot_id, since_ms),
    )
    rows = [dict(r) for r in conn.fetchall()]
    return {"by_ensemble": rows}


def _int0(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def fetch_metamagi_label_bundle(
    conn: sqlite3.Connection,
    bot_id: str,
    since_ms: int,
    *,
    recent_limit: int = 500,
) -> dict[str, Any]:
    """
    voter_feedback rows labeled by the MetaMagi loop (forward ROC from market_ticks).
    ``since_ms`` matches the report decision/voter window for summaries; ``recent`` rows
    are also limited to that window (newest first).
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            COUNT(*) AS n_total,
            SUM(CASE WHEN forward_roc_30s IS NOT NULL THEN 1 ELSE 0 END) AS n_lab_30s,
            SUM(CASE WHEN forward_roc_5m IS NOT NULL THEN 1 ELSE 0 END) AS n_lab_5m,
            SUM(
                CASE WHEN forward_roc_30s IS NOT NULL AND forward_roc_5m IS NOT NULL
                THEN 1 ELSE 0 END
            ) AS n_lab_both,
            SUM(CASE WHEN realized_pnl IS NOT NULL THEN 1 ELSE 0 END) AS n_lab_pnl,
            MIN(timestamp) AS ts_min,
            MAX(timestamp) AS ts_max
        FROM voter_feedback
        WHERE bot_id = ? AND timestamp >= ?
        """,
        (bot_id, since_ms),
    )
    win_raw = dict(cur.fetchone() or {})

    cur.execute(
        """
        SELECT
            COUNT(*) AS n_total,
            SUM(CASE WHEN forward_roc_30s IS NOT NULL THEN 1 ELSE 0 END) AS n_lab_30s,
            SUM(CASE WHEN forward_roc_5m IS NOT NULL THEN 1 ELSE 0 END) AS n_lab_5m,
            SUM(
                CASE WHEN forward_roc_30s IS NOT NULL AND forward_roc_5m IS NOT NULL
                THEN 1 ELSE 0 END
            ) AS n_lab_both,
            SUM(CASE WHEN realized_pnl IS NOT NULL THEN 1 ELSE 0 END) AS n_lab_pnl,
            MIN(timestamp) AS ts_min,
            MAX(timestamp) AS ts_max
        FROM voter_feedback
        WHERE bot_id = ?
        """,
        (bot_id,),
    )
    all_raw = dict(cur.fetchone() or {})

    def _norm_summary(d: dict[str, Any]) -> dict[str, Any]:
        nt = _int0(d.get("n_total"))
        l30 = _int0(d.get("n_lab_30s"))
        l5 = _int0(d.get("n_lab_5m"))
        lb = _int0(d.get("n_lab_both"))
        lp = _int0(d.get("n_lab_pnl"))
        return {
            "feedback_rows": nt,
            "labeled_forward_roc_30s": l30,
            "labeled_forward_roc_5m": l5,
            "labeled_both_horizons": lb,
            "rows_with_realized_pnl_column": lp,
            "pct_labeled_30s": round(100.0 * l30 / nt, 2) if nt else 0.0,
            "timestamp_min_ms": d.get("ts_min"),
            "timestamp_max_ms": d.get("ts_max"),
        }

    cur.execute(
        """
        SELECT voter_name AS voter,
               COUNT(*) AS rows_in_window,
               SUM(CASE WHEN forward_roc_30s IS NOT NULL THEN 1 ELSE 0 END) AS labeled_30s,
               AVG(forward_roc_30s) AS avg_roc_30s,
               AVG(forward_roc_5m) AS avg_roc_5m,
               AVG(realized_pnl) AS avg_realized_pnl_db
        FROM voter_feedback
        WHERE bot_id = ? AND timestamp >= ?
        GROUP BY voter_name
        ORDER BY voter_name
        """,
        (bot_id, since_ms),
    )
    by_voter = [dict(r) for r in cur.fetchall()]

    cur.execute(
        """
        SELECT feedback_id, timestamp, target_asset, ensemble_signal,
               voter_name, voter_signal, confidence, consensus_score,
               forward_roc_30s, forward_roc_5m, realized_pnl
        FROM voter_feedback
        WHERE bot_id = ? AND timestamp >= ?
          AND (
                forward_roc_30s IS NOT NULL
             OR forward_roc_5m IS NOT NULL
             OR realized_pnl IS NOT NULL
          )
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (bot_id, since_ms, max(1, recent_limit)),
    )
    recent: list[dict[str, Any]] = []
    for r in cur.fetchall():
        d = dict(r)
        ts = int(d["timestamp"])
        d["time_utc"] = datetime.fromtimestamp(
            ts / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")
        recent.append(d)

    return {
        "description": (
            "MetaMagi labels: forward_roc_30s and forward_roc_5m are filled by "
            "label_voter_feedback_forward_roc_batch from local market_ticks. "
            "MetaTrader.train_step uses forward_roc_30s with roc_threshold=0.0005 (see "
            "trading/metatrader.py) to update voter accuracy EMAs and dynamic weights. "
            "The voter_feedback.realized_pnl column is reserved but rarely filled."
        ),
        "window_since_ms": since_ms,
        "summary_in_window": _norm_summary(win_raw),
        "summary_all_time": _norm_summary(all_raw),
        "by_voter_in_window": by_voter,
        "labeled_rows_recent_newest_first": recent,
    }


def _trade_outcome_summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {"n": 0}
    pnls = [float(t["realized_pnl"]) for t in trades]
    wins = sum(1 for t in trades if t["outcome"] == "win")
    losses = sum(1 for t in trades if t["outcome"] == "loss")
    best = max(trades, key=lambda t: float(t["realized_pnl"]))
    worst = min(trades, key=lambda t: float(t["realized_pnl"]))
    return {
        "n": len(trades),
        "wins": wins,
        "losses": losses,
        "avg_pnl_quote": sum(pnls) / len(pnls),
        "best": {k: best[k] for k in ("timestamp", "realized_pnl", "outcome")},
        "worst": {k: worst[k] for k in ("timestamp", "realized_pnl", "outcome")},
    }


def _load_decision_events(conn: sqlite3.Cursor, bot_id: str) -> list[tuple[int, str, float | None]]:
    """(ts_ms, action_upper, confidence) sorted by ts ascending."""
    conn.execute(
        """
        SELECT UPPER(TRIM(COALESCE(d.action, 'HOLD'))) AS action,
               d.confidence,
               COALESCE(d.created_at, t.timestamp) AS ts
        FROM bot_decisions d
        LEFT JOIN market_ticks t ON t.tick_id = d.tick_id
        WHERE d.bot_id = ?
          AND COALESCE(d.created_at, t.timestamp) IS NOT NULL
        ORDER BY ts ASC
        """,
        (bot_id,),
    )
    out: list[tuple[int, str, float | None]] = []
    for r in conn.fetchall():
        try:
            ts = int(r["ts"])
        except (TypeError, ValueError):
            continue
        cf = r["confidence"]
        try:
            cf_f = float(cf) if cf is not None else None
        except (TypeError, ValueError):
            cf_f = None
        out.append((ts, str(r["action"] or "HOLD"), cf_f))
    return out


def _load_voter_batches(conn: sqlite3.Cursor, bot_id: str) -> list[dict[str, Any]]:
    """
    One entry per distinct voter_feedback timestamp (one ensemble cycle), ts ascending.
    """
    conn.execute(
        """
        SELECT timestamp, voter_name, confidence, consensus_score, ensemble_signal
        FROM voter_feedback
        WHERE bot_id = ?
        ORDER BY timestamp ASC, voter_name ASC
        """,
        (bot_id,),
    )
    rows = conn.fetchall()
    if not rows:
        return []
    batches: list[dict[str, Any]] = []
    i = 0
    while i < len(rows):
        ts = int(rows[i]["timestamp"])
        j = i
        confs: list[float] = []
        consensus: float | None = None
        ensemble: str | None = None
        voters: set[str] = set()
        while j < len(rows) and int(rows[j]["timestamp"]) == ts:
            r = rows[j]
            voters.add(str(r["voter_name"]))
            c = r["confidence"]
            if c is not None:
                try:
                    confs.append(float(c))
                except (TypeError, ValueError):
                    pass
            if consensus is None and r["consensus_score"] is not None:
                try:
                    consensus = float(r["consensus_score"])
                except (TypeError, ValueError):
                    pass
            if ensemble is None and r["ensemble_signal"] is not None:
                ensemble = str(r["ensemble_signal"])
            j += 1
        batches.append(
            {
                "timestamp": ts,
                "voter_count": len(voters),
                "avg_voter_confidence": (
                    sum(confs) / len(confs) if confs else None
                ),
                "consensus_score": consensus,
                "ensemble_signal": ensemble,
            }
        )
        i = j
    return batches


def _rightmost_batch_leq(batches: list[dict[str, Any]], t_ms: int) -> dict[str, Any] | None:
    if not batches or t_ms < batches[0]["timestamp"]:
        return None
    ts_list = [b["timestamp"] for b in batches]
    i = bisect.bisect_right(ts_list, t_ms) - 1
    return batches[i] if i >= 0 else None


def _pnl_map_for_sell_orders(
    orders: list[dict[str, Any]],
    closed_trades: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Map order_row_id -> FIFO closed trade (sell orders only, timestamp-aligned)."""
    from collections import deque

    q: deque[dict[str, Any]] = deque(closed_trades)
    out: dict[int, dict[str, Any]] = {}
    for o in orders:
        if str(o.get("side") or "").lower() != "sell" or not q:
            continue
        ots = int(o["created_at"])
        if ots != int(q[0]["timestamp"]):
            continue
        out[int(o["order_row_id"])] = q.popleft()
    return out


def build_transaction_ledger(
    conn: sqlite3.Connection,
    bot_id: str,
    orders: list[dict[str, Any]],
    closed_trades: list[dict[str, Any]],
    quote_ccy: str,
) -> list[dict[str, Any]]:
    decisions = _load_decision_events(conn.cursor(), bot_id)
    batches = _load_voter_batches(conn.cursor(), bot_id)
    pnl_by_sell = _pnl_map_for_sell_orders(orders, closed_trades)

    dec_i = 0
    n_dec = len(decisions)
    last_buy: tuple[int, float | None] | None = None
    last_sell: tuple[int, float | None] | None = None

    ledger: list[dict[str, Any]] = []
    for o in orders:
        ot = int(o["created_at"])
        while dec_i < n_dec and decisions[dec_i][0] <= ot:
            ts, act, cf = decisions[dec_i]
            if act == "BUY":
                last_buy = (ts, cf)
            elif act == "SELL":
                last_sell = (ts, cf)
            dec_i += 1

        side = str(o.get("side") or "").upper()
        if side == "BUY":
            d_st = last_buy
        elif side == "SELL":
            d_st = last_sell
        else:
            d_st = None

        vb = _rightmost_batch_leq(batches, ot)
        sell_pnl = pnl_by_sell.get(int(o["order_row_id"]))

        avg = o.get("average")
        cost = o.get("cost")
        filled = o.get("filled")
        amt = o.get("amount")
        try:
            px = float(avg) if avg is not None else None
        except (TypeError, ValueError):
            px = None
        if px is None and filled and cost:
            try:
                fn = float(filled)
                cn = float(cost)
                if fn > 0:
                    px = cn / fn
            except (TypeError, ValueError):
                pass

        row: dict[str, Any] = {
            "order_row_id": int(o["order_row_id"]),
            "exchange_order_id": o.get("exchange_order_id"),
            "time_utc": datetime.fromtimestamp(ot / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "created_at_ms": ot,
            "side": side.lower(),
            "base_qty": filled if filled is not None else amt,
            "avg_price": px,
            "quote_notional": cost,
            "quote_currency": quote_ccy,
            "decision_confidence": d_st[1] if d_st else None,
            "decision_ts_ms": d_st[0] if d_st else None,
            "voter_count": vb["voter_count"] if vb else None,
            "avg_voter_confidence": vb["avg_voter_confidence"] if vb else None,
            "consensus_score": vb["consensus_score"] if vb else None,
            "ensemble_signal_at_cycle": vb["ensemble_signal"] if vb else None,
            "voter_feedback_ts_ms": vb["timestamp"] if vb else None,
            "realized_pnl_quote": (
                float(sell_pnl["realized_pnl"]) if sell_pnl else None
            ),
            "fifo_outcome": (sell_pnl.get("outcome") if sell_pnl else None),
            "matched_base_closed": (sell_pnl.get("quantity") if sell_pnl else None),
        }
        ledger.append(row)
    return ledger


def _print_bot_list(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT bot_id, name, symbol, strategy, status, execution_mode, created_at "
        "FROM bots ORDER BY created_at DESC"
    ).fetchall()
    print(f"\n{'bot_id':<18} {'status':<10} {'mode':<8} {'symbol':<14} name / strategy")
    print("-" * 100)
    for r in rows:
        print(
            f"{r['bot_id']:<18} {r['status']:<10} {r['execution_mode']:<8} "
            f"{r['symbol']:<14} {r['name']} ({r['strategy']})"
        )
    print(f"\nTotal: {len(rows)} bots\n")


def _build_report_text(
    *,
    bot: sqlite3.Row,
    risk: dict[str, Any],
    params: dict[str, Any],
    budget: float | None,
    perf_all: dict[str, Any],
    perf_orders_window: dict[str, Any] | None,
    decisions: list[sqlite3.Row],
    voter: dict[str, dict[str, int]],
    feedback_q: dict[str, Any],
    closed_all: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
    metamagi: dict[str, Any],
    hours: float,
    mark_price: float | None,
) -> str:
    lines: list[str] = []
    sym = str(bot["symbol"] or "")
    bid = bot["bot_id"]

    def ln(s: str = "") -> None:
        lines.append(s)

    ln("=" * 72)
    ln("MagiTrader - Bot strategy report")
    ln(f"Generated: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    ln("=" * 72)

    ln("\n## Summary")
    ln(f"  Name     : {bot['name']}")
    ln(f"  Bot ID   : {bid}")
    ln(f"  Status   : {bot['status']}  |  Execution: {bot['execution_mode']}")
    ln(f"  Symbol   : {sym}")
    ln(f"  Strategy : {bot['strategy']}")
    ln(f"  Created  : {_ts_ms(bot['created_at'])}")
    if bot["started_at"]:
        ln(f"  Started  : {_ts_ms(bot['started_at'])}")

    ln("\n## Strategy parameters")
    ln(json.dumps(params, indent=2, default=str))

    ln("\n## Risk & sizing (effective)")
    risk_redacted = {k: v for k, v in risk.items() if not k.endswith("_json")}
    ln(json.dumps(risk_redacted, indent=2, default=str))

    ln("\n## Performance & ROI (FIFO on all orders in DB)")
    ln(f"  Quote currency : {perf_all['quote_currency']}")
    ln(
        f"  Realized PnL   : {perf_all['realized_pnl_quote']:.6f} "
        f"{perf_all['quote_currency']}"
    )
    u = perf_all["unrealized_pnl_quote"]
    if u is not None:
        ln(f"  Unrealized PnL : {u:.6f} (mark={mark_price})")
    else:
        ln("  Unrealized PnL : n/a (no mark price or flat)")
    total = perf_all["realized_pnl_quote"] + (u or 0.0)
    ln(f"  Total PnL      : {total:.6f}")
    ln(f"  Open base      : {perf_all['open_base_position']:.8f}")
    ln(f"  Open costbasis : {perf_all['open_cost_basis_quote']:.6f}")
    ln(
        f"  Closed rounds  : {perf_all['closed_trades']} "
        f"(W {perf_all['winning_trades']} / L {perf_all['losing_trades']} / "
        f"flat {perf_all['breakeven_trades']})"
    )
    wr = perf_all["win_rate_pct"]
    ln(f"  Win rate       : {wr:.2f}%" if wr is not None else "  Win rate       : n/a")
    ln(f"  Max DD (quote) : {perf_all['max_drawdown_quote']:.6f}")
    mdp = perf_all["max_drawdown_pct"]
    ln(f"  Max DD (peak%) : {mdp:.4f}" if mdp is not None else "  Max DD (peak%) : n/a")

    if budget and budget > 0:
        ret = (total / budget) * 100.0
        ddr = (perf_all["max_drawdown_quote"] / budget) * 100.0
        ln(f"  Initial budget : {budget:.6f} {perf_all['quote_currency']}")
        ln(f"  ROI vs budget  : {ret:.4f}%")
        ln(f"  Max DD vs budg : {ddr:.4f}%")
    else:
        ln("  Initial budget : not set in strategy params - ROI vs budget skipped")
        ln("                 (set initial_budget_quote in JSON to track ROI %)")

    to = _trade_outcome_summary(closed_all)
    if to["n"]:
        ln("\n  Closed-trade highlights (all time)")
        ln(
            f"    Avg PnL / trade: {to['avg_pnl_quote']:.6f}  "
            f"wins={to['wins']} losses={to['losses']}"
        )
        ln(
            f"    Best : {to['best']['realized_pnl']} @ {_ts_ms(to['best']['timestamp'])}"
        )
        ln(
            f"    Worst: {to['worst']['realized_pnl']} @ {_ts_ms(to['worst']['timestamp'])}"
        )

    ln("\n## All transactions (spot orders, chronological)")
    ln(
        "  voter_* / ensemble_* = last voter_feedback batch at or before order time; "
        "decision_confidence = last bot_decision matching this side at or before order."
    )
    ln(
        "  realized_pnl = FIFO round-trip on sells only (matched by order timestamp)."
    )
    if not transactions:
        ln("  (no orders)")
    else:
        ln(
            f"  {'time_utc':<20} {'side':<5} {'base':>12} {'px':>10} "
            f"{'#V':>4} {'vAvgC':>6} {'cons':>6} {'decC':>6} {'ens':<6} "
            f"{'PnL':>10} {'fifo':<5}"
        )
        ln("  " + "-" * 102)
        for tx in transactions:
            dc = tx.get("decision_confidence")
            dc_s = f"{dc:.4f}" if isinstance(dc, (int, float)) else "-"
            avc = tx.get("avg_voter_confidence")
            avc_s = f"{avc:.4f}" if isinstance(avc, (int, float)) else "-"
            cs = tx.get("consensus_score")
            cs_s = f"{cs:.4f}" if isinstance(cs, (int, float)) else "-"
            nv = tx.get("voter_count")
            nv_s = str(nv) if nv is not None else "-"
            ens = (tx.get("ensemble_signal_at_cycle") or "-")[:6]
            pnl = tx.get("realized_pnl_quote")
            pnl_s = f"{pnl:.6f}" if isinstance(pnl, (int, float)) else "-"
            fo = tx.get("fifo_outcome") or "-"
            bq = tx.get("base_qty")
            bqs = f"{float(bq):.6f}" if bq is not None else "-"
            ap = tx.get("avg_price")
            aps = f"{float(ap):.4f}" if ap is not None else "-"
            ln(
                f"  {tx.get('time_utc', '-'):<20} {tx.get('side', '-'):<5} "
                f"{bqs:>12} {aps:>10} {nv_s:>4} {avc_s:>6} {cs_s:>6} {dc_s:>6} "
                f"{ens:<6} {pnl_s:>10} {str(fo):<5}"
            )

    if perf_orders_window is not None:
        ln("\n## Orders-only window (informational)")
        ln(
            "  PnL below is FIFO run only on orders with created_at in the window. "
            "If trading continued across the boundary, this is not full-period accounting."
        )
        ln(
            f"  Realized (window orders): "
            f"{perf_orders_window['realized_pnl_quote']:.6f} "
            f"{perf_orders_window['quote_currency']}"
        )

    wh = f"{hours:.0f}h" if hours >= 1 else f"{int(hours * 60)}m"
    ln(f"\n## Decisions in last {wh}")
    counts: dict[str, int] = {"BUY": 0, "SELL": 0, "HOLD": 0}
    confs: list[float] = []
    for d in decisions:
        a = (d["action"] or "HOLD").upper()
        counts[a] = counts.get(a, 0) + 1
        if d["confidence"] is not None:
            confs.append(float(d["confidence"]))
    tot = len(decisions)
    if tot == 0:
        ln("  (none)")
    else:
        ln(f"  Total: {tot}  BUY {counts.get('BUY', 0)}  SELL {counts.get('SELL', 0)}  "
            f"HOLD {counts.get('HOLD', 0)}")
        hold_pct = counts.get("HOLD", 0) / tot
        ln(f"  Non-HOLD share: {_pct(tot - counts.get('HOLD', 0), tot)}")
        ln(f"  Activity note: {_activity_label(hold_pct)}")
        if confs:
            ln(f"  Avg confidence: {sum(confs) / len(confs):.4f}")

    ln(f"\n## Voter feedback (last {wh})")
    if not voter:
        ln("  (no rows)")
    else:
        voters = params.get("voters") or sorted(voter.keys())
        ln(f"  {'Voter':<28} {'buy':>6} {'sell':>6} {'hold':>6}  active%")
        ln("  " + "-" * 56)
        for name in voters:
            vc = voter.get(name, {"buy": 0, "sell": 0, "hold": 0})
            vt = sum(vc.values())
            active = _pct(vc["buy"] + vc["sell"], vt) if vt else "n/a"
            flag = "  * mostly idle" if vt and (vc["buy"] + vc["sell"]) / vt < 0.05 else ""
            ln(
                f"  {name:<28} {vc['buy']:>6} {vc['sell']:>6} {vc['hold']:>6}  "
                f"{active:>6}{flag}"
            )

    be = feedback_q.get("by_ensemble") or []
    if be:
        ln(f"\n## Labeled forward ROC (ensemble to 5m, last {wh})")
        ln("  " f"{'signal':<12} {'n':>6} {'avg ROC 30s':>14} {'avg ROC 5m':>14}")
        ln("  " + "-" * 50)
        for r in be:
            m30 = r["m30"]
            m5 = r["m5m"]
            ln(
                f"  {str(r['ensemble_signal']):<12} {r['n']:>6} "
                f"{(m30 if m30 is not None else 0)*100:>13.4f}% "
                f"{(m5 if m5 is not None else 0)*100:>13.4f}%"
            )

    sw = metamagi.get("summary_in_window") or {}
    sa = metamagi.get("summary_all_time") or {}
    ln(f"\n## MetaMagi labeled data (voter_feedback, last {wh})")
    ln(f"  {metamagi.get('description', '')}")
    ln(
        f"  Window rows: {sw.get('feedback_rows', 0)}  "
        f"labeled 30s: {sw.get('labeled_forward_roc_30s', 0)} "
        f"({sw.get('pct_labeled_30s', 0):.1f}%)  "
        f"labeled 5m: {sw.get('labeled_forward_roc_5m', 0)}"
    )
    ln(
        f"  All-time rows: {sa.get('feedback_rows', 0)}  "
        f"labeled 30s: {sa.get('labeled_forward_roc_30s', 0)}  "
        f"labeled 5m: {sa.get('labeled_forward_roc_5m', 0)}"
    )
    bv = metamagi.get("by_voter_in_window") or []
    if bv:
        ln(f"\n  Per voter (last {wh}, aggregates on labeled rows):")
        ln(
            f"  {'voter':<22} {'rows':>6} {'lab30':>6} "
            f"{'avgROC30s%':>11} {'avgROC5m%':>11}"
        )
        ln("  " + "-" * 62)
        for r in bv:
            rv = str(r.get("voter") or "")
            nr = int(r.get("rows_in_window") or 0)
            l3 = int(r.get("labeled_30s") or 0)
            a30 = r.get("avg_roc_30s")
            a5 = r.get("avg_roc_5m")
            a30p = (float(a30) * 100.0) if a30 is not None else 0.0
            a5p = (float(a5) * 100.0) if a5 is not None else 0.0
            ln(
                f"  {rv:<22} {nr:>6} {l3:>6} {a30p:>11.4f} {a5p:>11.4f}"
            )
    recent = metamagi.get("labeled_rows_recent_newest_first") or []
    show_n = 15
    if recent:
        ln(f"\n  Recent labeled rows (newest {min(show_n, len(recent))} of {len(recent)}):")
        ln(
            f"  {'time_utc':<20} {'voter':<14} {'vote':<5} "
            f"{'ens':<5} {'roc30s%':>9} {'roc5m%':>9}"
        )
        ln("  " + "-" * 72)
        for row in recent[:show_n]:
            r30 = row.get("forward_roc_30s")
            r5 = row.get("forward_roc_5m")
            r30p = f"{float(r30)*100:.4f}" if r30 is not None else "-"
            r5p = f"{float(r5)*100:.4f}" if r5 is not None else "-"
            ens = str(row.get("ensemble_signal") or "-")[:5]
            ln(
                f"  {row.get('time_utc', '-'):<20} "
                f"{str(row.get('voter_name') or '')[:14]:<14} "
                f"{str(row.get('voter_signal') or '')[:5]:<5} {ens:<5} "
                f"{r30p:>9} {r5p:>9}"
            )

    ln("\n## Ideas for strategy iteration")
    ideas: list[str] = []
    if tot > 0:
        hp = counts.get("HOLD", 0) / tot
        if hp > 0.92:
            ideas.append(
                "Very high HOLD rate - try lower consensus_threshold, different "
                "consensus_mode, or shorter timeframe voters."
            )
    if wr is not None and perf_all["closed_trades"] >= 5:
        if wr < 40:
            ideas.append(
                "Low win rate with enough trades - review exit timing, "
                "risk sizing, or voters that disagree with recent regime."
            )
        elif wr > 65 and perf_all["realized_pnl_quote"] < 0:
            ideas.append(
                "High win rate but negative realized - average loss may exceed "
                "average win; check position sizing and tail losses."
            )
    mdpv = perf_all["max_drawdown_pct"]
    if mdpv is not None and mdpv > 25:
        ideas.append(
            "Large drawdown vs peak realized curve - consider tighter "
            "daily_loss_limit_pct / max_drawdown_pct or reducing base_risk_pct."
        )
    if budget and budget > 0:
        ret = (total / budget) * 100.0
        if ret < -10:
            ideas.append(
                "ROI vs budget significantly negative - paper-trade parameter "
                "swaps or fork bot and A/B in backtests."
            )
    if not ideas:
        ideas.append(
            "No automatic flags - compare labeled forward ROC by ensemble signal "
            "and align voter weights (MetaMagi) if data is thin."
        )
    for i, s in enumerate(ideas, 1):
        ln(f"  {i}. {s}")

    ln("\n" + "=" * 72)
    return "\n".join(lines)


def _report_data_payload(
    *,
    bot: sqlite3.Row,
    params: dict[str, Any],
    risk: dict[str, Any],
    perf_all: dict[str, Any],
    perf_win: dict[str, Any] | None,
    budget: float | None,
    mark_price: float | None,
    closed_all: list[dict[str, Any]],
    hours: float,
    decisions: list[sqlite3.Row],
    voter: dict[str, dict[str, int]],
    feedback_q: dict[str, Any],
    transactions: list[dict[str, Any]],
    metamagi: dict[str, Any],
) -> dict[str, Any]:
    unreal = perf_all["unrealized_pnl_quote"]
    total_pnl = perf_all["realized_pnl_quote"] + (unreal or 0.0)
    return {
        "schema_version": 1,
        "document_purpose": (
            "Structured MagiTrader bot snapshot for LLM review: tune strategy params, "
            "risk, voters, and consensus using factual DB fields. Quote currency and FIFO "
            "PNL follow bot_performance semantics. metamagi_labeled_data is voter_feedback "
            "rows labeled with forward ROC for MetaMagi / MetaTrader."
        ),
        "bot": dict(bot),
        "strategy_params": params,
        "risk_settings": risk,
        "performance_all_orders": perf_all,
        "performance_orders_in_window": perf_win,
        "initial_budget_quote": budget,
        "pnl_return_on_budget_pct": round((total_pnl / budget) * 100.0, 6)
        if budget and budget > 0
        else None,
        "mark_price": mark_price,
        "closed_trades_summary": _trade_outcome_summary(closed_all),
        "closed_trades_fifo": closed_all,
        "decision_window_hours": hours,
        "decisions_sampled": len(decisions),
        "voter_counts_window": voter,
        "feedback_forward_roc": feedback_q,
        "metamagi_labeled_data": metamagi,
        "transactions": transactions,
    }


def _pdf_safe_line(s: str) -> str:
    """Helvetica-safe single line for fpdf core fonts."""
    return s.encode("cp1252", errors="replace").decode("cp1252")


def _pdf_safe_body(body: str) -> str:
    """Full report text as WinAnsi-safe string, preserving newlines."""
    lines_out: list[str] = []
    for line in body.split("\n"):
        raw = line if line.strip() else " "
        chunk = _pdf_safe_line(raw)
        if not chunk.strip():
            chunk = " "
        lines_out.append(chunk)
    return "\n".join(lines_out)


def _write_report_pdf(path: str, *, title: str, body: str) -> bool:
    """Return True if written, False if fpdf2 missing."""
    try:
        from fpdf import FPDF  # type: ignore[import-untyped]
        from fpdf.enums import Align  # type: ignore[import-untyped]
    except ImportError:
        print(
            "[warn] PDF export skipped: install fpdf2 (pip install fpdf2).",
            file=sys.stderr,
        )
        return False
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_left_margin(12)
    pdf.set_right_margin(12)
    pdf.add_page()
    pdf.set_text_color(0, 0, 0)
    # w=0: full width to the right margin (avoids edge cases with epw + wrapping).
    pdf.set_font("Helvetica", "B", 12)
    pdf.multi_cell(0, 6, _pdf_safe_line(title), align=Align.L, markdown=False)
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(0, 5, _pdf_safe_body(body), align=Align.L, markdown=False)
    pdf.output(path)
    return True


def run_bot_report(
    conn: sqlite3.Connection,
    bot: sqlite3.Row,
    *,
    hours: float,
    orders_since_hours: float | None,
    fetch_mark: bool,
    out: str | None,
    as_json: bool,
    bundle_dir: str | None = None,
) -> None:
    """Load data from `conn`, print or write report. Does not close `conn`.

    If ``bundle_dir`` is set (e.g. ``reports``), writes
    ``strategy_report_<stub>.pdf``, ``strategy_report_<stub>.txt``, and
    ``strategy_report_<stub>_for_llm.json`` there
    (in addition to normal stdout / ``out`` handling).
    """
    bot_id = bot["bot_id"]
    sym = str(bot["symbol"] or "")

    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    since_ms = int(now_ms - hours * 3600 * 1000)
    orders_since_ms: int | None = None
    if orders_since_hours is not None:
        orders_since_ms = int(now_ms - orders_since_hours * 3600 * 1000)

    cur = conn.cursor()
    orders_all = _fetch_orders_chronological(cur, bot_id, None)
    orders_win = (
        _fetch_orders_chronological(cur, bot_id, orders_since_ms)
        if orders_since_ms is not None
        else None
    )

    mark_price: float | None = None
    if fetch_mark and sym:
        try:
            from trading.exchange_factory import build_binance_spot  # noqa: E402

            mode = str(bot["execution_mode"] or "testnet")
            ex = build_binance_spot(mode)
            ex.load_markets()
            tk = ex.fetch_ticker(sym)
            lp = tk.get("last")
            if lp is not None:
                mark_price = float(lp)
        except Exception as exc:
            print(f"[warn] fetch-mark failed: {exc}", file=sys.stderr)

    perf_all = compute_strategy_performance(orders_all, sym, mark_price=mark_price)
    perf_win = (
        compute_strategy_performance(orders_win, sym, mark_price=mark_price)
        if orders_win is not None
        else None
    )
    closed_all = compute_closed_trades(orders_all, sym)

    transactions = build_transaction_ledger(
        conn,
        bot_id,
        orders_all,
        closed_all,
        str(perf_all.get("quote_currency") or "USDT"),
    )

    raw_params = bot["strategy_params_json"]
    params = (
        json.loads(raw_params) if isinstance(raw_params, str) and raw_params.strip() else {}
    )
    budget = initial_budget_from_strategy_params_json(
        raw_params if isinstance(raw_params, str) else None
    )

    risk = get_effective_bot_risk_settings(bot_id)
    decisions = _fetch_decisions_window(cur, bot_id, since_ms)
    voter = _fetch_voter_counts(cur, bot_id, since_ms)
    feedback_q = _fetch_feedback_signal_quality(cur, bot_id, since_ms)
    metamagi = fetch_metamagi_label_bundle(conn, bot_id, since_ms)

    payload = _report_data_payload(
        bot=bot,
        params=params,
        risk=risk,
        perf_all=perf_all,
        perf_win=perf_win,
        budget=budget,
        mark_price=mark_price,
        closed_all=closed_all,
        hours=hours,
        decisions=decisions,
        voter=voter,
        feedback_q=feedback_q,
        transactions=transactions,
        metamagi=metamagi,
    )

    need_text = (not as_json) or bool(bundle_dir)
    text: str | None
    if need_text:
        text = _build_report_text(
            bot=bot,
            risk=risk,
            params=params,
            budget=budget,
            perf_all=perf_all,
            perf_orders_window=perf_win,
            decisions=decisions,
            voter=voter,
            feedback_q=feedback_q,
            closed_all=closed_all,
            transactions=transactions,
            metamagi=metamagi,
            hours=hours,
            mark_price=mark_price,
        )
    else:
        text = None

    if bundle_dir:
        stub = _safe_report_filename_stub(bot)
        out_dir = os.path.normpath(bundle_dir)
        os.makedirs(out_dir, exist_ok=True)
        base = os.path.join(out_dir, f"strategy_report_{stub}")
        llm_path = f"{base}_for_llm.json"
        pdf_path = f"{base}.pdf"
        txt_path = f"{base}.txt"
        with open(llm_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
        pdf_ok = False
        if text:
            bdict = dict(bot)
            title = (
                f"MagiTrader bot report - {bdict.get('name') or bot_id} "
                f"({sym})  generated {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
            )
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"Wrote {txt_path}")
            pdf_ok = _write_report_pdf(pdf_path, title=title, body=text)
        print(f"Wrote {llm_path}")
        if pdf_ok:
            print(f"Wrote {pdf_path}")
        elif text:
            print("(PDF not created — see warning above)", file=sys.stderr)

    if as_json:
        print(json.dumps(payload, indent=2, default=str))
        return

    assert text is not None
    if out:
        os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Wrote {out}")
    else:
        print(text)


def main() -> None:
    p = argparse.ArgumentParser(
        description="Print a consolidated bot report (settings, risk, PnL, ROI, behavior)."
    )
    p.add_argument(
        "--ui",
        action="store_true",
        help="Interactive terminal UI: pick bot by name, then tune options and run.",
    )
    p.add_argument("--list", action="store_true", help="List bots and exit")
    p.add_argument("--bot", type=str, default=None, help="bot_id, unique id prefix, or exact name")
    p.add_argument(
        "--hours",
        type=float,
        default=24.0,
        help="Decision / voter_feedback window in hours (default: 24)",
    )
    p.add_argument(
        "--orders-since-hours",
        type=float,
        default=None,
        help="If set, second FIFO block uses only orders in this window (informational).",
    )
    p.add_argument(
        "--fetch-mark",
        action="store_true",
        help="Fetch last price from Binance for unrealized PnL (needs network + ccxt).",
    )
    p.add_argument("--out", type=str, default=None, help="Write text report to this path")
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit one JSON object on stdout instead of text (ignores --out)",
    )
    p.add_argument(
        "--bundle-dir",
        type=str,
        default=None,
        metavar="DIR",
        help="Also write strategy_report_<stub>.pdf, .txt, and _for_llm.json under DIR.",
    )
    args = p.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if args.list:
        _print_bot_list(conn)
        conn.close()
        return

    preset = _TuiReportConfig(
        hours=args.hours,
        orders_since_hours=args.orders_since_hours,
        fetch_mark=bool(args.fetch_mark),
        out=args.out,
        as_json=bool(args.json),
    )

    if args.ui:
        cli_bot: str | None = args.bot
        try:
            while True:
                if cli_bot:
                    bot = _resolve_bot_row(conn, cli_bot)
                    cli_bot = None
                else:
                    picked = _tui_pick_bot(conn)
                    if picked == "quit" or picked is None:
                        return
                    bot = picked
                cfg_o = _tui_configure_options(bot, preset)
                if cfg_o is None:
                    return
                if cfg_o == "back":
                    continue
                run_bot_report(
                    conn,
                    bot,
                    hours=cfg_o.hours,
                    orders_since_hours=cfg_o.orders_since_hours,
                    fetch_mark=cfg_o.fetch_mark,
                    out=cfg_o.out,
                    as_json=cfg_o.as_json,
                    bundle_dir="reports",
                )
                preset = cfg_o
                if _stdin_is_tty():
                    _prompt_line("\nPress Enter to return to the bot list...")
                else:
                    break
        finally:
            conn.close()
        return

    if not args.bot:
        p.error("Provide --bot <id_or_prefix>, or use --list / --ui")

    bot = _resolve_bot_row(conn, args.bot)
    try:
        run_bot_report(
            conn,
            bot,
            hours=args.hours,
            orders_since_hours=args.orders_since_hours,
            fetch_mark=args.fetch_mark,
            out=args.out,
            as_json=args.json,
            bundle_dir=args.bundle_dir,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
