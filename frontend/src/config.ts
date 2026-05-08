/** Backend API origin — override with VITE_API_URL in `.env` for non-local setups. */
export const API_BASE =
  import.meta.env.VITE_API_URL?.toString().replace(/\/$/, '') || 'http://localhost:8000';

/** Backend WebSocket origin derived from the API origin unless explicitly overridden. */
export const WS_BASE =
  import.meta.env.VITE_WS_URL?.toString().replace(/\/$/, '') ||
  API_BASE.replace(/^http:/, 'ws:').replace(/^https:/, 'wss:');

/** How often the tactical OHLCV chart refetches candles + SMA (ms). */
export const CHART_OHLCV_POLL_INTERVAL_MS = 60_000;
