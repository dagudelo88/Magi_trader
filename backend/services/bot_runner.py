"""
Polls DB for bots with status=running and executes the configured strategy on Binance
(Testnet by default, mainnet only when execution_mode=live in app_settings).

Runs as an asyncio.Task inside uvicorn's lifespan (main.py). Do NOT start as a
subprocess — it will create a duplicate runner. The __main__ block is retained only
for occasional standalone debugging.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import traceback
from typing import Any

# region path / env
_backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_repo_root = os.path.abspath(os.path.join(_backend_dir, ".."))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

# Force UTF-8 output so Unicode characters never crash on Windows cp1252 consoles.
# Wrapped in try/except because stdout may already be a broken pipe when spawned
# as a subprocess by uvicorn (reconfigure raises on closed handles).
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from dotenv import load_dotenv

load_dotenv(os.path.join(_repo_root, ".env"))
load_dotenv(os.path.join(_backend_dir, ".env"), override=True)
# endregion

from database import get_db_connection, record_bot_order, record_bot_decision, fetch_bot_orders_chronological
from trading.app_settings import get_execution_mode, is_global_halt
from trading.bot_performance import compute_strategy_performance
from trading.exchange_factory import build_binance_spot
from trading.strategies.sma_cross import default_strategy_params, evaluate_signal_details

_last_trade_monotonic: dict[str, float] = {}

POLL_SEC = 5

_last_throttled_bot_info: dict[str, float] = {}
_last_throttled_print: dict[str, float] = {}


def _idle_log_interval_sec() -> float:
    try:
        return float(os.environ.get("BOT_IDLE_LOG_INTERVAL_SEC", "45"))
    except ValueError:
        return 45.0


# Set BOT_DEBUG_LOGS=0 (or false/no) to hide extra numeric detail lines in bot_logs.
def _debug_logs_enabled() -> bool:
    raw = (os.environ.get("BOT_DEBUG_LOGS") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _log(bot_id: str, level: str, execution_mode: str, message: str) -> None:
    conn = get_db_connection()
    try:
        conn.execute(
            """
            INSERT INTO bot_logs (bot_id, created_at, level, execution_mode, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (bot_id, int(time.time() * 1000), level, execution_mode, message),
        )
        conn.commit()
    finally:
        conn.close()
    try:
        print(f"[{level}] [{execution_mode}] bot={bot_id} {message}", flush=True)
    except (OSError, ValueError):
        # Stdout pipe is broken or closed (common when spawned as a subprocess).
        # The DB write above already succeeded — this is just a console echo.
        pass


def _log_info_throttled(
    bot_id: str,
    execution_mode: str,
    throttle_key: str,
    message: str,
) -> None:
    now = time.monotonic()
    k = f"{bot_id}:{throttle_key}"
    interval = _idle_log_interval_sec()
    if now - _last_throttled_bot_info.get(k, 0.0) < interval:
        return
    _last_throttled_bot_info[k] = now
    _log(bot_id, "info", execution_mode, message)


def _throttled_print(key: str, message: str) -> None:
    now = time.monotonic()
    interval = _idle_log_interval_sec()
    if now - _last_throttled_print.get(key, 0.0) < interval:
        return
    _last_throttled_print[key] = now
    print(message)


def _load_running_bots():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM bots WHERE status = 'running' ORDER BY bot_id"
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _merge_params(raw: str | None) -> dict[str, Any]:
    base = default_strategy_params()
    if not raw:
        return base
    try:
        merged = {**base, **json.loads(raw)}
        return merged
    except json.JSONDecodeError:
        return base


def _f_order(x: Any) -> float:
    """Safe float coerce for order fields."""
    try:
        v = float(x)
        return v if v == v else 0.0  # guard NaN
    except (TypeError, ValueError):
        return 0.0


def _log_post_trade_balance(
    bot_id: str,
    execution_mode: str,
    ex: Any,
    base_cur: str,
    quote_cur: str,
) -> None:
    """Fetch and log wallet balances immediately after a fill."""
    try:
        bal = ex.fetch_balance()
        free_base = float(bal.get("free", {}).get(base_cur, 0) or 0)
        free_quote = float(bal.get("free", {}).get(quote_cur, 0) or 0)
        _log(
            bot_id,
            "info",
            execution_mode,
            f"Wallet after trade: {base_cur}={free_base:.8f}  {quote_cur}={free_quote:.4f}",
        )
    except Exception:
        pass


def _get_bot_position(bot_id: str, symbol: str) -> tuple[float, float]:
    """
    Returns (open_base_position, open_cost_basis_quote) for this bot
    by replaying its order history using FIFO accounting.
    Used to constrain trades to the bot's configured budget.
    """
    try:
        orders = fetch_bot_orders_chronological(bot_id)
        perf = compute_strategy_performance(orders, symbol)
        return float(perf["open_base_position"]), float(perf["open_cost_basis_quote"])
    except Exception:
        return 0.0, 0.0


def _process_bot(ex, bot: dict[str, Any], execution_mode: str) -> None:
    bot_id = bot["bot_id"]
    symbol = bot["symbol"]
    params = _merge_params(bot.get("strategy_params_json"))

    interval_key = float(params["min_trade_interval_sec"])
    last = _last_trade_monotonic.get(bot_id, 0.0)
    elapsed = time.monotonic() - last
    if elapsed < interval_key:
        left = max(0.0, interval_key - elapsed)
        _log_info_throttled(
            bot_id,
            execution_mode,
            "cooldown",
            f"Cooldown: {left:.0f}s remaining before next market check "
            f"(interval={interval_key:.0f}s).",
        )
        # No per-cycle debug countdown — it floods the log with no value.
        return

    timeframe = str(params.get("ohlcv_timeframe", "5m"))
    limit = int(params.get("ohlcv_limit", 50))

    _log(
        bot_id,
        "info",
        execution_mode,
        f"Cycle: {symbol} {timeframe} — fetching {limit} candles…",
    )

    try:
        ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception as e:
        _log(bot_id, "error", execution_mode, f"fetch_ohlcv failed: {e}")
        return

    if not ohlcv:
        _log(bot_id, "warn", execution_mode, f"OHLCV empty for {symbol} {timeframe} — retrying next poll.")
        return

    last_close = float(ohlcv[-1][4])
    _log(
        bot_id,
        "info",
        execution_mode,
        f"Market: last_close={last_close:.4f}  candles={len(ohlcv)}",
    )

    closes = [float(x[4]) for x in ohlcv]
    details = evaluate_signal_details(
        closes,
        int(params["fast_period"]),
        int(params["slow_period"]),
    )

    if details.signal == "hold":
        if (
            details.fast_sma is not None
            and details.slow_sma is not None
            and details.prev_fast_sma is not None
            and details.prev_slow_sma is not None
        ):
            _log(
                bot_id,
                "info",
                execution_mode,
                f"Signal: HOLD — fast_sma={details.fast_sma:.4f}  slow_sma={details.slow_sma:.4f}  "
                f"(gap={(details.fast_sma - details.slow_sma):+.4f})",
            )
        else:
            need = int(params["slow_period"]) + 2
            _log(
                bot_id,
                "info",
                execution_mode,
                f"Signal: HOLD (warmup) — {details.close_count}/{need} bars collected.",
            )
        try:
            record_bot_decision(bot_id, symbol, execution_mode, "HOLD", None, False)
        except Exception:
            pass
        return

    _log(
        bot_id,
        "info",
        execution_mode,
        f"Signal: {details.signal.upper()} — fast_sma={details.fast_sma:.4f}  slow_sma={details.slow_sma:.4f}  "
        f"(gap={(details.fast_sma - details.slow_sma):+.4f}  prev_gap={(details.prev_fast_sma - details.prev_slow_sma):+.4f})",
    )

    # Confidence proxy: normalised SMA gap width (0–1 capped at 0.05 spread).
    _signal_confidence: float | None = None
    if (
        details.fast_sma is not None
        and details.slow_sma is not None
        and details.slow_sma > 0
    ):
        _signal_confidence = round(
            min(1.0, abs(details.fast_sma - details.slow_sma) / details.slow_sma * 20), 4
        )

    try:
        if not ex.markets:
            ex.load_markets()
        market = ex.market(symbol)
    except Exception as e:
        _log(bot_id, "error", execution_mode, f"market metadata failed: {e}")
        return

    limits = market.get("limits") or {}
    min_amt = float((limits.get("amount") or {}).get("min") or 0)
    min_cost = float((limits.get("cost") or {}).get("min") or 0)

    try:
        balance = ex.fetch_balance()
    except Exception as e:
        _log(bot_id, "error", execution_mode, f"fetch_balance failed: {e}")
        return

    base_cur, quote_cur = market["base"], market["quote"]
    free_base = float(balance.get("free", {}).get(base_cur, 0) or 0)
    free_quote = float(balance.get("free", {}).get(quote_cur, 0) or 0)
    _log(
        bot_id,
        "info",
        execution_mode,
        f"Wallet before trade: {base_cur}={free_base:.8f}  {quote_cur}={free_quote:.4f}",
    )

    if details.signal == "buy":
        quote_fraction = float(params["quote_fraction"])
        initial_budget = float(params.get("initial_budget_quote") or 0)

        if initial_budget > 0:
            # Budget-constrained: only trade within the configured budget
            _, open_cost = _get_bot_position(bot_id, symbol)
            remaining_budget = max(0.0, initial_budget - open_cost)
            if remaining_budget < min_cost:
                _log(bot_id, "warn", execution_mode,
                     f"BUY skipped — budget exhausted "
                     f"({remaining_budget:.4f} {quote_cur} remaining < exchange min {min_cost} {quote_cur})")
                return
            # Ideal spend = fraction of remaining budget, capped by available funds
            spend = min(quote_fraction * remaining_budget, remaining_budget, free_quote)
            # If ideal spend falls below exchange minimum, bump up to minimum (budget permitting)
            if spend < min_cost:
                spend = min_cost
            _log(bot_id, "info", execution_mode,
                 f"Budget: {remaining_budget:.4f} {quote_cur} remaining of {initial_budget:.2f} {quote_cur} budget")
        else:
            # No budget configured — use exchange wallet as fallback
            spend = free_quote * quote_fraction
            if spend < min_cost:
                _log(bot_id, "warn", execution_mode,
                     f"BUY skipped — spend {spend:.4f} {quote_cur} below exchange minimum {min_cost} {quote_cur}")
                return
        if last_close <= 0:
            _log(bot_id, "warn", execution_mode, "BUY skipped — last_close is zero")
            return

        # Compute the buy quantity from spend, then apply lot-size precision.
        # We use create_order(qty) instead of create_market_buy_order_with_cost(cost)
        # because Binance converts cost→qty internally and rounds DOWN, which can leave
        # the actual fill below min_cost — permanently creating an unsellable position.
        qty_raw = spend / last_close
        qty_prec = float(ex.amount_to_precision(symbol, qty_raw))

        # If lot-size rounding dropped the notional below min_cost, ceiling-round
        # the quantity up to the next valid lot step so the fill always meets the minimum.
        if min_cost > 0 and qty_prec * last_close < min_cost:
            try:
                prec_val = market.get("precision", {}).get("amount")
                if prec_val is not None:
                    pv = float(prec_val)
                    # CCXT precision can be in two modes:
                    #   TICK_SIZE    — pv IS the step size  (e.g. 0.00001)
                    #   DECIMAL_PLACES — pv is decimal count (e.g. 5 → step 0.00001)
                    if pv > 0:
                        step = pv if pv < 1 else 10.0 ** (-int(pv))
                        p = max(0, round(-math.log10(step)))
                        min_qty = min_cost / last_close
                        qty_prec = round(math.ceil(round(min_qty / step, 9)) * step, p)
            except Exception:
                pass  # best-effort; proceed with rounded-down qty

        if qty_prec <= 0:
            _log(bot_id, "warn", execution_mode, "BUY skipped — quantity rounds to zero")
            return

        actual_spend = qty_prec * last_close
        budget_ref = remaining_budget if initial_budget > 0 else free_quote
        _log(bot_id, "info", execution_mode,
             f"BUY order: spending ~{actual_spend:.4f} {quote_cur} "
             f"({quote_fraction*100:.1f}% of {budget_ref:.4f} {quote_cur} remaining) @ ~{last_close:.2f}")
        try:
            order = ex.create_order(symbol, "market", "buy", qty_prec)
            _last_trade_monotonic[bot_id] = time.monotonic()
            record_bot_order(bot_id, execution_mode, order)
            filled_base = _f_order(order.get("filled"))
            cost_quote = _f_order(order.get("cost")) or actual_spend
            avg_price = _f_order(order.get("average")) or (cost_quote / filled_base if filled_base > 0 else 0.0)
            _log(bot_id, "info", execution_mode,
                 f"[OK] BUY filled — spent {cost_quote:.4f} {quote_cur} "
                 f"-> received {filled_base:.8f} {base_cur} "
                 f"@ avg {avg_price:.2f} {quote_cur}/{base_cur}  [id={order.get('id')}]")
            _log_post_trade_balance(bot_id, execution_mode, ex, base_cur, quote_cur)
            try:
                record_bot_decision(bot_id, symbol, execution_mode, "BUY", _signal_confidence, True)
            except Exception:
                pass
        except Exception as e:
            try:
                record_bot_decision(bot_id, symbol, execution_mode, "BUY", _signal_confidence, False)
            except Exception:
                pass
            _log(bot_id, "error", execution_mode, f"BUY failed: {e}\n{traceback.format_exc()}")

    elif details.signal == "sell":
        base_fraction = float(params["base_fraction"])
        initial_budget = float(params.get("initial_budget_quote") or 0)

        if initial_budget > 0:
            # Sell only the base position this bot acquired with its own budget
            open_base, _ = _get_bot_position(bot_id, symbol)
            if open_base <= 1e-12:
                _log(bot_id, "info", execution_mode,
                     "SELL skipped — no open position in this bot's budget")
                return
            # Cap by what the exchange actually holds (safety net)
            sell_amt = min(open_base * base_fraction, free_base)
        else:
            # No budget configured — use exchange wallet as fallback
            sell_amt = free_base * base_fraction

        # Minimum base-amount check
        if min_amt and sell_amt < min_amt:
            _log(bot_id, "warn", execution_mode,
                 f"SELL skipped — amount {sell_amt:.8f} {base_cur} below exchange minimum {min_amt}")
            return

        # Minimum notional (quote value) check — same filter Binance enforces.
        # If the fractional sell is too small, try selling the full position instead.
        # Only skip entirely when even the full position is below minimum notional.
        if min_cost > 0 and last_close > 0:
            notional = sell_amt * last_close
            if notional < min_cost:
                full_position = open_base if initial_budget > 0 else free_base
                full_notional = full_position * last_close
                if full_notional >= min_cost:
                    sell_amt = min(full_position, free_base)
                    _log(bot_id, "info", execution_mode,
                         f"SELL: fractional notional {notional:.4f} {quote_cur} < min {min_cost} "
                         f"— selling full position {sell_amt:.8f} {base_cur} instead")
                else:
                    _log(bot_id, "warn", execution_mode,
                         f"SELL skipped — position notional {full_notional:.4f} {quote_cur} "
                         f"below exchange minimum {min_cost} {quote_cur}")
                    return

        sell_prec = ex.amount_to_precision(symbol, sell_amt)
        if float(sell_prec) <= 0:
            _log(bot_id, "warn", execution_mode,
                 f"SELL skipped — rounded amount is zero (raw {sell_amt:.8f} {base_cur})")
            return
        _log(bot_id, "info", execution_mode,
             f"SELL order: selling {sell_prec} {base_cur} "
             f"(notional ~{float(sell_prec) * last_close:.4f} {quote_cur}) @ ~{last_close:.2f}")
        try:
            order = ex.create_order(symbol, "market", "sell", float(sell_prec))
            _last_trade_monotonic[bot_id] = time.monotonic()
            record_bot_order(bot_id, execution_mode, order)
            filled_base = _f_order(order.get("filled"))
            cost_quote = _f_order(order.get("cost"))
            avg_price = _f_order(order.get("average")) or (cost_quote / filled_base if filled_base > 0 else 0.0)
            _log(bot_id, "info", execution_mode,
                 f"[OK] SELL filled — sold {filled_base:.8f} {base_cur} "
                 f"→ received {cost_quote:.4f} {quote_cur} "
                 f"@ avg {avg_price:.2f} {quote_cur}/{base_cur}  [id={order.get('id')}]")
            _log_post_trade_balance(bot_id, execution_mode, ex, base_cur, quote_cur)
            try:
                record_bot_decision(bot_id, symbol, execution_mode, "SELL", _signal_confidence, True)
            except Exception:
                pass
        except Exception as e:
            try:
                record_bot_decision(bot_id, symbol, execution_mode, "SELL", _signal_confidence, False)
            except Exception:
                pass
            _log(bot_id, "error", execution_mode, f"SELL failed: {e}\n{traceback.format_exc()}")


def _build_exchanges_for_bots(bots: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Build one CCXT exchange instance per unique execution_mode in the bot list.
    Each bot carries its own execution_mode ('testnet' | 'live'), so a single
    runner can serve mixed-mode bots safely.
    """
    exchanges: dict[str, Any] = {}
    modes_needed = {bot.get("execution_mode", "testnet") for bot in bots}
    for mode in modes_needed:
        if mode in exchanges:
            continue
        try:
            exchanges[mode] = build_binance_spot(mode)
        except ValueError as e:
            print(f"[bot_runner] Exchange config error for mode={mode}: {e}")
    return exchanges


async def run_async():
    """Async entry-point embedded as an asyncio task in uvicorn's lifespan."""
    import asyncio
    print("Bot runner started — polling for running bots.")
    while True:
        try:
            _run_one_cycle()
        except Exception:
            pass
        await asyncio.sleep(POLL_SEC)


def _run_one_cycle():
    """One polling cycle — extracted so both the async and sync entrypoints share it."""
    if is_global_halt():
        _throttled_print(
            "global_halt",
            "[bot_runner] Waiting: global trading halt is ON — no bot cycles.",
        )
        return

    bots = _load_running_bots()
    if not bots:
        _throttled_print(
            "no_running_bots",
            f"[bot_runner] Waiting: no bots with status=running in DB (sleep {POLL_SEC}s).",
        )
        return

    exchanges = _build_exchanges_for_bots(bots)

    for bot in bots:
        bot_mode = bot.get("execution_mode", "testnet")
        ex = exchanges.get(bot_mode)
        if ex is None:
            _log(
                bot["bot_id"],
                "error",
                bot_mode,
                f"No exchange available for execution_mode={bot_mode!r} — check API keys in .env.",
            )
            continue
        try:
            _process_bot(ex, bot, bot_mode)
        except Exception:
            _log(
                bot["bot_id"],
                "error",
                bot_mode,
                f"Unhandled: {traceback.format_exc()}",
            )


def main():
    """Synchronous blocking loop — used only when running as __main__ (standalone/legacy)."""
    print("Bot runner started — polling for running bots.")
    while True:
        try:
            _run_one_cycle()
            time.sleep(POLL_SEC)
        except KeyboardInterrupt:
            print("Bot runner stopped.")
            break
        except Exception:
            try:
                print(traceback.format_exc())
            except (OSError, ValueError):
                pass
            time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
