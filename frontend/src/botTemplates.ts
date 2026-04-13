/** Premade bot strategy templates the user can select when creating a new bot. */

export interface BotTemplate {
  id: string;
  name: string;
  tagline: string;
  description: string;
  defaultSymbol: string;
  params: {
    fast_period: number;
    slow_period: number;
    quote_fraction: number;
    base_fraction: number;
    min_trade_interval_sec: number;
    ohlcv_timeframe: string;
    ohlcv_limit: number;
  };
}

export const BOT_TEMPLATES: BotTemplate[] = [
  {
    id: 'sma_scalper',
    name: 'SMA Scalper',
    tagline: 'High-frequency · 1m candles',
    description:
      'Reacts to fast SMA crossovers on 1-minute bars. Trades small fractions often — best on liquid pairs like BTC and ETH.',
    defaultSymbol: 'BTC/USDT',
    params: {
      fast_period: 3,
      slow_period: 7,
      quote_fraction: 0.05,
      base_fraction: 0.5,
      min_trade_interval_sec: 60,
      ohlcv_timeframe: '1m',
      ohlcv_limit: 50,
    },
  },
  {
    id: 'sma_standard',
    name: 'SMA Standard',
    tagline: 'Balanced · 5m candles',
    description:
      'Classic 5/15 crossover on 5-minute bars with moderate position sizing. Good all-round starting point for any pair.',
    defaultSymbol: 'BTC/USDT',
    params: {
      fast_period: 5,
      slow_period: 15,
      quote_fraction: 0.02,
      base_fraction: 0.5,
      min_trade_interval_sec: 300,
      ohlcv_timeframe: '5m',
      ohlcv_limit: 50,
    },
  },
  {
    id: 'sma_swing',
    name: 'SMA Swing Trader',
    tagline: 'Momentum · 15m candles',
    description:
      'Captures medium-term momentum swings on 15-minute bars. Larger position sizes, less noise-sensitive.',
    defaultSymbol: 'ETH/USDT',
    params: {
      fast_period: 8,
      slow_period: 21,
      quote_fraction: 0.05,
      base_fraction: 0.7,
      min_trade_interval_sec: 900,
      ohlcv_timeframe: '15m',
      ohlcv_limit: 60,
    },
  },
  {
    id: 'sma_trend',
    name: 'SMA Trend Rider',
    tagline: 'Macro trend · 1h candles',
    description:
      'Follows sustained macro trends on hourly bars. Very few trades — high conviction, large sizing per signal.',
    defaultSymbol: 'BTC/USDT',
    params: {
      fast_period: 10,
      slow_period: 30,
      quote_fraction: 0.10,
      base_fraction: 0.80,
      min_trade_interval_sec: 3600,
      ohlcv_timeframe: '1h',
      ohlcv_limit: 80,
    },
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
