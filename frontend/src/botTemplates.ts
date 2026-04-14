/**
 * UI metadata for bot creation templates.
 *
 * Strategy params (voters, consensus_mode, thresholds, timeframes, etc.) are
 * intentionally NOT stored here. They are fetched at runtime from the backend
 * via GET /api/strategies, which calls each strategy's default_params().
 *
 * Single source of truth for params: backend/trading/strategy_templates.py
 */

export interface BotTemplate {
  id: string;
  /** Registry strategy name sent to POST /api/bots */
  strategy: string;
  name: string;
  tagline: string;
  description: string;
  defaultSymbol: string;
}

export const BOT_TEMPLATES: BotTemplate[] = [
  // ── Magi Ensemble ───────────────────────────────────────────────────────
  {
    id: 'magi_high',
    strategy: 'magi_ensemble_high',
    name: 'Magi — High Frequency',
    tagline: 'Committee · 1m · directional_net',
    description:
      'Five high-activity voters vote on every 1-minute bar using directional_net consensus. A net edge on one side of the committee is enough to trade. Ideal for scalping liquid pairs.',
    defaultSymbol: 'BTC/USDT',
  },
  {
    id: 'magi_mid',
    strategy: 'magi_ensemble_mid',
    name: 'Magi — Mid Frequency',
    tagline: 'Committee · 5m · directional_net · recommended',
    description:
      'Best balance of signal quality vs. frequency. Five voters on 5-minute bars with a stricter net threshold to filter microstructure noise.',
    defaultSymbol: 'BTC/USDT',
  },
  {
    id: 'magi_low',
    strategy: 'magi_ensemble_low',
    name: 'Magi — Swing Trading',
    tagline: 'Committee · 1h · directional_net · high conviction',
    description:
      'Trend + breakout voters on 1-hour bars. High threshold requires a clear net directional edge. Trades rarely but with maximum confidence. Best for larger budgets.',
    defaultSymbol: 'BTC/USDT',
  },
  // ── Magi Lag Ensemble ────────────────────────────────────────────────────
  {
    id: 'magi_lag_high',
    strategy: 'magi_lag_ensemble_high',
    name: 'Magi Lag — High Frequency',
    tagline: 'BTC-Alt Lag · 1m · directional_net',
    description:
      'Detects BTC lead / alt lag using per-second microstructure data. Four lag-specialized voters fire on every 1-minute bar. Best on ETH/USDT or BNB/USDT.',
    defaultSymbol: 'ETH/USDT',
  },
  {
    id: 'magi_lag_mid',
    strategy: 'magi_lag_ensemble_mid',
    name: 'Magi Lag — Mid Frequency',
    tagline: 'BTC-Alt Lag · 5m · directional_net · recommended',
    description:
      'Best balance of lag signal quality vs. trade frequency. All four lag voters on 5-minute bars. Recommended starting point for lag-based trading.',
    defaultSymbol: 'ETH/USDT',
  },
  {
    id: 'magi_lag_low',
    strategy: 'magi_lag_ensemble_low',
    name: 'Magi Lag — Low Frequency',
    tagline: 'BTC-Alt Lag · 15m · directional_net · high conviction',
    description:
      'Three high-reliability lag voters on 15-minute bars. Equivalent to 2/3 majority but resistant to all-hold bias. Best for larger budgets.',
    defaultSymbol: 'ETH/USDT',
  },
];

export const SUPPORTED_SYMBOLS = [
  'BTC/USDT',
  'ETH/USDT',
  'BNB/USDT',
  'SOL/USDT',
  'ADA/USDT',
  'XRP/USDT',
  'DOGE/USDT',
  'AVAX/USDT',
];
