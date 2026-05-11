"""
Export MetaMagi labeled training rows (`voter_feedback`) for LLM-assisted analysis.

Produces JSON (and optional human-readable text) describing the schema, aggregates
per voter/bot, MetaTrader-style accuracy on forward_roc_30s, and optional row
samples. Use this to reason about static `voter_weights` in strategy_params;
runtime MetaMagi still applies in-process EMAs on top of those bases (see
`trading/metatrader.py`).

Usage:
    python scripts/metamagi_labeled_export.py --list
    python scripts/metamagi_labeled_export.py --ui
        # TUI: pick a bot (or ALL BOTS); writes reports/metamagi_labeled_*_for_llm.json + .txt
    python scripts/metamagi_labeled_export.py --bot ab12cd34 --hours 168
    python scripts/metamagi_labeled_export.py --hours 720 --sample-rows 80
    python scripts/metamagi_labeled_export.py --bot mybot --bundle-dir reports
        # --weight-method edge|accuracy|both (default edge; JSON includes accuracy, edge, and blended maps)
        # --blend-alpha 0.65  (blend for suggested_blended_weights; default 0.65 = 65% edge + 35% accuracy)

Reads the same SQLite DB as the app (`database.DB_PATH`). No network calls.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from database import DB_PATH  # noqa: E402

# Default directory for `python scripts/metamagi_labeled_export.py --ui`
TUI_BUNDLE_DIR = "reports"

# Same hour presets as scripts/bot_strategy_report.py
WINDOW_HOURS_PRESETS: list[float] = [1.0, 6.0, 12.0, 24.0, 72.0, 168.0, 720.0]
SAMPLE_ROWS_PRESETS: list[int] = [0, 30, 60, 100, 200, 500]
ROC_THRESHOLD_PRESETS: list[float] = [0.0002, 0.0003, 0.0005, 0.0008, 0.001]

# Edge weights: voters with fewer than this many buy+sell labeled rows use accuracy weights
# instead (means are too noisy vs. flat/hold-heavy participation).
_EDGE_MIN_DIRECTIONAL_SAMPLES: int = 15

# Winsorize per-voter mean edge scores (forward ROC × direction) before normalizing —
# shields weights from absurd single-voter outliers from labeling glitches / fat tails.
_EDGE_WINSORIZE_Q_LOW = 0.05
_EDGE_WINSORIZE_Q_HIGH = 0.95


def labeled_table_schema() -> dict[str, Any]:
    """Static description of `voter_feedback` for LLM context."""
    return {
        "table": "voter_feedback",
        "grain": (
            "One row per voter per ensemble cycle. All voters for the same bot "
            "share the same `timestamp`, `ensemble_signal`, and `target_asset`."
        ),
        "columns": [
            {
                "name": "feedback_id",
                "type": "integer",
                "role": "Primary key.",
            },
            {
                "name": "bot_id",
                "type": "text",
                "role": "Bot that produced the vote (nullable in legacy rows).",
            },
            {
                "name": "timestamp",
                "type": "integer (ms since Unix epoch)",
                "role": "Cycle time; same for all voters in one ensemble decision.",
            },
            {
                "name": "target_asset",
                "type": "text",
                "role": "Symbol key used with market_ticks (e.g. BTCUSDT).",
            },
            {
                "name": "ensemble_signal",
                "type": "text",
                "role": "Consensus outcome that cycle: buy | sell | hold.",
            },
            {
                "name": "voter_name",
                "type": "text",
                "role": "Strategy voter id (must match ensemble registry).",
            },
            {
                "name": "voter_signal",
                "type": "text",
                "role": "That voter's vote: buy | sell | hold.",
            },
            {
                "name": "confidence",
                "type": "real | null",
                "role": "Voter confidence if the strategy provides it.",
            },
            {
                "name": "consensus_score",
                "type": "real | null",
                "role": "Fraction of weight behind winning signal that cycle.",
            },
            {
                "name": "forward_roc_30s",
                "type": "real | null",
                "role": (
                    "Labeled forward return (ROC) ~30s after timestamp from "
                    "local market_ticks; null until MetaMagi labeling runs."
                ),
            },
            {
                "name": "forward_roc_5m",
                "type": "real | null",
                "role": "Same as 30s but ~5 minute horizon.",
            },
            {
                "name": "realized_pnl",
                "type": "real | null",
                "role": "Reserved; usually null (not trade-attributed PnL).",
            },
            {
                "name": "features_snapshot",
                "type": "text (JSON) | null",
                "role": (
                    "Optional JSON feature vector at decision time; can be large. "
                    "Exports may include only keys or omit."
                ),
            },
        ],
        "labels": {
            "how_populated": (
                "Background task calls `label_voter_feedback_forward_roc_batch` "
                "(`backend/database.py`), joining voter_feedback to market_ticks. "
                "No exchange calls."
            ),
            "labeled_row": "Row with non-null `forward_roc_30s` (typically 5m filled too).",
        },
        "metatrader_correctness_rule": {
            "roc_threshold": (
                "Default 0.0005 (5 bps): |forward_roc_30s| below this counts as "
                "a flat market — only voter_signal=='hold' is correct."
            ),
            "directional": (
                "If forward_roc_30s > threshold effective: buy correct; "
                "if forward_roc_30s < -threshold: sell correct. "
                "(Implementation: abs(roc) < threshold → hold; elif roc > 0 → buy; else sell.)"
            ),
            "reference_code": "backend/trading/metatrader.py — MetaTrader._is_correct",
        },
        "ensemble_weighting_reference": (
            "Base weights: strategy_params_json.voter_weights. Each cycle, "
            "consensus merges base_weights with MetaTrader.get_dynamic_weights() "
            "(in-memory EMA accuracy, same correctness rule). "
            "See trading/strategies/ensemble_core.py."
        ),
    }


def _slug(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")[:48] or "export"


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
    )


def _print_bot_list(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT name, bot_id, symbol, status
        FROM bots
        ORDER BY LOWER(TRIM(COALESCE(name, ''))), bot_id
        """
    )
    print("bots:")
    for r in cur.fetchall():
        nm = (r["name"] or "").strip() or "(unnamed)"
        print(f"  {r['bot_id']}  {nm}  {r['symbol'] or ''}  {r['status'] or ''}")


# --- Terminal UI (same interaction style as bot_strategy_report.py) ---


def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False


_BUNDLE_WRITE_MSG = (
    "Writes reports/metamagi_labeled_<stub>_<ts>_for_llm.json + .txt"
)


def _read_key_interactive() -> str:
    """Return logical key: up, down, left, right, enter, escape, space, quit."""
    if sys.platform == "win32":
        import msvcrt

        c = msvcrt.getwch()
        if c in ("\x00", "\xe0"):
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


def _tui_pick_bot_or_all(
    conn: sqlite3.Connection,
) -> sqlite3.Row | Literal["all"] | Literal["quit"] | None:
    """List every bot plus an ALL BOTS aggregate option (arrow keys or numbered input)."""
    rows = _fetch_bots_sorted_by_name(conn)
    if not rows:
        print("\nNo bots in the database.\n")
        return None

    labels = [
        "[ ALL BOTS - aggregate voter_feedback across every bot ]",
    ] + _bot_picker_labels(rows)
    title = "Select bot for MetaMagi labeled export"
    help_line = (
        "Up/Down  move   Enter  select   Q  quit"
        if _stdin_is_tty()
        else f"Enter a number 1-{len(labels)} (empty=cancel; 1 = all bots)"
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
        if n == 1:
            return "all"
        if 2 <= n <= len(labels):
            return rows[n - 2]
        return None

    idx = 0
    while True:
        _tui_clear()
        print(title)
        print(help_line)
        print(_BUNDLE_WRITE_MSG)
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
            if idx == 0:
                return "all"
            return rows[idx - 1]


@dataclass
class _TuiExportConfig:
    hours: float
    all_time: bool
    sample_rows: int
    roc_threshold: float
    include_feature_keys: bool


def _nearest_float_idx(presets: list[float], value: float) -> int:
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


def _nearest_int_idx(presets: list[int], value: int) -> int:
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


def _tui_configure_export(
    target: sqlite3.Row | Literal["all"],
    preset: _TuiExportConfig,
) -> Literal["back"] | _TuiExportConfig | None:
    """Options menu: back = bot list, None = quit."""
    hours_i = _nearest_float_idx(WINDOW_HOURS_PRESETS, preset.hours)
    sample_i = _nearest_int_idx(SAMPLE_ROWS_PRESETS, preset.sample_rows)
    roc_i = _nearest_float_idx(ROC_THRESHOLD_PRESETS, preset.roc_threshold)
    all_time = preset.all_time
    feat = preset.include_feature_keys
    cursor = 0
    n_rows = 9

    if not _stdin_is_tty():
        _tui_clear()
        tgt = (
            "ALL BOTS"
            if target == "all"
            else str((target["name"] or target["bot_id"]) or "")
        )
        print(f"MetaMagi export options  --  {tgt}\n")
        at = _prompt_line("Use full table (all time)? y/N: ").lower()
        all_time = at in ("y", "yes")
        if not all_time:
            for i, h in enumerate(WINDOW_HOURS_PRESETS):
                print(f"  {i + 1}. {h:g} h window")
            hi = _prompt_line(
                f"Window hours [default nearest {preset.hours:g}h = {hours_i + 1}]: "
            )
            if hi.strip().isdigit():
                hours_i = max(
                    0, min(len(WINDOW_HOURS_PRESETS) - 1, int(hi) - 1)
                )
        for i, s in enumerate(SAMPLE_ROWS_PRESETS):
            print(f"  sample rows {i + 1}. {s}")
        si = _prompt_line(f"Sample rows preset [1-{len(SAMPLE_ROWS_PRESETS)}, default {sample_i + 1}]: ")
        if si.strip().isdigit():
            sample_i = max(0, min(len(SAMPLE_ROWS_PRESETS) - 1, int(si) - 1))
        for i, r in enumerate(ROC_THRESHOLD_PRESETS):
            print(f"  ROC threshold {i + 1}. {r}")
        ri = _prompt_line(f"ROC threshold [1-{len(ROC_THRESHOLD_PRESETS)}, default {roc_i + 1}]: ")
        if ri.strip().isdigit():
            roc_i = max(0, min(len(ROC_THRESHOLD_PRESETS) - 1, int(ri) - 1))
        fk = _prompt_line("Include feature key names in samples? y/N: ").lower()
        feat = fk in ("y", "yes")
        return _TuiExportConfig(
            hours=WINDOW_HOURS_PRESETS[hours_i],
            all_time=all_time,
            sample_rows=SAMPLE_ROWS_PRESETS[sample_i],
            roc_threshold=ROC_THRESHOLD_PRESETS[roc_i],
            include_feature_keys=feat,
        )

    help_line = (
        "Up/Down  move row   Left/Right  change value   "
        "Space toggles yes/no   Enter on 'Run'  export   Esc / Q  quit"
    )

    while True:
        _tui_clear()
        if target == "all":
            title = "MetaMagi export options  --  ALL BOTS (aggregate)"
        else:
            bn = target["name"] or "(unnamed)"
            title = f"MetaMagi export options  --  {bn}  ({target['symbol']})"
        print(title)
        print(help_line)
        print(_BUNDLE_WRITE_MSG)
        print("-" * 72)

        h_line = (
            "(ignored — all time)"
            if all_time
            else f"{WINDOW_HOURS_PRESETS[hours_i]:g} h"
        )
        lines = [
            f"Use full voter_feedback history  :  {'yes' if all_time else 'no'}",
            f"Time window (if not all time)    :  {h_line}",
            f"Sample labeled rows (newest)     :  {SAMPLE_ROWS_PRESETS[sample_i]}",
            f"ROC flat threshold (MetaTrader)  :  {ROC_THRESHOLD_PRESETS[roc_i]}",
            f"Include feature_keys in samples  :  {'yes' if feat else 'no'}",
            "(Output: reports/metamagi_labeled_<stub>_<ts>_for_llm.json + .txt)",
            "[ Run export ]",
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
            if cursor == 1 and not all_time:
                hours_i = (hours_i + delta) % len(WINDOW_HOURS_PRESETS)
            elif cursor == 2:
                sample_i = (sample_i + delta) % len(SAMPLE_ROWS_PRESETS)
            elif cursor == 3:
                roc_i = (roc_i + delta) % len(ROC_THRESHOLD_PRESETS)
        elif key == "space":
            if cursor == 0:
                all_time = not all_time
            elif cursor == 4:
                feat = not feat
        elif key == "enter":
            if cursor == 6:
                return _TuiExportConfig(
                    hours=WINDOW_HOURS_PRESETS[hours_i],
                    all_time=all_time,
                    sample_rows=SAMPLE_ROWS_PRESETS[sample_i],
                    roc_threshold=ROC_THRESHOLD_PRESETS[roc_i],
                    include_feature_keys=feat,
                )
            if cursor == 7:
                return "back"
            if cursor == 8:
                return None


def _time_range_ms(conn: sqlite3.Connection, where_sql: str, params: tuple) -> dict[str, Any]:
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT MIN(timestamp) AS mn, MAX(timestamp) AS mx
        FROM voter_feedback
        WHERE {where_sql}
        """,
        params,
    )
    r = cur.fetchone()
    mn, mx = r["mn"], r["mx"]
    out: dict[str, Any] = {"timestamp_min_ms": mn, "timestamp_max_ms": mx}
    if mn is not None:
        out["timestamp_min_utc"] = datetime.fromtimestamp(
            int(mn) / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")
    if mx is not None:
        out["timestamp_max_utc"] = datetime.fromtimestamp(
            int(mx) / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")
    return out


def _fetch_summary_counts(
    conn: sqlite3.Connection,
    where_sql: str,
    params: tuple,
) -> dict[str, Any]:
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT
            COUNT(*) AS n_total,
            SUM(CASE WHEN forward_roc_30s IS NOT NULL THEN 1 ELSE 0 END) AS n_labeled_30s,
            SUM(CASE WHEN forward_roc_5m IS NOT NULL THEN 1 ELSE 0 END) AS n_labeled_5m
        FROM voter_feedback
        WHERE {where_sql}
        """,
        params,
    )
    return dict(cur.fetchone() or {})


def _per_voter_sql(where_sql: str) -> str:
    """where_sql is AND-clauses for voter_feedback; one ? for roc flat threshold."""
    return f"""
        SELECT
            voter_name,
            COUNT(*) AS n_rows,
            SUM(CASE WHEN forward_roc_30s IS NOT NULL THEN 1 ELSE 0 END) AS n_labeled,
            SUM(CASE WHEN forward_roc_30s IS NOT NULL AND LOWER(TRIM(voter_signal)) = 'buy'
                THEN 1 ELSE 0 END) AS n_buy_labeled,
            SUM(CASE WHEN forward_roc_30s IS NOT NULL AND LOWER(TRIM(voter_signal)) = 'sell'
                THEN 1 ELSE 0 END) AS n_sell_labeled,
            SUM(CASE WHEN forward_roc_30s IS NOT NULL AND LOWER(TRIM(voter_signal)) = 'hold'
                THEN 1 ELSE 0 END) AS n_hold_labeled,
            SUM(CASE WHEN forward_roc_30s IS NOT NULL THEN
                CASE
                    WHEN ABS(forward_roc_30s) < ? THEN
                        CASE WHEN LOWER(TRIM(voter_signal)) = 'hold' THEN 1 ELSE 0 END
                    WHEN forward_roc_30s > 0 THEN
                        CASE WHEN LOWER(TRIM(voter_signal)) = 'buy' THEN 1 ELSE 0 END
                    ELSE
                        CASE WHEN LOWER(TRIM(voter_signal)) = 'sell' THEN 1 ELSE 0 END
                END
            ELSE 0 END) AS n_correct_metatrader,
            AVG(CASE WHEN forward_roc_30s IS NOT NULL THEN forward_roc_30s END) AS avg_roc_30s,
            AVG(CASE WHEN forward_roc_5m IS NOT NULL THEN forward_roc_5m END) AS avg_roc_5m,
            AVG(CASE WHEN forward_roc_30s IS NOT NULL AND LOWER(TRIM(voter_signal)) = 'buy'
                THEN forward_roc_30s END) AS avg_roc_30s_when_buy,
            AVG(CASE WHEN forward_roc_30s IS NOT NULL AND LOWER(TRIM(voter_signal)) = 'sell'
                THEN forward_roc_30s END) AS avg_roc_30s_when_sell,
            AVG(CASE WHEN forward_roc_30s IS NOT NULL AND LOWER(TRIM(voter_signal)) = 'hold'
                THEN forward_roc_30s END) AS avg_roc_30s_when_hold,
            AVG(CASE WHEN forward_roc_30s IS NOT NULL THEN
                CASE LOWER(TRIM(voter_signal))
                    WHEN 'buy' THEN forward_roc_30s
                    WHEN 'sell' THEN -forward_roc_30s
                    ELSE 0.0
                END
            ELSE NULL END) AS edge_score_mean
        FROM voter_feedback
        WHERE {where_sql}
        GROUP BY voter_name
        ORDER BY voter_name
    """


def _norm_per_voter_row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    nl = int(d.get("n_labeled") or 0)
    nc = int(d.get("n_correct_metatrader") or 0)
    acc = round(nc / nl, 6) if nl else None
    d["accuracy_metatrader_rule"] = acc
    for k in (
        "avg_roc_30s",
        "avg_roc_5m",
        "avg_roc_30s_when_buy",
        "avg_roc_30s_when_sell",
        "avg_roc_30s_when_hold",
        "edge_score_mean",
    ):
        v = d.get(k)
        if v is not None:
            d[k] = round(float(v), 8)
    d["n_directional_labeled"] = int(d.get("n_buy_labeled") or 0) + int(
        d.get("n_sell_labeled") or 0
    )
    return d


def _quantile_sorted(sorted_vals: list[float], q: float) -> float:
    """q in [0, 1]; linear interpolation on sorted finite values."""
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    q = max(0.0, min(1.0, q))
    pos = q * (len(sorted_vals) - 1)
    lo_i = int(math.floor(pos))
    hi_i = int(math.ceil(pos))
    if lo_i == hi_i:
        return sorted_vals[lo_i]
    t = pos - lo_i
    return sorted_vals[lo_i] * (1.0 - t) + sorted_vals[hi_i] * t


def _winsorize_edges_for_voters(
    edge_by_voter: dict[str, float],
    voters: list[str],
) -> dict[str, float]:
    """
    Clip each voter's mean edge to global [p5, p95] across `voters` (labeled rows only).
    """
    vals = [edge_by_voter[v] for v in voters if math.isfinite(edge_by_voter.get(v, float("nan")))]
    if not vals:
        return {v: edge_by_voter.get(v, float("nan")) for v in voters}
    s = sorted(vals)
    lo = _quantile_sorted(s, _EDGE_WINSORIZE_Q_LOW)
    hi = _quantile_sorted(s, _EDGE_WINSORIZE_Q_HIGH)
    if lo > hi:
        lo, hi = hi, lo
    out: dict[str, float] = {}
    for v in voters:
        e = edge_by_voter.get(v, float("nan"))
        if not math.isfinite(e):
            out[v] = e
        else:
            # Hard clip on forward ROC-scale extremes (beyond winsor bands)
            e2 = max(-0.05, min(0.05, e))
            out[v] = max(lo, min(hi, e2))
    return out


def _suggested_weights_from_accuracies(
    accuracies: dict[str, float],
) -> dict[str, float]:
    """
    Same normalization/clamps as MetaTrader.get_dynamic_weights (not EMA —
    feed window batch accuracies or any 0..1 per-voter scores).
    """
    _MIN_WEIGHT = 0.5
    _MAX_WEIGHT_MULTIPLIER = 2.0
    names = sorted(accuracies)
    raw = {v: accuracies[v] for v in names}
    if not raw:
        return {}
    avg_acc = sum(raw.values()) / len(raw)
    if avg_acc <= 0:
        return {v: 1.0 for v in names}
    max_w = avg_acc * _MAX_WEIGHT_MULTIPLIER
    out: dict[str, float] = {}
    for voter, acc in raw.items():
        w = acc / avg_acc
        w = max(_MIN_WEIGHT, min(max_w, w))
        out[voter] = round(w, 4)
    return out


def _suggested_weights_from_edge(
    edge_means: dict[str, float],
    n_directional: dict[str, int],
    accuracies: dict[str, float],
    *,
    min_directional_samples: int = _EDGE_MIN_DIRECTIONAL_SAMPLES,
) -> dict[str, float]:
    """
    Turn per-voter mean(forward_roc_30s × direction) into relative weights aligned with ROI.

    Edge weighting is generally superior to binary accuracy when tuning static voters:
    correctness treats +1 bp and +50 bp identically once "right," while edge keeps
    marginal contribution to portfolio return — better proxy for stacking / weighting voters.

    Normalization matches the ROI-oriented spec: divide by mean edge among voters that
    use the edge path, then clamp each ratio to [_MIN_WEIGHT, _MAX_WEIGHT_MULTIPLIER].

    - No buy/sell calls (hold-only labeled rows): forced weight ``0.5`` (minimum).
    - Too few directional samples: reuse accuracy-derived weight for that voter.
    - NaN / non-finite edge: accuracy fallback for that voter.
    - Degenerate mean edge (~0): all edge-path voters reuse accuracy weights.
    """
    _MIN_WEIGHT = 0.5
    _MAX_WEIGHT_MULTIPLIER = 2.0
    _AVG_EDGE_EPS = 1e-12

    accuracy_weights = _suggested_weights_from_accuracies(accuracies)

    voters = sorted(set(edge_means) | set(n_directional) | set(accuracy_weights))

    edge_path: list[str] = []
    out: dict[str, float] = {}

    for v in voters:
        nd = int(n_directional.get(v, 0))
        raw_e = edge_means.get(v, float("nan"))
        if nd == 0:
            out[v] = round(_MIN_WEIGHT, 4)
            continue
        if nd < min_directional_samples or not math.isfinite(raw_e):
            out[v] = accuracy_weights.get(v, round(1.0, 4))
            continue
        edge_path.append(v)

    if not edge_path:
        return out

    wins = _winsorize_edges_for_voters(edge_means, edge_path)
    adjusted_vals = [wins[v] for v in edge_path if math.isfinite(wins.get(v, float("nan")))]
    avg_edge = sum(adjusted_vals) / len(adjusted_vals) if adjusted_vals else 0.0

    if math.isfinite(avg_edge) and abs(avg_edge) >= _AVG_EDGE_EPS:
        for v in edge_path:
            w = wins[v] / avg_edge
            w = max(_MIN_WEIGHT, min(_MAX_WEIGHT_MULTIPLIER, w))
            out[v] = round(w, 4)
    else:
        for v in edge_path:
            out[v] = accuracy_weights.get(v, round(1.0, 4))

    return out


def _suggested_blended_weights(
    edge_weights: dict[str, float],
    accuracy_weights: dict[str, float],
    blend_alpha: float = 0.65,
) -> dict[str, float]:
    """
    Convex combination of edge- and accuracy-derived weights, then mean-normalize and clamp.

    Per voter: ``blend_alpha * edge_w + (1 - blend_alpha) * acc_w``. Divide by the mean of
    those blended values, then clamp each ratio to [0.5, 2.0] (same band as edge-path
    multipliers vs batch mean). Rounded to 4 decimals.

    If only one family has a voter, that weight is used as-is for the linear step (coefficient
    irrelevant when one side is missing).
    """
    _MIN_WEIGHT = 0.5
    _MAX_WEIGHT_MULTIPLIER = 2.0

    ba = float(blend_alpha)
    bb = 1.0 - ba
    voters = sorted(set(edge_weights) | set(accuracy_weights))
    linear: dict[str, float] = {}
    for v in voters:
        ev = edge_weights.get(v)
        av = accuracy_weights.get(v)
        if ev is None and av is None:
            continue
        if ev is None:
            linear[v] = float(av) if av is not None else 1.0
        elif av is None:
            linear[v] = float(ev)
        else:
            linear[v] = ba * float(ev) + bb * float(av)

    if not linear:
        return {}

    avg_lin = sum(linear.values()) / len(linear)
    if avg_lin <= 0 or not math.isfinite(avg_lin):
        return {v: round(1.0, 4) for v in linear}

    out: dict[str, float] = {}
    for v, val in linear.items():
        if not math.isfinite(val):
            out[v] = round(1.0, 4)
            continue
        r = val / avg_lin
        r = max(_MIN_WEIGHT, min(_MAX_WEIGHT_MULTIPLIER, r))
        out[v] = round(r, 4)
    return out


def _fetch_ensemble_roc_quality(
    conn: sqlite3.Connection,
    where_sql: str,
    params: tuple,
) -> list[dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT ensemble_signal,
               COUNT(*) AS n,
               AVG(forward_roc_30s) AS m30,
               AVG(forward_roc_5m) AS m5m
        FROM voter_feedback
        WHERE {where_sql}
          AND forward_roc_30s IS NOT NULL
        GROUP BY ensemble_signal
        ORDER BY ensemble_signal
        """,
        params,
    )
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        if d.get("m30") is not None:
            d["m30"] = round(float(d["m30"]), 8)
        if d.get("m5m") is not None:
            d["m5m"] = round(float(d["m5m"]), 8)
        rows.append(d)
    return rows


def _fetch_sample_rows(
    conn: sqlite3.Connection,
    where_sql: str,
    params: tuple,
    limit: int,
    *,
    include_feature_keys: bool,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT feedback_id, bot_id, timestamp, target_asset, ensemble_signal,
               voter_name, voter_signal, confidence, consensus_score,
               forward_roc_30s, forward_roc_5m,
               features_snapshot
        FROM voter_feedback
        WHERE {where_sql}
          AND forward_roc_30s IS NOT NULL
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (*params, limit),
    )
    out: list[dict[str, Any]] = []
    for row in cur.fetchall():
        d = dict(row)
        ts = int(d["timestamp"])
        d["time_utc"] = datetime.fromtimestamp(
            ts / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")
        snap = d.pop("features_snapshot", None)
        if include_feature_keys and snap:
            try:
                obj = json.loads(snap)
                if isinstance(obj, dict):
                    d["feature_keys"] = sorted(obj.keys())[:80]
                else:
                    d["feature_keys"] = None
            except json.JSONDecodeError:
                d["feature_keys"] = None
        else:
            d.pop("features_snapshot", None)
        for k in ("forward_roc_30s", "forward_roc_5m", "confidence", "consensus_score"):
            v = d.get(k)
            if isinstance(v, float):
                d[k] = round(v, 8)
        out.append(d)
    return out


def _per_bot_labeled_counts(
    conn: sqlite3.Connection,
    where_sql: str,
    global_params: tuple,
) -> list[dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT
            bot_id,
            COUNT(*) AS n_rows,
            SUM(CASE WHEN forward_roc_30s IS NOT NULL THEN 1 ELSE 0 END) AS n_labeled
        FROM voter_feedback
        WHERE {where_sql}
        GROUP BY bot_id
        ORDER BY n_labeled DESC
        """,
        global_params,
    )
    return [dict(r) for r in cur.fetchall()]


def build_export_payload(
    conn: sqlite3.Connection,
    *,
    bot_id_filter: str | None,
    since_ms: int | None,
    roc_threshold: float,
    sample_rows: int,
    include_feature_keys: bool,
    bot_row: sqlite3.Row | None,
    weight_method: Literal["edge", "accuracy", "both"] = "edge",
    blend_alpha: float = 0.65,
) -> dict[str, Any]:
    clauses: list[str] = ["1 = 1"]
    params: list[Any] = []
    if bot_id_filter:
        clauses.append("bot_id = ?")
        params.append(bot_id_filter)
    if since_ms is not None:
        clauses.append("timestamp >= ?")
        params.append(since_ms)
    where_sql = " AND ".join(clauses)
    tup = tuple(params)

    summary = _fetch_summary_counts(conn, where_sql, tup)
    time_range = _time_range_ms(conn, where_sql, tup)
    nt = int(summary.get("n_total") or 0)
    nl = int(summary.get("n_labeled_30s") or 0)

    cur = conn.cursor()
    cur.execute(_per_voter_sql(where_sql), (roc_threshold, *tup))
    per_voter = [_norm_per_voter_row(r) for r in cur.fetchall()]

    accuracies: dict[str, float] = {}
    edge_means: dict[str, float] = {}
    n_directional: dict[str, int] = {}
    for pv in per_voter:
        name = str(pv["voter_name"])
        n_directional[name] = int(pv.get("n_directional_labeled") or 0)
        em = pv.get("edge_score_mean")
        if em is not None:
            edge_means[name] = float(em)
        acc = pv.get("accuracy_metatrader_rule")
        if acc is not None:
            accuracies[name] = float(acc)

    suggested_accuracy = _suggested_weights_from_accuracies(accuracies)
    suggested_edge = _suggested_weights_from_edge(
        edge_means, n_directional, accuracies
    )
    suggested_blended = _suggested_blended_weights(
        suggested_edge, suggested_accuracy, blend_alpha=float(blend_alpha)
    )
    ensemble_quality = _fetch_ensemble_roc_quality(conn, where_sql, tup)
    samples = _fetch_sample_rows(
        conn,
        where_sql,
        tup,
        sample_rows,
        include_feature_keys=include_feature_keys,
    )

    payload: dict[str, Any] = {
        "schema_version": 1,
        "document_purpose": (
            "Export of SQLite voter_feedback rows with forward-return labels for "
            "LLM-assisted review of voter quality and proposed static voter_weights "
            "(strategy_params_json). This is not the live MetaTrader EMA state."
        ),
        "generated_at_utc": datetime.now(tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        ),
        "query": {
            "bot_id": bot_id_filter,
            "since_ms": since_ms,
            "all_time": since_ms is None,
            "roc_threshold_used": roc_threshold,
            "weight_method": weight_method,
            "blend_alpha_used": float(blend_alpha),
        },
        "labeled_data_schema": labeled_table_schema(),
        "counts": {
            "total_rows": nt,
            "labeled_forward_roc_30s": nl,
            "labeled_forward_roc_5m": int(summary.get("n_labeled_5m") or 0),
            "pct_labeled_30s": round(100.0 * nl / nt, 4) if nt else 0.0,
        },
        "time_range": time_range,
        "ensemble_signal_forward_roc": ensemble_quality,
        "per_voter_labeled": per_voter,
        "llm_hints": {
            "suggested_relative_weights_from_window_accuracy": suggested_accuracy,
            "suggested_accuracy_weights": suggested_accuracy,
            "suggested_edge_weights": suggested_edge,
            "suggested_blended_weights": suggested_blended,
            "blend_alpha_used": float(blend_alpha),
            "note": (
                "suggested_edge_weights: mean(forward_roc_30s * vote direction) per voter, "
                "winsorized, mean-normalized, clamp [0.5, 2.0]; better ROI prior than accuracy. "
                "suggested_accuracy_weights (alias: suggested_relative_weights_from_window_accuracy): "
                "MetaTrader-style batch accuracy normalization (clamp uses 2x avg accuracy). "
                "suggested_blended_weights: convex combo (blend_alpha * edge + (1-blend_alpha) * accuracy), "
                "mean-normalized, clamp [0.5, 2.0]; preferred stable ROI-aware prior for production. "
                "Neither substitutes live MetaTrader dynamic weights."
            ),
            "weight_method": weight_method,
        },
        "sample_labeled_rows_newest_first": samples,
    }

    if not bot_id_filter:
        payload["per_bot_row_counts"] = _per_bot_labeled_counts(conn, where_sql, tup)

    if bot_row is not None:
        raw = bot_row["strategy_params_json"]
        params_json: dict[str, Any] = {}
        if isinstance(raw, str) and raw.strip():
            try:
                params_json = json.loads(raw)
            except json.JSONDecodeError:
                params_json = {}
        payload["bot_context"] = {
            "bot_id": bot_row["bot_id"],
            "name": bot_row["name"],
            "symbol": bot_row["symbol"],
            "voters_configured": params_json.get("voters"),
            "voter_weights_current": params_json.get("voter_weights"),
            "consensus_mode": params_json.get("consensus_mode"),
            "consensus_threshold": params_json.get("consensus_threshold"),
        }

    return payload


def build_text_report(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    q = payload.get("query") or {}
    lines.append("MetaMagi labeled data export")
    lines.append("=" * 60)
    lines.append(f"Generated: {payload.get('generated_at_utc', '')}")
    lines.append(
        f"Filter: bot_id={q.get('bot_id') or 'ALL'}  "
        f"since_ms={q.get('since_ms')}  "
        f"roc_threshold={q.get('roc_threshold_used')}  "
        f"weight_method={q.get('weight_method')}  "
        f"blend_alpha={q.get('blend_alpha_used')}"
    )
    c = payload.get("counts") or {}
    lines.append(
        f"Rows: total={c.get('total_rows')}  labeled_30s={c.get('labeled_forward_roc_30s')} "
        f"({c.get('pct_labeled_30s')}%)"
    )
    tr = payload.get("time_range") or {}
    lines.append(
        f"Time span: {tr.get('timestamp_min_utc', '?')} -> {tr.get('timestamp_max_utc', '?')}"
    )
    bc = payload.get("bot_context")
    if bc:
        lines.append("")
        lines.append("Bot context:")
        lines.append(f"  voters: {bc.get('voters_configured')}")
        lines.append(f"  voter_weights_current: {bc.get('voter_weights_current')}")

    lines.append("")
    lines.append(
        "Blended weights (default 65% edge + 35% accuracy) are recommended for most use cases "
        "as they balance ROI signal with stability."
    )
    lines.append(
        f"(This export: blend_alpha={q.get('blend_alpha_used')!r}; JSON keys "
        "suggested_blended_weights + blend_alpha_used under llm_hints.)"
    )

    wm = str((payload.get("query") or {}).get("weight_method") or "both").lower()
    blend_alpha_q = q.get("blend_alpha_used")
    if wm == "edge":
        lines.append(
            "Weight hint (CLI --weight-method): edge — emphasize suggested_edge_weights for ROI priors. "
            "suggested_blended_weights is always computed (see --blend-alpha; recommended default prior)."
        )
    elif wm == "accuracy":
        lines.append(
            "Weight hint (CLI --weight-method): accuracy — emphasize suggested_accuracy_weights "
            "(MetaTrader-style batch accuracy). suggested_blended_weights is always computed "
            f"(blend_alpha={blend_alpha_q!r}); recommended for most production uses."
        )
    else:
        ba_pct = (
            float(blend_alpha_q)
            if isinstance(blend_alpha_q, (int, float))
            else 0.65
        )
        lines.append(
            "Weight hint (CLI --weight-method): both — compare accuracy vs edge vs blended columns below. "
            f"suggested_blended_weights mixes {ba_pct:.2f} edge / {1.0 - ba_pct:.2f} accuracy "
            "and is the recommended default prior when edge looks noisy on short windows."
        )

    lines.append("")
    lines.append(
        "Per-voter (labeled rows): MetaTrader acc %, mean edge = mean(roc_30s * direction), "
        "suggested weights (accuracy | edge | blended):"
    )
    lines.append(
        f"  {'voter':<22} {'n_lab':>7} {'n_dir':>6} {'acc':>8} "
        f"{'edge_mean':>12} {'w_acc':>8} {'w_edge':>8} {'w_blend':>8}"
    )
    hints_acc = (payload.get("llm_hints") or {}).get("suggested_accuracy_weights") or {}
    hints_edge = (payload.get("llm_hints") or {}).get("suggested_edge_weights") or {}
    hints_blend = (payload.get("llm_hints") or {}).get("suggested_blended_weights") or {}
    for pv in payload.get("per_voter_labeled") or []:
        vn = str(pv.get("voter_name", ""))[:22]
        n = int(pv.get("n_labeled") or 0)
        nd = int(pv.get("n_directional_labeled") or 0)
        acc = pv.get("accuracy_metatrader_rule")
        accs = f"{acc * 100:.2f}%" if isinstance(acc, (int, float)) else "n/a"
        em = pv.get("edge_score_mean")
        if isinstance(em, (int, float)):
            ems = f"{float(em):.8f}"
        else:
            ems = "n/a"
        vname_key = str(pv.get("voter_name"))
        wa = hints_acc.get(vname_key, "-")
        we = hints_edge.get(vname_key, "-")
        wb = hints_blend.get(vname_key, "-")
        lines.append(
            f"  {vn:<22} {n:>7} {nd:>6} {accs:>8} {ems:>12} {wa!s:>8} {we!s:>8} {wb!s:>8}"
        )

    lines.append("")
    lines.append("(n_dir = buy+sell labeled rows; holds contribute 0 to edge_mean numerator.)")

    lines.append("")
    lines.append("Ensemble signal vs mean forward ROC (labeled):")
    for row in payload.get("ensemble_signal_forward_roc") or []:
        lines.append(
            f"  {row.get('ensemble_signal')}: n={row.get('n')}  "
            f"avg_30s={row.get('m30')}  avg_5m={row.get('m5m')}"
        )

    lines.append("")
    lines.append("See labeled_data_schema in JSON for column semantics and labeling rules.")
    return "\n".join(lines)


def write_metamagi_bundle(
    bundle_dir: str,
    bot_row: sqlite3.Row | None,
    *,
    json_s: str,
    text_body: str,
) -> tuple[str, str]:
    """Write `metamagi_labeled_<stub>_<ts>_for_llm.json` and `.txt`. Returns (json_path, txt_path)."""
    os.makedirs(bundle_dir, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M")
    stub = _slug(str(bot_row["name"] or "")) if bot_row is not None else "all_bots"
    base = os.path.join(bundle_dir, f"metamagi_labeled_{stub}_{ts}")
    jp = base + "_for_llm.json"
    tp = base + ".txt"
    with open(jp, "w", encoding="utf-8") as f:
        f.write(json_s)
    with open(tp, "w", encoding="utf-8") as f:
        f.write(text_body)
    return jp, tp


def _parse_blend_alpha(x: str) -> float:
    try:
        v = float(x)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"invalid float value: {x!r}") from e
    if not 0.0 <= v <= 1.0:
        raise argparse.ArgumentTypeError("blend-alpha must be between 0.0 and 1.0")
    return v


def main() -> None:
    p = argparse.ArgumentParser(
        description="Export voter_feedback labeled rows for LLM / voter weight analysis.",
    )
    p.add_argument("--list", action="store_true", help="List bots and exit")
    p.add_argument(
        "--ui",
        action="store_true",
        help="Interactive TUI: list every bot + ALL BOTS; writes JSON and txt under reports/",
    )
    p.add_argument("--bot", type=str, default=None, help="Restrict to one bot (id, name, or prefix)")
    p.add_argument(
        "--hours",
        type=float,
        default=168.0,
        help="Only rows with timestamp in the last N hours (default 168)",
    )
    p.add_argument(
        "--all-time",
        action="store_true",
        help="Ignore --hours; use entire voter_feedback table",
    )
    p.add_argument(
        "--roc-threshold",
        type=float,
        default=0.0005,
        help="MetaTrader flat-market threshold on abs(forward_roc_30s) (default 0.0005)",
    )
    p.add_argument(
        "--sample-rows",
        type=int,
        default=60,
        help="Max labeled rows to include as samples (0 to disable)",
    )
    p.add_argument(
        "--include-feature-keys",
        action="store_true",
        help="For each sample row, add feature_keys from features_snapshot JSON (no values)",
    )
    p.add_argument("--json", action="store_true", help="Print JSON to stdout")
    p.add_argument("--out", type=str, default=None, help="Write JSON to this path")
    p.add_argument(
        "--bundle-dir",
        type=str,
        default=None,
        metavar="DIR",
        help="Write metamagi_labeled_<stub>_<ts>_for_llm.json and .txt",
    )
    p.add_argument(
        "--blend-alpha",
        type=_parse_blend_alpha,
        default=0.65,
        metavar="A",
        help=(
            "Blend coefficient on suggested_edge_weights when forming suggested_blended_weights "
            "(remaining mass on suggested_accuracy_weights). Range [0.0, 1.0]; default 0.65."
        ),
    )
    p.add_argument(
        "--weight-method",
        choices=("edge", "accuracy", "both"),
        default="edge",
        help=(
            "Which weight family the text report emphasizes (edge = ROI-aligned; accuracy = MetaTrader-style). "
            "JSON always includes suggested_accuracy_weights, suggested_edge_weights, and suggested_blended_weights "
            "(blend_alpha controls blended); blended is recommended for production priors when pure edge is noisy."
        ),
    )
    args = p.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    try:
        if args.list:
            _print_bot_list(conn)
            return

        if args.ui:
            preset = _TuiExportConfig(
                hours=float(args.hours),
                all_time=bool(args.all_time),
                sample_rows=int(args.sample_rows),
                roc_threshold=float(args.roc_threshold),
                include_feature_keys=bool(args.include_feature_keys),
            )
            cli_bot_token: str | None = args.bot
            while True:
                if cli_bot_token:
                    picked = _resolve_bot_row(conn, cli_bot_token)
                    cli_bot_token = None
                else:
                    picked = _tui_pick_bot_or_all(conn)
                if picked == "quit" or picked is None:
                    return
                cfg_o = _tui_configure_export(picked, preset)
                if cfg_o is None:
                    return
                if cfg_o == "back":
                    continue

                bot_row: sqlite3.Row | None = None if picked == "all" else picked
                bot_id_filter: str | None = None if picked == "all" else str(bot_row["bot_id"])

                since_ms: int | None = None
                if not cfg_o.all_time:
                    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
                    since_ms = int(now_ms - float(cfg_o.hours) * 3600 * 1000)

                payload = build_export_payload(
                    conn,
                    bot_id_filter=bot_id_filter,
                    since_ms=since_ms,
                    roc_threshold=float(cfg_o.roc_threshold),
                    sample_rows=int(cfg_o.sample_rows),
                    include_feature_keys=bool(cfg_o.include_feature_keys),
                    bot_row=bot_row,
                    weight_method=args.weight_method,
                    blend_alpha=float(args.blend_alpha),
                )
                text_body = build_text_report(payload)
                json_s = json.dumps(payload, indent=2, default=str)
                jp, tp = write_metamagi_bundle(
                    TUI_BUNDLE_DIR,
                    bot_row,
                    json_s=json_s,
                    text_body=text_body,
                )
                print(f"Wrote {jp}", file=sys.stderr)
                print(f"Wrote {tp}", file=sys.stderr)
                sys.stdout.write(text_body + "\n")
                preset = cfg_o
                if _stdin_is_tty():
                    _prompt_line("\nPress Enter to return to the bot list...")
                else:
                    break
            return

        bot_row = None
        bot_id_filter = None
        if args.bot:
            bot_row = _resolve_bot_row(conn, args.bot)
            bot_id_filter = str(bot_row["bot_id"])

        since_ms: int | None = None
        if not args.all_time:
            now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
            since_ms = int(now_ms - float(args.hours) * 3600 * 1000)

        payload = build_export_payload(
            conn,
            bot_id_filter=bot_id_filter,
            since_ms=since_ms,
            roc_threshold=float(args.roc_threshold),
            sample_rows=int(args.sample_rows),
            include_feature_keys=bool(args.include_feature_keys),
            bot_row=bot_row,
            weight_method=args.weight_method,
            blend_alpha=float(args.blend_alpha),
        )
        text_body = build_text_report(payload)
        json_s = json.dumps(payload, indent=2, default=str)

        if args.bundle_dir:
            jp, tp = write_metamagi_bundle(
                args.bundle_dir,
                bot_row,
                json_s=json_s,
                text_body=text_body,
            )
            print(f"Wrote {jp}", file=sys.stderr)
            print(f"Wrote {tp}", file=sys.stderr)

        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(json_s)
            print(f"Wrote {args.out}", file=sys.stderr)

        if args.json:
            sys.stdout.write(json_s + "\n")
        elif not args.out and not args.bundle_dir:
            sys.stdout.write(text_body + "\n")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
