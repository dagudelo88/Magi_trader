"""
Migrate all existing bots to the current canonical strategy params.

Reads params directly from backend/trading/strategy_templates.py — the single
source of truth — so this script never needs to be edited when params change.

Safe to run while the backend is stopped. Shows a dry-run preview first;
re-run with --confirm to apply.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from database import DB_PATH
from trading.strategy_templates import ENSEMBLE_TEMPLATES

import sqlite3


def _build_new_params(strategy: str, old: dict) -> dict:
    """Merge canonical template with any bot-specific preserved fields."""
    if strategy not in ENSEMBLE_TEMPLATES:
        return {}
    new = dict(ENSEMBLE_TEMPLATES[strategy])
    # Preserve bot-specific runtime fields that shouldn't be overwritten.
    for key in ("initial_budget_quote", "target_asset"):
        existing = old.get(key)
        if existing is not None:
            new[key] = existing
        elif new.get(key) is None:
            new.pop(key, None)
    return new


def main() -> None:
    confirm = "--confirm" in sys.argv
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    bots = conn.execute(
        "SELECT bot_id, name, symbol, strategy, strategy_params_json FROM bots"
    ).fetchall()

    print(f"\n{'Bot ID':<18} {'Strategy':<26} {'Symbol':<12} Old mode → New mode")
    print("-" * 80)

    updates: list[tuple[str, str]] = []
    for bot in bots:
        strategy = bot["strategy"]
        if strategy not in ENSEMBLE_TEMPLATES:
            print(f"  {bot['bot_id']:<18} {strategy:<26} -- SKIP (not a managed ensemble)")
            continue

        old = json.loads(bot["strategy_params_json"] or "{}")
        new = _build_new_params(strategy, old)
        if not new:
            continue

        old_mode = f"{old.get('consensus_mode', '?')} {old.get('consensus_threshold', '?')}"
        new_mode = f"{new['consensus_mode']} {new['consensus_threshold']}"
        old_voters = old.get("voters", [])
        new_voters = new.get("voters", [])

        changed = (old_mode != new_mode) or (old_voters != new_voters)
        marker = "[CHANGE]" if changed else "[same  ]"
        print(
            f"  {marker} {bot['bot_id'][:16]:<18} {strategy:<26} "
            f"{bot['symbol']:<12} {old_mode} → {new_mode}"
        )
        if old_voters != new_voters:
            print(f"    voters: {old_voters}")
            print(f"         → {new_voters}")

        updates.append((json.dumps(new), bot["bot_id"]))

    if not confirm:
        print(f"\n  DRY RUN — {len(updates)} bots would be updated.")
        print("  Re-run with --confirm to apply.\n")
    else:
        for new_params_json, bot_id in updates:
            conn.execute(
                "UPDATE bots SET strategy_params_json = ? WHERE bot_id = ?",
                (new_params_json, bot_id),
            )
        conn.commit()
        print(f"\n  Applied — {len(updates)} bots updated.\n")

    conn.close()


if __name__ == "__main__":
    main()
