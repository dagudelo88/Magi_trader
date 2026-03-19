/** Backend API origin — override with VITE_API_URL in `.env` for non-local setups. */
export const API_BASE =
  import.meta.env.VITE_API_URL?.toString().replace(/\/$/, '') || 'http://localhost:8000';
