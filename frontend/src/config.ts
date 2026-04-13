/** Backend API origin — override with VITE_API_URL in `.env` for non-local setups. */
export const API_BASE =
  import.meta.env.VITE_API_URL?.toString().replace(/\/$/, '') || 'http://localhost:8000';

/** How often the tactical OHLCV chart refetches candles + SMA (ms). */
export const CHART_OHLCV_POLL_INTERVAL_MS = 60_000;
