import { useCallback, useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { API_BASE } from '../config';

interface BotRecord {
  bot_id: string;
  name: string;
  symbol: string;
  strategy: string;
  status: string;
  strategy_params_json: string | null;
}

interface BotLogRow {
  log_id: number;
  bot_id: string;
  created_at: number;
  level: string;
  execution_mode: string;
  message: string;
}

interface BotOrderStats {
  total_orders: number;
  buy_count: number;
  sell_count: number;
  last_order_at_ms: number | null;
}

interface BotOrderRow {
  order_row_id: number;
  bot_id: string;
  execution_mode: string;
  exchange_order_id: string | null;
  symbol: string;
  side: string;
  order_type: string;
  amount: number | null;
  cost: number | null;
  average: number | null;
  filled: number | null;
  status: string | null;
  created_at: number;
}

function formatLogTime(ms: number) {
  try {
    return new Date(ms).toISOString().replace('T', ' ').slice(0, 19);
  } catch {
    return String(ms);
  }
}

function formatParamsJson(raw: string | null) {
  if (!raw) return '—';
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

export default function BotDetail() {
  const { id } = useParams();
  const [bot, setBot] = useState<BotRecord | null>(null);
  const [logs, setLogs] = useState<BotLogRow[]>([]);
  const [orderStats, setOrderStats] = useState<BotOrderStats | null>(null);
  const [orders, setOrders] = useState<BotOrderRow[]>([]);
  const [executionMode, setExecutionMode] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    if (!id) return;
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/bots/${id}`);
      if (res.status === 404) {
        setBot(null);
        setError('Bot not found');
        return;
      }
      if (!res.ok) throw new Error('Failed to load bot');
      const data = await res.json();
      setBot(data.bot);
      setLogs(data.logs || []);
      setOrderStats(data.order_stats ?? null);
      setOrders(data.orders || []);
      setExecutionMode(data.execution_mode ?? null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Load failed');
    }
  }, [id]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (!id || bot?.status !== 'running') return;
    const t = window.setInterval(refresh, 4000);
    return () => window.clearInterval(t);
  }, [id, bot?.status, refresh]);

  const setStatus = async (status: 'running' | 'stopped' | 'paused') => {
    if (!id) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/bots/${id}/status`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Update failed');
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Update failed');
    } finally {
      setBusy(false);
    }
  };

  if (!id) return null;

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      <main className="flex-1 flex overflow-hidden">
        <section className="flex-1 flex flex-col p-6 overflow-y-auto border-r border-border">
          <div className="flex justify-between items-start mb-8">
            <div>
              <h1 className="text-2xl font-bold text-white mb-1">
                {bot?.name ?? '…'}{' '}
                <span className="text-sm font-normal text-gray-500 ml-2">#{id}</span>
              </h1>
              <p className="text-gray-400 text-sm">
                Strategy: {bot?.strategy ?? '—'} | Pair: {bot?.symbol ?? '—'}
                {executionMode && (
                  <span className="ml-2 text-xs uppercase text-primary">({executionMode})</span>
                )}
              </p>
            </div>
            <div className="text-right">
              <div className="text-xs text-gray-500 uppercase tracking-widest font-semibold mb-1">Status</div>
              <div className="text-xl font-mono font-bold text-white uppercase">{bot?.status ?? '—'}</div>
            </div>
          </div>

          {error && (
            <div className="mb-4 p-3 rounded border border-red-500/40 text-red-300 text-sm">{error}</div>
          )}

          <div className="grid grid-cols-2 gap-4 mb-8">
            <div className="bg-panel border border-border p-4 rounded-custom">
              <div className="text-xs text-gray-500 mb-1">Strategy params</div>
              <pre className="text-xs text-gray-300 font-mono whitespace-pre-wrap break-all max-h-32 overflow-y-auto">
                {formatParamsJson(bot?.strategy_params_json ?? null)}
              </pre>
            </div>
            <div className="bg-panel border border-border p-4 rounded-custom">
              <div className="text-xs text-gray-500 mb-1">How it works</div>
              <p className="text-sm text-gray-400">
                The worker polls OHLCV on your symbol, applies a fast/slow SMA crossover, and places small{' '}
                <span className="text-white">market</span> orders on the exchange for the configured environment
                (testnet by default). Tune fractions and min interval in params to reduce frequency and size.
              </p>
            </div>
          </div>

          <div className="flex-1 min-h-[200px] bg-panel border border-border rounded-custom p-6 flex flex-col gap-4">
            <div>
              <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400 mb-3">
                Order history (this bot)
              </h3>
              <p className="text-xs text-gray-500 mb-4">
                Counts and rows below are orders this worker placed and stored locally. Cross-check the exchange for
                definitive fills and fees.
              </p>
              {orderStats && (
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
                  <div className="bg-black/40 border border-border rounded p-3">
                    <div className="text-[10px] text-gray-500 uppercase font-bold tracking-wider">Total orders</div>
                    <div className="text-xl font-mono text-white">{orderStats.total_orders}</div>
                  </div>
                  <div className="bg-black/40 border border-border rounded p-3">
                    <div className="text-[10px] text-gray-500 uppercase font-bold tracking-wider">Buys</div>
                    <div className="text-xl font-mono text-emerald-400">{orderStats.buy_count}</div>
                  </div>
                  <div className="bg-black/40 border border-border rounded p-3">
                    <div className="text-[10px] text-gray-500 uppercase font-bold tracking-wider">Sells</div>
                    <div className="text-xl font-mono text-amber-400">{orderStats.sell_count}</div>
                  </div>
                  <div className="bg-black/40 border border-border rounded p-3">
                    <div className="text-[10px] text-gray-500 uppercase font-bold tracking-wider">Last order</div>
                    <div className="text-sm font-mono text-gray-200">
                      {orderStats.last_order_at_ms != null
                        ? formatLogTime(orderStats.last_order_at_ms)
                        : '—'}
                    </div>
                  </div>
                </div>
              )}
            </div>
            <div className="flex-1 min-h-0 overflow-auto rounded border border-border">
              <table className="w-full text-left text-xs">
                <thead className="sticky top-0 bg-panel border-b border-border text-gray-500 uppercase tracking-wider">
                  <tr>
                    <th className="p-2 font-semibold">Time</th>
                    <th className="p-2 font-semibold">Side</th>
                    <th className="p-2 font-semibold">Pair</th>
                    <th className="p-2 font-semibold">Order id</th>
                    <th className="p-2 font-semibold">Amount</th>
                    <th className="p-2 font-semibold">Cost</th>
                    <th className="p-2 font-semibold">Status</th>
                    <th className="p-2 font-semibold">Mode</th>
                  </tr>
                </thead>
                <tbody>
                  {orders.length === 0 && (
                    <tr>
                      <td colSpan={8} className="p-4 text-gray-600 italic">
                        No recorded orders yet — the bot stores each successful placement here.
                      </td>
                    </tr>
                  )}
                  {orders.map((o) => (
                    <tr key={o.order_row_id} className="border-b border-border/60 text-gray-300 hover:bg-black/20">
                      <td className="p-2 font-mono whitespace-nowrap">{formatLogTime(o.created_at)}</td>
                      <td
                        className={`p-2 font-bold uppercase ${
                          o.side === 'buy' ? 'text-emerald-400' : 'text-amber-400'
                        }`}
                      >
                        {o.side}
                      </td>
                      <td className="p-2">{o.symbol}</td>
                      <td className="p-2 font-mono text-gray-400 break-all max-w-[120px]">
                        {o.exchange_order_id ?? '—'}
                      </td>
                      <td className="p-2 font-mono">{o.amount != null ? String(o.amount) : '—'}</td>
                      <td className="p-2 font-mono">{o.cost != null ? String(o.cost) : '—'}</td>
                      <td className="p-2">{o.status ?? '—'}</td>
                      <td className="p-2 text-gray-500 uppercase">{o.execution_mode}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="text-sm text-gray-500">
              Performance PnL aggregates are not computed in-app yet — use exchange history plus bot logs for analysis.
            </p>
          </div>
        </section>

        <aside className="w-[450px] bg-black flex flex-col shrink-0">
          <div className="p-4 border-b border-border bg-panel flex justify-between items-center">
            <h3 className="text-xs font-bold uppercase tracking-widest flex items-center gap-2">
              <span className="w-2 h-2 bg-primary rounded-full animate-pulse" />
              Bot logs
            </h3>
            <span className="text-[10px] text-gray-500 font-mono">{executionMode ?? '—'}</span>
          </div>
          <div
            className="flex-1 overflow-y-auto p-4 font-mono text-[12px] leading-relaxed space-y-2"
            data-purpose="execution-logs"
          >
            {logs.length === 0 && (
              <div className="text-gray-600 italic">No log lines yet — start the bot after configuring keys.</div>
            )}
            {logs.map((log) => (
              <div
                key={log.log_id}
                className={
                  log.level === 'error'
                    ? 'text-red-400'
                    : log.level === 'warn'
                      ? 'text-amber-400'
                      : log.level === 'debug'
                        ? 'text-gray-500'
                        : log.level === 'info'
                          ? 'text-slate-300'
                          : 'text-gray-300'
                }
              >
                <span className="text-primary">[{formatLogTime(log.created_at)}]</span>{' '}
                <span className="text-gray-500">[{log.execution_mode}]</span>{' '}
                <span className="text-blue-400">[{log.level}]</span> {log.message}
              </div>
            ))}
          </div>
        </aside>
      </main>

      <footer className="h-20 bg-panel border-t border-border px-6 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-6">
          <div className="flex flex-col">
            <span className="text-[10px] text-gray-500 uppercase font-bold tracking-widest">Bot status</span>
            <div className="flex items-center gap-2">
              <span
                className={`w-3 h-3 rounded-full ${
                  bot?.status === 'running' ? 'bg-green-500 shadow-[0_0_8px_rgba(34,197,94,0.6)]' : 'bg-gray-500'
                }`}
              />
              <span className="font-bold text-white uppercase tracking-tight">{bot?.status ?? '—'}</span>
            </div>
          </div>
          <div className="h-8 w-px bg-border" />
          <div className="flex flex-col">
            <span className="text-[10px] text-gray-500 uppercase font-bold tracking-widest">Backend mode</span>
            <span
              className={`font-bold uppercase tracking-tight ${
                executionMode === 'live' ? 'text-red-400' : 'text-blue-400'
              }`}
            >
              {executionMode === 'live' ? 'Mainnet' : executionMode === 'testnet' ? 'Testnet' : '—'}
            </span>
          </div>
        </div>
        <div className="flex gap-3">
          {bot?.status !== 'running' ? (
            <button
              type="button"
              disabled={busy}
              onClick={() => setStatus('running')}
              className="px-6 py-2 bg-emerald-600 text-white rounded-custom text-sm font-bold hover:bg-emerald-700 disabled:opacity-50"
            >
              START
            </button>
          ) : (
            <button
              type="button"
              disabled={busy}
              onClick={() => setStatus('paused')}
              className="px-6 py-2 bg-yellow-600/10 border border-yellow-600/50 text-yellow-500 rounded-custom text-sm font-bold hover:bg-yellow-600 hover:text-white transition-all disabled:opacity-50"
            >
              PAUSE
            </button>
          )}
          {bot?.status === 'paused' && (
            <button
              type="button"
              disabled={busy}
              onClick={() => setStatus('running')}
              className="px-6 py-2 bg-emerald-600/20 border border-emerald-600/50 text-emerald-400 rounded-custom text-sm font-bold hover:bg-emerald-600 hover:text-white disabled:opacity-50"
            >
              RESUME
            </button>
          )}
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              if (window.confirm('Stop this bot?')) setStatus('stopped');
            }}
            className="px-6 py-2 bg-red-600 border border-red-700 text-white rounded-custom text-sm font-bold hover:bg-red-700 disabled:opacity-50"
          >
            STOP
          </button>
        </div>
      </footer>
    </div>
  );
}
