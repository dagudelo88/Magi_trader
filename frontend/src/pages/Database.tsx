import { useState, useEffect } from "react";
import LiveCryptoFeed from "../components/LiveCryptoFeed";

export default function Database() {
  const [isActive, setIsActive] = useState(false);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    fetchStatus();
  }, []);

  const fetchStatus = async () => {
    try {
      const res = await fetch("http://localhost:8000/api/data/status");
      const data = await res.json();
      setIsActive(data.active);
    } catch (e) {
      console.error("Error fetching status", e);
    }
  };

  const toggleCollection = async () => {
    setLoading(true);
    try {
      const endpoint = isActive ? "/api/data/stop" : "/api/data/start";
      const res = await fetch(`http://localhost:8000${endpoint}`, { method: "POST" });
      const data = await res.json();
      setIsActive(data.active);
    } catch (e) {
      console.error("Error toggling collection", e);
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="flex-1 flex flex-col overflow-hidden p-6">
      <div className="flex justify-between items-center mb-6">
        <div>
          <h1 className="text-2xl font-bold text-white">Database Management</h1>
          <p className="text-sm text-gray-400">Local SQLite instance: data/magitrader.db</p>
        </div>
        <div className="flex gap-3">
          <button className="px-4 py-2 bg-surface border border-border text-white rounded-custom text-sm font-bold hover:bg-gray-800 transition-all flex items-center gap-2">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth="2"><path strokeLinecap="round" strokeLinejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"></path></svg>
            Export Parquet
          </button>
          <button className="px-4 py-2 bg-surface border border-border text-white rounded-custom text-sm font-bold hover:bg-gray-800 transition-all flex items-center gap-2">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth="2"><path strokeLinecap="round" strokeLinejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12"></path></svg>
            Backup DB
          </button>
        </div>
      </div>
      
      <div className="grid grid-cols-3 gap-6 mb-8">
        <div className="bg-panel border border-border p-6 rounded-custom flex flex-col items-center justify-center">
          <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-1">File Size</h3>
          <div className="text-2xl font-mono font-bold text-white">24.5 MB</div>
        </div>
        <div className="bg-panel border border-border p-6 rounded-custom flex flex-col items-center justify-center">
          <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-1">Total Ticks Logged</h3>
          <div className="text-2xl font-mono font-bold text-white">1,240,592</div>
        </div>
        <div className="bg-panel border border-border p-6 rounded-custom flex flex-col items-center justify-center">
          <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-1">Total Trades</h3>
          <div className="text-2xl font-mono font-bold text-white">14,204</div>
        </div>
      </div>

      <div className="mb-8">
        <LiveCryptoFeed />
      </div>

      <div className="flex-1 bg-panel border border-border rounded-custom overflow-hidden flex flex-col">
        <div className="p-4 border-b border-border flex justify-between items-center bg-surface/50">
          <h3 className="text-sm font-bold text-white uppercase tracking-wider">Storage Distribution</h3>
          <select className="bg-surface border border-border text-white text-xs px-2 py-1 rounded outline-none focus:border-primary">
            <option>All Tables</option>
            <option>market_ticks</option>
            <option>bot_decisions</option>
            <option>market_depth</option>
          </select>
        </div>
        <div className="flex-1 p-6 flex flex-col gap-4">
          <div className="w-full">
            <div className="flex justify-between text-xs mb-1">
              <span className="text-gray-400">market_ticks</span>
              <span className="text-gray-500 font-mono">18.2 MB (74%)</span>
            </div>
            <div className="w-full bg-surface rounded-full h-2">
              <div className="bg-primary h-2 rounded-full" style={{ width: '74%' }}></div>
            </div>
          </div>
          <div className="w-full">
            <div className="flex justify-between text-xs mb-1">
              <span className="text-gray-400">bot_decisions</span>
              <span className="text-gray-500 font-mono">5.1 MB (20%)</span>
            </div>
            <div className="w-full bg-surface rounded-full h-2">
              <div className="bg-green-500 h-2 rounded-full" style={{ width: '20%' }}></div>
            </div>
          </div>
          <div className="w-full">
            <div className="flex justify-between text-xs mb-1">
              <span className="text-gray-400">market_depth</span>
              <span className="text-gray-500 font-mono">1.2 MB (6%)</span>
            </div>
            <div className="w-full bg-surface rounded-full h-2">
              <div className="bg-yellow-500 h-2 rounded-full" style={{ width: '6%' }}></div>
            </div>
          </div>
          
          <div className="mt-auto pt-6 border-t border-border flex justify-between items-center">
            <div className="flex items-center gap-4">
              <p className="text-xs text-gray-500">
                ML Data collection is currently{" "}
                <span className={isActive ? "text-green-500 font-bold" : "text-red-500 font-bold"}>
                  {isActive ? "ACTIVE" : "STOPPED"}
                </span>
              </p>
              <button 
                onClick={toggleCollection}
                disabled={loading}
                className={`px-3 py-1 text-xs font-bold rounded ${
                  isActive 
                    ? "bg-red-500/10 text-red-500 hover:bg-red-500/20" 
                    : "bg-green-500/10 text-green-500 hover:bg-green-500/20"
                } transition-colors border ${
                  isActive ? "border-red-500/20" : "border-green-500/20"
                }`}
              >
                {loading ? "..." : isActive ? "Stop Collection" : "Start Collection"}
              </button>
            </div>
            <button className="text-xs text-red-500 hover:text-red-400 transition-colors underline">Purge Simulation Logs</button>
          </div>
        </div>
      </div>
    </main>
  );
}
