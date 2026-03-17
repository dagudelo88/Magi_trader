import { useParams } from 'react-router-dom';

export default function BotDetail() {
  const { id } = useParams();

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* BEGIN: Main Content Area */}
      <main className="flex-1 flex overflow-hidden">
        {/* Left Side - Analytics */}
        <section className="flex-1 flex flex-col p-6 overflow-y-auto border-r border-border" data-purpose="performance-analytics">
          {/* Top Meta Info */}
          <div className="flex justify-between items-start mb-8">
            <div>
              <h1 className="text-2xl font-bold text-white mb-1">Alpha_Trend_v4 <span className="text-sm font-normal text-gray-500 ml-2">#{id}-LIVE</span></h1>
              <p className="text-gray-400 text-sm">Deployment: AWS-USE-1 | Strategy: Mean Reversion Scalper</p>
            </div>
            <div className="text-right">
              <div className="text-xs text-gray-500 uppercase tracking-widest font-semibold mb-1">Total PnL</div>
              <div className="text-3xl font-mono font-bold text-green-400">+$12,450.82</div>
            </div>
          </div>
          {/* Performance Cards Grid */}
          <div className="grid grid-cols-4 gap-4 mb-8">
            <div className="bg-panel border border-border p-4 rounded-custom" data-purpose="metric-card">
              <div className="text-xs text-gray-500 mb-1">Sharpe Ratio</div>
              <div className="text-xl font-mono font-semibold text-white">2.41</div>
            </div>
            <div className="bg-panel border border-border p-4 rounded-custom" data-purpose="metric-card">
              <div className="text-xs text-gray-500 mb-1">Max Drawdown</div>
              <div className="text-xl font-mono font-semibold text-red-400">-4.2%</div>
            </div>
            <div className="bg-panel border border-border p-4 rounded-custom" data-purpose="metric-card">
              <div className="text-xs text-gray-500 mb-1">Win Rate</div>
              <div className="text-xl font-mono font-semibold text-white">68.5%</div>
            </div>
            <div className="bg-panel border border-border p-4 rounded-custom" data-purpose="metric-card">
              <div className="text-xs text-gray-500 mb-1">Avg Trade</div>
              <div className="text-xl font-mono font-semibold text-white">$142.00</div>
            </div>
          </div>
          {/* Equity Curve Placeholder */}
          <div className="flex-1 min-h-[400px] bg-panel border border-border rounded-custom p-6 relative overflow-hidden" data-purpose="equity-chart-container">
            <div className="flex justify-between items-center mb-6">
              <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400">Historical Equity Curve</h3>
              <div className="flex gap-2">
                <button className="px-3 py-1 text-xs bg-surface border border-border rounded hover:border-primary transition-colors">1D</button>
                <button className="px-3 py-1 text-xs bg-primary text-white rounded">1W</button>
                <button className="px-3 py-1 text-xs bg-surface border border-border rounded hover:border-primary transition-colors">1M</button>
              </div>
            </div>
            {/* Mock Chart Visualization */}
            <div className="absolute inset-x-6 bottom-10 top-20 flex items-end gap-[2px]">
              <div className="flex-1 bg-primary/20 hover:bg-primary/40 transition-colors" style={{ height: "30%" }}></div>
              <div className="flex-1 bg-primary/20 hover:bg-primary/40 transition-colors" style={{ height: "35%" }}></div>
              <div className="flex-1 bg-primary/20 hover:bg-primary/40 transition-colors" style={{ height: "32%" }}></div>
              <div className="flex-1 bg-primary/20 hover:bg-primary/40 transition-colors" style={{ height: "45%" }}></div>
              <div className="flex-1 bg-primary/20 hover:bg-primary/40 transition-colors" style={{ height: "40%" }}></div>
              <div className="flex-1 bg-primary/20 hover:bg-primary/40 transition-colors" style={{ height: "55%" }}></div>
              <div className="flex-1 bg-primary/20 hover:bg-primary/40 transition-colors" style={{ height: "52%" }}></div>
              <div className="flex-1 bg-primary/20 hover:bg-primary/40 transition-colors" style={{ height: "60%" }}></div>
              <div className="flex-1 bg-primary/20 hover:bg-primary/40 transition-colors" style={{ height: "75%" }}></div>
              <div className="flex-1 bg-primary/20 hover:bg-primary/40 transition-colors" style={{ height: "70%" }}></div>
              <div className="flex-1 bg-primary/20 hover:bg-primary/40 transition-colors" style={{ height: "85%" }}></div>
              <div className="flex-1 bg-primary/20 hover:bg-primary/40 transition-colors" style={{ height: "90%" }}></div>
              <div className="flex-1 bg-primary/20 hover:bg-primary/40 transition-colors" style={{ height: "82%" }}></div>
              <div className="flex-1 bg-primary/20 hover:bg-primary/40 transition-colors" style={{ height: "95%" }}></div>
              <div className="flex-1 bg-primary/20 hover:bg-primary/40 transition-colors" style={{ height: "100%" }}></div>
            </div>
            {/* Y-Axis labels */}
            <div className="absolute left-6 top-20 bottom-10 flex flex-col justify-between text-[10px] text-gray-600 font-mono pointer-events-none">
              <span>$150k</span>
              <span>$125k</span>
              <span>$100k</span>
              <span>$75k</span>
              <span>$50k</span>
            </div>
          </div>
        </section>

        {/* Right Side - Live Logs */}
        <aside className="w-[450px] bg-black flex flex-col" data-purpose="execution-logs">
          <div className="p-4 border-b border-border bg-panel flex justify-between items-center">
            <h3 className="text-xs font-bold uppercase tracking-widest flex items-center gap-2">
              <span className="w-2 h-2 bg-primary rounded-full"></span>
              Real-time Logs
            </h3>
            <span className="text-[10px] text-gray-500 font-mono">v1.2.4-stable</span>
          </div>
          <div className="flex-1 overflow-y-auto p-4 font-mono text-[12px] leading-relaxed space-y-1" id="log-container">
            <div className="text-gray-500"><span className="text-primary">[2023-10-27 14:02:01]</span> <span className="text-blue-400">[LIVE]</span> System heartbeat initialized...</div>
            <div className="text-gray-300"><span className="text-primary">[2023-10-27 14:02:05]</span> <span className="text-blue-400">[LIVE]</span> Connecting to Binance WebSocket...</div>
            <div className="text-green-500"><span className="text-primary">[2023-10-27 14:02:06]</span> <span className="text-blue-400">[LIVE]</span> Connection established.</div>
            <div className="text-gray-300"><span className="text-primary">[2023-10-27 14:05:22]</span> <span className="text-blue-400">[LIVE]</span> Signal detected: BTC/USDT Long @ 34,201.50</div>
            <div className="text-yellow-500"><span className="text-primary">[2023-10-27 14:05:23]</span> <span className="text-blue-400">[LIVE]</span> Executing Market Buy Order #4421...</div>
            <div className="text-green-400 bg-green-500/5 px-1"><span className="text-primary">[2023-10-27 14:05:24]</span> <span className="text-blue-400">[LIVE]</span> ORDER FILLED: 0.52 BTC @ 34,202.10</div>
            <div className="text-gray-500"><span className="text-primary">[2023-10-27 14:10:00]</span> <span className="text-gray-400">[SIM]</span> Paper-trading validator check: SUCCESS</div>
            <div className="text-gray-300"><span className="text-primary">[2023-10-27 14:15:44]</span> <span className="text-blue-400">[LIVE]</span> Updating SL to 34,100.00</div>
            <div className="text-gray-300"><span className="text-primary">[2023-10-27 14:20:12]</span> <span className="text-blue-400">[LIVE]</span> Updating TP to 34,800.00</div>
            <div className="text-gray-500"><span className="text-primary">[2023-10-27 14:22:01]</span> <span className="text-blue-400">[LIVE]</span> Trailing stop activated (+1.2%)</div>
            <div className="text-gray-300"><span className="text-primary">[2023-10-27 14:25:30]</span> <span className="text-blue-400">[LIVE]</span> Signal intensity: 0.88 - Sustaining position</div>
            <div className="text-gray-300"><span className="text-primary">[2023-10-27 14:28:15]</span> <span className="text-blue-400">[LIVE]</span> RSI Overbought threshold approaching (68.4)</div>
            <div className="text-gray-500"><span className="text-primary">[2023-10-27 14:30:00]</span> <span className="text-gray-400">[SIM]</span> Shadow bot comparison: No variance detected.</div>
            <div className="text-yellow-500"><span className="text-primary">[2023-10-27 14:32:05]</span> <span className="text-blue-400">[LIVE]</span> Partial exit triggered (50% volume)</div>
            <div className="text-green-400"><span className="text-primary">[2023-10-27 14:32:06]</span> <span className="text-blue-400">[LIVE]</span> ORDER FILLED: 0.26 BTC @ 34,750.00</div>
            <div className="text-gray-500 italic">... monitoring market conditions ...</div>
          </div>
          <div className="p-4 bg-panel border-t border-border grid grid-cols-2 gap-2 text-[10px] font-mono">
            <div className="flex justify-between border-r border-border pr-2">
              <span className="text-gray-500">LATENCY:</span>
              <span className="text-green-400">14ms</span>
            </div>
            <div className="flex justify-between pl-2">
              <span className="text-gray-500">API:</span>
              <span className="text-green-400">OK</span>
            </div>
          </div>
        </aside>
      </main>

      {/* Lifecycle Controls Bar */}
      <footer className="h-20 bg-panel border-t border-border px-6 flex items-center justify-between shrink-0" data-purpose="lifecycle-controls">
        <div className="flex items-center gap-6">
          <div className="flex flex-col">
            <span className="text-[10px] text-gray-500 uppercase font-bold tracking-widest">Bot Status</span>
            <div className="flex items-center gap-2">
              <span className="w-3 h-3 bg-green-500 rounded-full shadow-[0_0_8px_rgba(34,197,94,0.6)]"></span>
              <span className="font-bold text-white uppercase tracking-tight">Active</span>
            </div>
          </div>
          <div className="h-8 w-px bg-border"></div>
          <div className="flex flex-col">
            <span className="text-[10px] text-gray-500 uppercase font-bold tracking-widest">Mode</span>
            <span className="font-bold text-blue-400 uppercase tracking-tight">Live Trading</span>
          </div>
        </div>
        <div className="flex gap-3">
          <button className="px-4 py-2 bg-surface border border-border rounded-custom text-sm font-semibold hover:bg-gray-800 transition-colors flex items-center gap-2">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M19 14l-7 7m0 0l-7-7m7 7V3" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2"></path></svg>
            Demote to Staging
          </button>
          <div className="w-px h-10 bg-border mx-2"></div>
          <button className="px-6 py-2 bg-yellow-600/10 border border-yellow-600/50 text-yellow-500 rounded-custom text-sm font-bold hover:bg-yellow-600 hover:text-white transition-all flex items-center gap-2">
            <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><path d="M6 4h4v16H6V4zm8 0h4v16h-4V4z"></path></svg>
            PAUSE
          </button>
          <button className="px-6 py-2 bg-red-600 border border-red-700 text-white rounded-custom text-sm font-bold hover:bg-red-700 shadow-lg shadow-red-900/20 transition-all flex items-center gap-2">
            <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"></path></svg>
            TERMINATE
          </button>
        </div>
      </footer>
    </div>
  );
}
