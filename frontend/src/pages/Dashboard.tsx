import { useState, useEffect, useRef } from 'react';

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

const SPOT_SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT', 'ADAUSDT'];

export default function Dashboard() {
  const [tickers, setTickers] = useState<Record<string, Ticker>>({});
  const [wallet, setWallet] = useState<WalletItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const walletRef = useRef<WalletItem[]>([]);
  walletRef.current = wallet;

  // Fetch real wallet balances from backend
  useEffect(() => {
    fetch('http://localhost:8000/api/wallet/balances')
      .then(async res => {
        if (!res.ok) {
          const errData = await res.json().catch(() => ({}));
          throw new Error(errData.detail || 'Failed to fetch wallet balances. Are your API keys configured?');
        }
        return res.json();
      })
      .then(data => {
        if (data.balances) {
          setWallet(data.balances);
        }
        setLoading(false);
      })
      .catch(err => {
        setError(err.message);
        setLoading(false);
      });
  }, []);

  // Single stream for the Dashboard lifetime; wallet pairs are read from walletRef (no reconnect when wallet loads).
  useEffect(() => {
    const ws = new WebSocket('wss://stream.binance.com:9443/ws/!miniTicker@arr');

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (!Array.isArray(data)) return;

        setTickers((prev) => {
          const next = { ...prev };
          let updated = false;
          const holdings = walletRef.current;

          data.forEach((t: any) => {
            const isSpotSymbol = SPOT_SYMBOLS.includes(t.s);
            const isWalletAssetPair = holdings.some((w) => t.s === `${w.asset}USDT`);

            if (isSpotSymbol || isWalletAssetPair) {
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
  }, []);

  const walletWithValues = wallet.map(item => {
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

  return (
    <main className="flex-1 flex overflow-hidden p-6 bg-background text-white">
      <div className="w-full h-full flex flex-col">
        <h1 className="text-2xl font-bold mb-6">Dashboard</h1>
        
        {/* Top Stats */}
        <div className="grid grid-cols-3 gap-6 mb-8">
          <div className="bg-panel border border-border p-6 rounded-custom shadow-md">
            <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400 mb-2">Total Value (Est)</h3>
            <div className="text-3xl font-mono font-bold">
              {error ? '---' : `$${totalWalletValue.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`}
            </div>
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
            <h2 className="text-xl font-bold mb-4 flex items-center gap-2">
              <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 10h18M7 15h1m4 0h1m-7 4h12a3 3 0 003-3V8a3 3 0 00-3-3H6a3 3 0 00-3 3v8a3 3 0 003 3z" />
              </svg>
              Real Spot Wallet Balances
            </h2>
            <div className="flex-1 overflow-auto pr-2">
              {loading && <div className="text-gray-400 py-4 flex items-center gap-2">
                <span className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin"></span>
                Loading real balances from exchange...
              </div>}
              
              {error && <div className="text-red-400 py-4 bg-red-900/20 p-4 rounded-md border border-red-500/30">
                <p className="font-bold mb-1">Configuration Required:</p>
                <p>{error}</p>
                <div className="mt-4 text-sm text-gray-300">
                  <p>1. Copy <code className="bg-black/50 px-1 rounded">backend/.env.example</code> to <code className="bg-black/50 px-1 rounded">backend/.env</code></p>
                  <p>2. Add <code className="bg-black/50 px-1 rounded">BINANCE_API_KEY</code> and <code className="bg-black/50 px-1 rounded">BINANCE_API_SECRET</code> (or <code className="bg-black/50 px-1 rounded">BINANCE_SECRET</code>) in repo root <code className="bg-black/50 px-1 rounded">.env</code> or <code className="bg-black/50 px-1 rounded">backend/.env</code></p>
                  <p>3. Restart the application backend</p>
                </div>
              </div>}

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
                        <td colSpan={3} className="py-8 text-center text-gray-500 italic">No assets found or balances are 0</td>
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
                          <td className="py-4 text-right font-mono text-gray-300">{item.total.toLocaleString(undefined, { maximumFractionDigits: 6 })}</td>
                          <td className="py-4 text-right font-mono">
                            {item.value !== undefined && item.value > 0 ? 
                              `$${item.value.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}` : 
                              <span className="text-gray-600">Syncing...</span>}
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
            <h2 className="text-xl font-bold mb-4 flex items-center gap-2">
              <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" />
              </svg>
              Live Spot Markets
            </h2>
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
                  {SPOT_SYMBOLS.map((symbol) => {
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
                        <td className={`py-4 text-right font-mono ${!data ? 'text-gray-500' : (isPositive ? 'text-green-500' : 'text-red-500')}`}>
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
