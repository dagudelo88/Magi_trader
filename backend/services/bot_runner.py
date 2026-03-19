"""
Polls DB for bots with status=running and executes the configured strategy on Binance
(Testnet by default, mainnet only when execution_mode=live in app_settings).

Run as a subprocess from the API (same pattern as data_collector).
"""
from __future__ import annotations

import json
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

from dotenv import load_dotenv

load_dotenv(os.path.join(_repo_root, ".env"))
load_dotenv(os.path.join(_backend_dir, ".env"), override=True)
# endregion

from database import get_db_connection, record_bot_order
from trading.app_settings import get_execution_mode, is_global_halt
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
    print(f"[{level}] [{execution_mode}] bot={bot_id} {message}")


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
            f"Waiting: post-trade cooldown — ~{left:.0f}s left before the next full market check "
            f"(min_trade_interval_sec={interval_key:.0f}s).",
        )
        if _debug_logs_enabled():
            _log(
                bot_id,
                "debug",
                execution_mode,
                f"Cooldown detail: {left:.1f}s remaining.",
            )
        return

    timeframe = str(params.get("ohlcv_timeframe", "5m"))
    limit = int(params.get("ohlcv_limit", 50))

    _log(
        bot_id,
        "info",
        execution_mode,
        f"Decision: start cycle symbol={symbol} timeframe={timeframe} ohlcv_limit={limit}.",
    )

    _log(
        bot_id,
        "info",
        execution_mode,
        "Decision: fetching OHLCV from exchange…",
    )
    try:
        ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception as e:
        _log(bot_id, "error", execution_mode, f"fetch_ohlcv failed: {e}")
        return

    if not ohlcv:
        _log(
            bot_id,
            "warn",
            execution_mode,
            "Decision: OHLCV returned empty — cannot evaluate; will retry next poll.",
        )
        if _debug_logs_enabled():
            _log(
                bot_id,
                "debug",
                execution_mode,
                f"OHLCV empty for {symbol} {timeframe}",
            )
        return

    last_row = ohlcv[-1]
    last_close = float(last_row[4])
    last_open_ms = int(last_row[0])
    _log(
        bot_id,
        "info",
        execution_mode,
        f"Decision: OHLCV ok — candles={len(ohlcv)} last_close={last_close} candle_open_ms={last_open_ms}.",
    )
    if _debug_logs_enabled():
        _log(
            bot_id,
            "debug",
            execution_mode,
            f"OHLCV {timeframe} n={len(ohlcv)} last_close={last_close} candle_open_ms={last_open_ms}",
        )

    closes = [float(x[4]) for x in ohlcv]
    _log(bot_id, "info", execution_mode, "Decision: running SMA crossover on closes…")
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
                f"Decision: outcome HOLD (no cross) — fast={details.fast_sma:.8g} slow={details.slow_sma:.8g} "
                f"prev_fast={details.prev_fast_sma:.8g} prev_slow={details.prev_slow_sma:.8g}; no order.",
            )
        else:
            need = int(params["slow_period"]) + 2
            _log(
                bot_id,
                "info",
                execution_mode,
                f"Decision: outcome HOLD (warmup) — closes={details.close_count}, need>={need} bars; no order.",
            )
        if _debug_logs_enabled():
            _log(
                bot_id,
                "debug",
                execution_mode,
                f"Hold detail closes={details.close_count}",
            )
        return

    _log(
        bot_id,
        "info",
        execution_mode,
        f"Decision: outcome {details.signal.upper()} — crossover fast={details.fast_sma:.8g} "
        f"slow={details.slow_sma:.8g} (prev fast={details.prev_fast_sma:.8g} slow={details.prev_slow_sma:.8g}).",
    )

    _log(bot_id, "info", execution_mode, "Decision: loading market metadata and precision…")
    try:
        markets = ex.markets if ex.markets else None
        if not markets:
            ex.load_markets()
        market = ex.market(symbol)
    except Exception as e:
        _log(bot_id, "error", execution_mode, f"market metadata failed: {e}")
        return

    limits = market.get("limits") or {}
    amt_lim = limits.get("amount") or {}
    cost_lim = limits.get("cost") or {}
    min_amt = float(amt_lim.get("min") or 0)
    min_cost = float(cost_lim.get("min") or 0)
    _log(
        bot_id,
        "info",
        execution_mode,
        f"Decision: market limits — min_amount={min_amt} min_cost={min_cost}.",
    )

    _log(bot_id, "info", execution_mode, "Decision: fetching wallet balance…")
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
        f"Decision: balances — free {base_cur}={free_base} free {quote_cur}={free_quote}.",
    )

    if details.signal == "buy":
        quote_fraction = float(params["quote_fraction"])
        spend = free_quote * quote_fraction
        _log(
            bot_id,
            "info",
            execution_mode,
            f"Decision: sizing BUY — quote_fraction={quote_fraction} spend_estimate={spend} (before precision).",
        )
        if spend < min_cost:
            _log(
                bot_id,
                "warn",
                execution_mode,
                f"BUY skipped: quote spend {spend:.6f} below min cost {min_cost}",
            )
            return
        spend_prec = float(ex.cost_to_precision(symbol, spend))
        _log(
            bot_id,
            "info",
            execution_mode,
            f"Decision: submitting MARKET BUY quoteOrderQty={spend_prec} …",
        )
        try:
            order = ex.create_market_buy_order_with_cost(symbol, spend_prec)
            _last_trade_monotonic[bot_id] = time.monotonic()
            record_bot_order(bot_id, execution_mode, order)
            _log(
                bot_id,
                "info",
                execution_mode,
                f"BUY filled/accepted id={order.get('id')} quoteOrderQty={spend_prec} status={order.get('status')}",
            )
        except Exception as e:
            _log(bot_id, "error", execution_mode, f"BUY failed: {e}\n{traceback.format_exc()}")

    elif details.signal == "sell":
        base_fraction = float(params["base_fraction"])
        sell_amt = free_base * base_fraction
        _log(
            bot_id,
            "info",
            execution_mode,
            f"Decision: sizing SELL — base_fraction={base_fraction} amount_estimate={sell_amt}.",
        )
        if min_amt and sell_amt < min_amt:
            _log(
                bot_id,
                "warn",
                execution_mode,
                f"SELL skipped: amount {sell_amt:.8f} below min {min_amt}",
            )
            return
        sell_prec = ex.amount_to_precision(symbol, sell_amt)
        if float(sell_prec) <= 0:
            _log(
                bot_id,
                "warn",
                execution_mode,
                f"SELL skipped: rounded amount is zero (raw {sell_amt}).",
            )
            return
        _log(
            bot_id,
            "info",
            execution_mode,
            f"Decision: submitting MARKET SELL amount={sell_prec} …",
        )
        try:
            order = ex.create_order(symbol, "market", "sell", float(sell_prec))
            _last_trade_monotonic[bot_id] = time.monotonic()
            record_bot_order(bot_id, execution_mode, order)
            _log(
                bot_id,
                "info",
                execution_mode,
                f"SELL filled/accepted id={order.get('id')} amount={sell_prec} status={order.get('status')}",
            )
        except Exception as e:
            _log(bot_id, "error", execution_mode, f"SELL failed: {e}\n{traceback.format_exc()}")


def main():
    print("Bot runner started — polling for running bots.")
    while True:
        try:
            if is_global_halt():
                _throttled_print(
                    "global_halt",
                    f"[bot_runner] Waiting: global trading halt is ON — no bot cycles (sleep {POLL_SEC}s).",
                )
                time.sleep(POLL_SEC)
                continue

            mode = get_execution_mode()
            try:
                ex = build_binance_spot(mode)
            except ValueError as e:
                print(f"Exchange config error: {e}")
                time.sleep(30)
                continue

            bots = _load_running_bots()
            if not bots:
                _throttled_print(
                    "no_running_bots",
                    f"[bot_runner] Waiting: no bots with status=running in DB (sleep {POLL_SEC}s).",
                )
                time.sleep(POLL_SEC)
                continue

            for bot in bots:
                try:
                    _process_bot(ex, bot, mode)
                except Exception:
                    _log(
                        bot["bot_id"],
                        "error",
                        mode,
                        f"Unhandled: {traceback.format_exc()}",
                    )

            time.sleep(POLL_SEC)
        except KeyboardInterrupt:
            print("Bot runner stopped.")
            break
        except Exception:
            print(traceback.format_exc())
            time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
