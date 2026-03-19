import { useState, useEffect, useRef } from 'react';
import { API_BASE } from '../config';
import { TRACKED_TICKER_SYMBOLS_FALLBACK } from '../trackedMarketsFallback';

interface Ticker {
  symbol: string;
  price: string;
  change: string;
  changePercent: string;
}

interface WalletItem {
  asset: string;
  free: number;
  used: number;
  total: number;
  value?: number;
}

type NetworkView = 'testnet' | 'live';

const MINI_TICKER_WS: Record<NetworkView, string> = {
  testnet: 'wss://stream.testnet.binance.vision/ws/!miniTicker@arr',
  live: 'wss://stream.binance.com:9443/ws/!miniTicker@arr',
};

export default function Dashboard() {
  const [tickers, setTickers] = useState<Record<string, Ticker>>({});
  const [wallet, setWallet] = useState<WalletItem[]>([]);
  const [trackedTickers, setTrackedTickers] = useState<string[]>([]);
  /** Which network the dashboard wallet + spot table use (independent toggle). */
  const [walletView, setWalletView] = useState<NetworkView | null>(null);
  /** Global Settings mode used for bots (returned with wallet for context). */
  const [botExecutionMode, setBotExecutionMode] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const walletRef = useRef<WalletItem[]>([]);
  walletRef.current = wallet;

  useEffect(() => {
    fetch(`${API_BASE}/api/market/tracked`)
      .then((r) => r.json())
      .then((d: { ticker_symbols?: string[] }) =>
        setTrackedTickers(Array.isArray(d.ticker_symbols) ? d.ticker_symbols : TRACKED_TICKER_SYMBOLS_FALLBACK)
      )
      .catch(() => setTrackedTickers(TRACKED_TICKER_SYMBOLS_FALLBACK));
  }, []);

  useEffect(() => {
    fetch(`${API_BASE}/api/settings/trading`)
      .then((r) => r.json())
      .then((s: { execution_mode?: string }) => {
        setWalletView(s.execution_mode === 'live' ? 'live' : 'testnet');
      })
      .catch(() => setWalletView('testnet'));
  }, []);

  useEffect(() => {
    if (walletView === null) return;

    setLoading(true);
    setError(null);
    fetch(`${API_BASE}/api/wallet/balances?view=${walletView}`)
      .then(async (res) => {
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
        if (data.execution_mode) {
          setBotExecutionMode(data.execution_mode);
        }
        setLoading(false);
      })
      .catch((err: Error) => {
        setError(err.message);
        setLoading(false);
      });
  }, [walletView]);

  useEffect(() => {
    if (walletView === null) return;
    setTickers({});
  }, [walletView]);

  useEffect(() => {
    if (walletView === null || trackedTickers.length === 0) return;

    const wsUrl = MINI_TICKER_WS[walletView];
    const ws = new WebSocket(wsUrl);

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (!Array.isArray(data)) return;

        setTickers((prev) => {
          const next = { ...prev };
          let updated = false;
          const holdings = walletRef.current;

          data.forEach((t: { s: string; c: string; o: string }) => {
            const isTracked = trackedTickers.includes(t.s);
            const isWalletAssetPair = holdings.some((w) => t.s === `${w.asset}USDT`);

            if (isTracked || isWalletAssetPair) {
              next[t.s] = {
                symbol: t.s,
                price: parseFloat(t.c).toFixed(2),
                change: (parseFloat(t.c) - parseFloat(t.o)).toFixed(2),
                changePercent: (((parseFloat(t.c) - parseFloat(t.o)) / parseFloat(t.o)) * 100).toFixed(2),
              };
              updated = true;
            }
          });

          return updated ? next : prev;
        });
      } catch {
        // ignore JSON parse errors
      }
    };

    return () => ws.close();
  }, [trackedTickers, walletView]);

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

  const viewLabel = walletView === 'live' ? 'Mainnet' : walletView === 'testnet' ? 'Testnet' : '…';

  return (
    <main className="flex-1 flex overflow-hidden p-6 bg-background text-white">
      <div className="w-full h-full flex flex-col">
        <h1 className="text-2xl font-bold mb-6">Dashboard</h1>

        {/* Top Stats */}
        <div className="grid grid-cols-3 gap-6 mb-8">
          <div className="bg-panel border border-border p-6 rounded-custom shadow-md">
            <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400 mb-2">Total Value (Est)</h3>
            <div className="text-3xl font-mono font-bold">
              {error || walletView === null
                ? '---'
                : `$${totalWalletValue.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
            </div>
            {walletView && (
              <p className="text-xs text-gray-500 mt-2">Based on {viewLabel} wallet & matching spot prices</p>
            )}
          </div>
          <div className="bg-panel border border-border p-6 rounded-custom shadow-md">
            <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400 mb-2">Active Bots</h3>
            <div className="text-3xl font-mono font-bold">4 <span className="text-sm text-green-500 font-sans ml-2">2 Live / 2 Sim</span></div>
          </div>
          <div className="bg-panel border border-border p-6 rounded-custom shadow-md">
            <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400 mb-2">24h PnL</h3>
            <div className="text-3xl font-mono font-bold text-green-400">+$412.50</div>
          </div>
        </div>

        <div className="flex-1 grid grid-cols-2 gap-6 min-h-0">
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
                  onClick={() => setWalletView('testnet')}
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
                  onClick={() => setWalletView('live')}
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
              {walletView === null || loading ? (
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
                  {(trackedTickers.length ? trackedTickers : TRACKED_TICKER_SYMBOLS_FALLBACK).map((symbol) => {
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
