import { useState, useEffect } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { API_BASE } from '../config';
import { useRealtimeStore, type NetworkView, type WalletItem } from '../stores/realtimeStore';

function formatUptime(startedAtSec: number | null, status: string): string {
  if (status !== 'running' || startedAtSec == null) return '—';
  const secs = Math.max(0, Math.floor(Date.now() / 1000) - startedAtSec);
  const d = Math.floor(secs / 86400);
  const h = Math.floor((secs % 86400) / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m`;
  return `${secs}s`;
}

interface SmaParams {
  fast_period?: number;
  slow_period?: number;
  quote_fraction?: number;
  ohlcv_timeframe?: string;
}

function parseStrategyLabel(strategy: string, paramsJson: string | null): string {
  if (strategy === 'sma_cross') {
    try {
      const p: SmaParams = paramsJson ? JSON.parse(paramsJson) : {};
      const fast = p.fast_period ?? '?';
      const slow = p.slow_period ?? '?';
      const tf   = p.ohlcv_timeframe ?? '';
      const frac = p.quote_fraction != null ? ` ${(p.quote_fraction * 100).toFixed(0)}%` : '';
      return `SMA ${fast}/${slow}${tf ? ' · ' + tf : ''}${frac}`;
    } catch {
      return 'SMA Cross';
    }
  }
  if (strategy.startsWith('magi_ensemble')) {
    try {
      const p: Record<string, unknown> = paramsJson ? JSON.parse(paramsJson) : {};
      const tf     = typeof p.ohlcv_timeframe === 'string' ? p.ohlcv_timeframe : '';
      const voters = Array.isArray(p.voters) ? p.voters.length : '?';
      const mode   = typeof p.consensus_mode === 'string' ? p.consensus_mode : '';
      const score  = typeof p.consensus_threshold === 'number'
        ? ` ≥${(p.consensus_threshold * 100).toFixed(0)}%`
        : '';
      return `Magi ${voters}v · ${tf}${mode ? ' · ' + mode : ''}${score}`;
    } catch {
      return 'Magi Ensemble';
    }
  }
  return strategy.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

export default function Dashboard() {
  const navigate = useNavigate();
  const tickers = useRealtimeStore((state) => state.marketTickers);
  const trackedTickers = useRealtimeStore((state) => state.trackedTickers);
  const bots = useRealtimeStore((state) => state.bots);
  const botExecutionMode = useRealtimeStore((state) => state.tradingSettings?.execution_mode ?? null);
  const [wallet, setWallet] = useState<WalletItem[]>([]);
  const [selectedWalletView, setSelectedWalletView] = useState<NetworkView | null>(null);
  const walletView: NetworkView = selectedWalletView ?? (botExecutionMode === 'live' ? 'live' : 'testnet');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);

  useEffect(() => {
    // Only show the full-page spinner on the very first load.
    // Subsequent re-fetches (walletView switch etc.) update silently so the
    // UI never goes blank while waiting for the Binance balance call.
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10_000);

    fetch(`${API_BASE}/api/wallet/balances?view=${walletView}`, {
      signal: controller.signal,
    })
      .then(async (res) => {
        clearTimeout(timeout);
        if (!res.ok) {
          const errData = await res.json().catch(() => ({}));
          const d = errData.detail;
          const msg =
            typeof d === 'string'
              ? d
              : d != null
                ? JSON.stringify(d)
                : 'Failed to fetch wallet balances. Are your API keys configured?';
          throw new Error(msg);
        }
        return res.json();
      })
      .then((data: { balances?: WalletItem[]; execution_mode?: string; wallet_view?: string }) => {
        if (data.balances) {
          setWallet(data.balances);
        }
        setLoading(false);
      })
      .catch((err: Error) => {
        clearTimeout(timeout);
        if (err.name === 'AbortError') {
          // Timeout — keep showing whatever data we already have
          setLoading(false);
          return;
        }
        setError(err.message);
        setLoading(false);
      });

    return () => { clearTimeout(timeout); controller.abort(); };
  }, [walletView]);

  const walletWithValues = wallet.map((item) => {
    let usdPrice = 0;
    if (['USDT', 'USDC', 'FDUSD', 'BUSD'].includes(item.asset)) {
      usdPrice = 1;
    } else {
      const ticker = tickers[`${item.asset}USDT`];
      if (ticker) {
        usdPrice = parseFloat(ticker.price);
      }
    }
    return { ...item, value: item.total * usdPrice };
  });

  const totalWalletValue = walletWithValues.reduce((acc, curr) => acc + (curr.value || 0), 0);

  const viewLabel = walletView === 'live' ? 'Mainnet' : 'Testnet';

  // Bot aggregates
  const runningBots   = bots.filter((b) => b.status === 'running');
  const liveBots      = runningBots.filter((b) => b.execution_mode === 'live').length;
  const simBots       = runningBots.filter((b) => b.execution_mode !== 'live').length;
  const totalPnl      = bots.reduce((s, b) => s + (b.realized_pnl_quote ?? 0), 0);
  const totalBudget   = bots.reduce((s, b) => s + (b.initial_budget_quote ?? 0), 0);
  const totalTrades   = bots.reduce((s, b) => s + (b.closed_trades ?? 0), 0);
  const botsWithWR    = bots.filter((b) => b.win_rate_pct != null && (b.closed_trades ?? 0) > 0);
  const avgWR         = botsWithWR.length > 0
    ? botsWithWR.reduce((s, b) => s + (b.win_rate_pct ?? 0), 0) / botsWithWR.length
    : null;
  const pnlPositive   = totalPnl >= 0;

  return (
    <main className="flex-1 overflow-y-auto p-6 bg-background text-white">
      <div className="w-full flex flex-col">
        <h1 className="text-2xl font-bold mb-6">Dashboard</h1>

        {/* Top Stats */}
        <div className="grid grid-cols-4 gap-4 mb-6">
          {/* Wallet value */}
          <div className="bg-panel border border-border p-5 rounded-custom shadow-md">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-400 mb-2">Total Value (Est)</h3>
            <div className="text-2xl font-mono font-bold">
              {error
                ? '---'
                : `$${totalWalletValue.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
            </div>
            {walletView && (
              <p className="text-xs text-gray-500 mt-1">Based on {viewLabel} wallet & matching spot prices</p>
            )}
          </div>

          {/* Active bots (live) */}
          <div className="bg-panel border border-border p-5 rounded-custom shadow-md">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-400 mb-2">Active Bots</h3>
            <div className="text-2xl font-mono font-bold">
              {runningBots.length}
              <span className="text-sm font-sans ml-2 text-gray-400">/ {bots.length} total</span>
            </div>
            <p className="text-xs mt-1 text-gray-500">
              {liveBots > 0 && <span className="text-red-400 font-semibold">{liveBots} Live </span>}
              {simBots > 0 && <span className="text-blue-400 font-semibold">{simBots} Testnet</span>}
              {runningBots.length === 0 && 'None running'}
            </p>
          </div>

          {/* Realized P&L across all bots */}
          <div className="bg-panel border border-border p-5 rounded-custom shadow-md">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-400 mb-2">Bot Realized P&L</h3>
            <div className={`text-2xl font-mono font-bold ${pnlPositive ? 'text-green-400' : 'text-red-400'}`}>
              {bots.length === 0
                ? '---'
                : `${pnlPositive ? '+' : ''}${totalPnl.toFixed(4)} USDT`}
            </div>
            <p className="text-xs mt-1 text-gray-500">
              {totalTrades} closed trades · budget {totalBudget.toLocaleString()} USDT
            </p>
          </div>

          {/* Win rate */}
          <div className="bg-panel border border-border p-5 rounded-custom shadow-md">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-400 mb-2">Avg Win Rate</h3>
            <div className="text-2xl font-mono font-bold">
              {avgWR != null ? `${avgWR.toFixed(1)}%` : '—'}
            </div>
            <p className="text-xs mt-1 text-gray-500">
              across {botsWithWR.length} bot{botsWithWR.length !== 1 ? 's' : ''} with trades
            </p>
          </div>
        </div>

        {/* Bot Performance Table */}
        {bots.length > 0 && (
          <div className="bg-panel border border-border rounded-custom shadow-md mb-6 overflow-hidden">
            <div className="px-5 py-3 border-b border-border flex items-center justify-between">
              <h2 className="text-sm font-bold uppercase tracking-wider text-gray-300">Bot Performance</h2>
              <Link to="/bots" className="text-xs text-primary hover:underline">Manage →</Link>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-gray-500 text-xs uppercase">
                    <th className="px-5 py-2 text-left font-semibold">Bot</th>
                    <th className="px-4 py-2 text-left font-semibold">Pair</th>
                    <th className="px-4 py-2 text-left font-semibold">Strategy</th>
                    <th className="px-4 py-2 text-left font-semibold">Status</th>
                    <th className="px-4 py-2 text-right font-semibold">Budget</th>
                    <th className="px-4 py-2 text-right font-semibold">Realized P&L</th>
                    <th className="px-4 py-2 text-right font-semibold">Win Rate</th>
                    <th className="px-4 py-2 text-right font-semibold">Trades</th>
                    <th className="px-4 py-2 text-right font-semibold">Uptime</th>
                  </tr>
                </thead>
                <tbody>
                  {bots.map((bot) => {
                    const pnl = bot.realized_pnl_quote ?? 0;
                    const positive = pnl >= 0;
                    return (
                      <tr
                        key={bot.bot_id}
                        onClick={() => navigate(`/bots/${bot.bot_id}`)}
                        className="border-b border-border/40 hover:bg-primary/10 cursor-pointer transition-colors group"
                      >
                        <td className="px-5 py-2.5">
                          <span className="font-semibold text-white group-hover:text-primary transition-colors">
                            {bot.name}
                          </span>
                        </td>
                        <td className="px-4 py-2.5 text-gray-400 font-mono text-xs">{bot.symbol}</td>
                        <td className="px-4 py-2.5">
                          <span className="text-xs font-mono text-primary/90 bg-primary/10 border border-primary/20 px-2 py-0.5 rounded">
                            {parseStrategyLabel(bot.strategy, bot.strategy_params_json ?? null)}
                          </span>
                        </td>
                        <td className="px-4 py-2.5">
                          <span className={`text-[9px] font-bold uppercase tracking-widest px-2 py-0.5 rounded ${
                            bot.status === 'running' ? 'bg-green-500/20 text-green-400 border border-green-500/30'
                            : bot.status === 'paused' ? 'bg-amber-500/20 text-amber-400 border border-amber-500/30'
                            : 'bg-gray-500/20 text-gray-400 border border-gray-500/30'
                          }`}>
                            {bot.status}
                          </span>
                          {bot.execution_mode === 'live' && (
                            <span className="ml-1.5 text-[9px] font-black uppercase px-1.5 py-0.5 rounded bg-red-500/20 text-red-400 border border-red-500/30">LIVE</span>
                          )}
                        </td>
                        <td className="px-4 py-2.5 text-right font-mono text-gray-300 text-xs">
                          {bot.initial_budget_quote != null ? `${bot.initial_budget_quote.toLocaleString()} USDT` : '—'}
                        </td>
                        <td className={`px-4 py-2.5 text-right font-mono text-xs font-bold ${positive ? 'text-green-400' : 'text-red-400'}`}>
                          {bot.realized_pnl_quote != null
                            ? `${positive ? '+' : ''}${pnl.toFixed(4)} USDT`
                            : '—'}
                        </td>
                        <td className="px-4 py-2.5 text-right font-mono text-xs text-gray-300">
                          {bot.win_rate_pct != null ? `${bot.win_rate_pct.toFixed(1)}%` : '—'}
                        </td>
                        <td className="px-4 py-2.5 text-right font-mono text-xs text-gray-400">
                          {bot.closed_trades ?? 0}
                        </td>
                        <td className="px-4 py-2.5 text-right font-mono text-xs text-gray-400">
                          {formatUptime(bot.started_at ?? null, bot.status)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        <div className="grid grid-cols-2 gap-6 min-h-0" style={{ minHeight: '320px' }}>
          {/* Wallet Data Section */}
          <div className="bg-panel border border-border p-6 rounded-custom overflow-y-auto shadow-md flex flex-col">
            <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
              <h2 className="text-xl font-bold flex items-center gap-2">
                <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5 text-gray-400 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 10h18M7 15h1m4 0h1m-7 4h12a3 3 0 003-3V8a3 3 0 00-3-3H6a3 3 0 00-3 3v8a3 3 0 003 3z" />
                </svg>
                Spot wallet
                {walletView && (
                  <span
                    className={`text-xs font-bold uppercase px-2 py-0.5 rounded ${
                      walletView === 'live' ? 'bg-red-500/20 text-red-400' : 'bg-blue-500/20 text-blue-400'
                    }`}
                  >
                    {viewLabel}
                  </span>
                )}
              </h2>
              <div
                className="flex rounded-lg border border-border bg-surface p-0.5 shrink-0"
                role="group"
                aria-label="Wallet network"
              >
                <button
                  type="button"
                  aria-pressed={walletView === 'testnet'}
                  onClick={() => setSelectedWalletView('testnet')}
                  className={`px-3 py-1.5 text-xs font-bold rounded-md transition-colors ${
                    walletView === 'testnet'
                      ? 'bg-primary text-white'
                      : 'text-gray-400 hover:text-white'
                  }`}
                >
                  Testnet
                </button>
                <button
                  type="button"
                  aria-pressed={walletView === 'live'}
                  onClick={() => setSelectedWalletView('live')}
                  className={`px-3 py-1.5 text-xs font-bold rounded-md transition-colors ${
                    walletView === 'live'
                      ? 'bg-red-600 text-white'
                      : 'text-gray-400 hover:text-white'
                  }`}
                >
                  Mainnet
                </button>
              </div>
            </div>
            {botExecutionMode && walletView && botExecutionMode !== walletView && (
              <p className="text-[11px] text-amber-400/90 mb-3">
                Trading bots use Settings mode:{' '}
                <span className="font-semibold">{botExecutionMode === 'live' ? 'Mainnet' : 'Testnet'}</span>. This
                toggle is display-only for the dashboard.
              </p>
            )}
            <div className="flex-1 overflow-auto pr-2">
              {loading ? (
                <div className="text-gray-400 py-4 flex items-center gap-2">
                  <span className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
                  Loading balances…
                </div>
              ) : null}

              {error && (
                <div className="text-red-400 py-4 bg-red-900/20 p-4 rounded-md border border-red-500/30">
                  <p className="font-bold mb-1 text-white">Could not load {viewLabel} wallet</p>
                  <p className="text-sm text-red-200/90 whitespace-pre-line font-mono leading-relaxed">{error}</p>
                  <div className="mt-4 text-sm text-gray-300 space-y-1">
                    <p className="font-semibold text-gray-400">Checklist</p>
                    <p>
                      1. In <code className="bg-black/50 px-1 rounded">.env</code>, use{' '}
                      <code className="bg-black/50 px-1 rounded">BINANCE_TESTNET_API_KEY</code> for Testnet and{' '}
                      <code className="bg-black/50 px-1 rounded">BINANCE_API_KEY</code> for Mainnet.
                    </p>
                    <p>
                      2. API key needs <strong className="text-gray-200">Enable Reading</strong>. Check IP whitelist.
                    </p>
                    <p>
                      3. Restart the backend after editing <code className="bg-black/50 px-1 rounded">.env</code>.
                    </p>
                  </div>
                </div>
              )}

              {!loading && !error && (
                <table className="w-full text-left border-collapse">
                  <thead>
                    <tr className="border-b border-border text-gray-400 text-sm uppercase">
                      <th className="pb-3 font-semibold">Asset</th>
                      <th className="pb-3 font-semibold text-right">Balance</th>
                      <th className="pb-3 font-semibold text-right">Est. USD Value</th>
                    </tr>
                  </thead>
                  <tbody>
                    {walletWithValues.length === 0 ? (
                      <tr>
                        <td colSpan={3} className="py-8 text-center text-gray-500 italic">
                          No assets found or balances are 0
                        </td>
                      </tr>
                    ) : (
                      walletWithValues.map((item) => (
                        <tr key={item.asset} className="border-b border-border/50 hover:bg-border/30 transition-colors">
                          <td className="py-4 font-medium flex items-center gap-2">
                            <div className="w-8 h-8 rounded-full bg-border flex items-center justify-center text-xs font-bold text-gray-300">
                              {item.asset.substring(0, 3)}
                            </div>
                            {item.asset}
                          </td>
                          <td className="py-4 text-right font-mono text-gray-300">
                            {item.total.toLocaleString(undefined, { maximumFractionDigits: 6 })}
                          </td>
                          <td className="py-4 text-right font-mono">
                            {item.value !== undefined && item.value > 0 ? (
                              `$${item.value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
                            ) : (
                              <span className="text-gray-600">Syncing...</span>
                            )}
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              )}
            </div>
          </div>

          {/* Spot Data Section */}
          <div className="bg-panel border border-border p-6 rounded-custom overflow-y-auto shadow-md flex flex-col">
            <h2 className="text-xl font-bold mb-1 flex items-center gap-2">
              <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" />
              </svg>
              Spot markets
              {walletView && (
                <span className={`text-xs font-bold uppercase px-2 py-0.5 rounded ml-1 ${walletView === 'live' ? 'bg-red-500/15 text-red-400' : 'bg-blue-500/15 text-blue-400'}`}>
                  {viewLabel}
                </span>
              )}
            </h2>
            <p className="text-xs text-gray-500 mb-4">Eight tracked pairs — prices from the same network as the wallet toggle.</p>
            <div className="flex-1 overflow-auto pr-2">
              <table className="w-full text-left border-collapse">
                <thead>
                  <tr className="border-b border-border text-gray-400 text-sm uppercase">
                    <th className="pb-3 font-semibold">Pair</th>
                    <th className="pb-3 font-semibold text-right">Price</th>
                    <th className="pb-3 font-semibold text-right">24h Change</th>
                  </tr>
                </thead>
                <tbody>
                  {trackedTickers.map((symbol) => {
                    const data = tickers[symbol];
                    const isPositive = data && parseFloat(data.changePercent) >= 0;

                    return (
                      <tr key={symbol} className="border-b border-border/50 hover:bg-border/30 transition-colors">
                        <td className="py-4 font-medium flex items-center gap-2">
                          <span className="font-bold">{symbol.replace('USDT', '')}</span>
                          <span className="text-xs text-gray-500">/USDT</span>
                        </td>
                        <td className="py-4 text-right font-mono">
                          {data ? `$${data.price}` : <span className="text-gray-500">Loading...</span>}
                        </td>
                        <td className={`py-4 text-right font-mono ${!data ? 'text-gray-500' : isPositive ? 'text-green-500' : 'text-red-500'}`}>
                          {data ? (
                            <span className="flex items-center justify-end gap-1">
                              {isPositive ? '↑' : '↓'}
                              {Math.abs(parseFloat(data.changePercent))}%
                            </span>
                          ) : (
                            '...'
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </main>
  );
}
