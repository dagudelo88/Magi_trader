/**
 * Fallback if `/api/market/tracked` is unreachable — must stay aligned with
 * `backend/tracked_markets.py` (`TRACKED_USDT_STREAM_IDS`).
 */
export const TRACKED_TICKER_SYMBOLS_FALLBACK: string[] = [
  'BTCUSDT',
  'ETHUSDT',
  'BNBUSDT',
  'SOLUSDT',
  'XRPUSDT',
  'ADAUSDT',
  'DOGEUSDT',
  'AVAXUSDT',
];

export const TRACKED_STREAM_IDS_FALLBACK: string[] = TRACKED_TICKER_SYMBOLS_FALLBACK.map((s) =>
  s.toLowerCase()
);
