import { useState, useEffect, useCallback } from "react";
import LiveCryptoFeed from "../components/LiveCryptoFeed";
import { API_BASE } from "../config";

interface TableDist {
  table: string;
  rows: number;
  pct: number;
}

interface DbStats {
  file_size_mb: number;
  total_ticks: number;
  total_orders: number;
  distribution: TableDist[];
}

const TABLE_COLORS: Record<string, string> = {
  market_ticks:  "bg-primary",
  bot_orders:    "bg-green-500",
  bot_logs:      "bg-yellow-500",
  bot_decisions: "bg-purple-500",
  market_depth:  "bg-blue-400",
  bots:          "bg-gray-400",
};

const TABLE_LABEL: Record<string, string> = {
  market_ticks:  "Market Ticks",
  bot_orders:    "Bot Orders",
  bot_logs:      "Bot Logs",
  bot_decisions: "Bot Decisions",
  market_depth:  "Market Depth",
  bots:          "Bots",
};

function fmt(n: number): string {
  return n.toLocaleString("en-US");
}

export default function Database() {
  const [stats, setStats]       = useState<DbStats | null>(null);
  const [statsErr, setStatsErr] = useState(false);
  const [purging, setPurging]   = useState(false);
  const [purgeMsg, setPurgeMsg] = useState<string | null>(null);

  const loadStats = useCallback(() => {
    fetch(`${API_BASE}/api/db/stats`)
      .then((r) => r.json())
      .then((d: DbStats) => { setStats(d); setStatsErr(false); })
      .catch(() => setStatsErr(true));
  }, []);

  useEffect(() => {
    loadStats();
    const id = setInterval(loadStats, 30_000);
    return () => clearInterval(id);
  }, [loadStats]);

  const handlePurge = async () => {
    if (!confirm("Delete all bot_logs and bot_orders for testnet bots? This cannot be undone.")) return;
    setPurging(true);
    setPurgeMsg(null);
    try {
      const res = await fetch(`${API_BASE}/api/data/purge-sim-logs`, { method: "POST" });
      const d = await res.json();
      setPurgeMsg(
        `Purged ${d.deleted_logs.toLocaleString()} logs + ${d.deleted_orders.toLocaleString()} orders across ${d.bots_affected} testnet bot(s).`
      );
      loadStats();
    } catch {
      setPurgeMsg("Purge failed — check backend logs.");
    } finally {
      setPurging(false);
    }
  };

  return (
    <main className="flex-1 overflow-y-auto p-6">
      {/* Header */}
      <div className="flex justify-between items-center mb-6">
        <div>
          <h1 className="text-2xl font-bold text-white">Database Management</h1>
          <p className="text-sm text-gray-400">Local SQLite instance: data/magitrader.db</p>
        </div>
        <div className="flex gap-3">
          <button className="px-4 py-2 bg-surface border border-border text-white rounded-custom text-sm font-bold hover:bg-gray-800 transition-all flex items-center gap-2 opacity-50 cursor-not-allowed" disabled title="Coming soon">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth="2"><path strokeLinecap="round" strokeLinejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" /></svg>
            Export Parquet
          </button>
          <button className="px-4 py-2 bg-surface border border-border text-white rounded-custom text-sm font-bold hover:bg-gray-800 transition-all flex items-center gap-2 opacity-50 cursor-not-allowed" disabled title="Coming soon">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth="2"><path strokeLinecap="round" strokeLinejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" /></svg>
            Backup DB
          </button>
        </div>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-3 gap-6 mb-8">
        <div className="bg-panel border border-border p-6 rounded-custom flex flex-col items-center justify-center">
          <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-1">File Size</h3>
          <div className="text-2xl font-mono font-bold text-white">
            {statsErr ? '—' : stats ? `${stats.file_size_mb} MB` : '…'}
          </div>
        </div>
        <div className="bg-panel border border-border p-6 rounded-custom flex flex-col items-center justify-center">
          <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-1">Total Ticks Logged</h3>
          <div className="text-2xl font-mono font-bold text-white">
            {statsErr ? '—' : stats ? fmt(stats.total_ticks) : '…'}
          </div>
        </div>
        <div className="bg-panel border border-border p-6 rounded-custom flex flex-col items-center justify-center">
          <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-1">Total Bot Orders</h3>
          <div className="text-2xl font-mono font-bold text-white">
            {statsErr ? '—' : stats ? fmt(stats.total_orders) : '…'}
          </div>
        </div>
      </div>

      {/* Live feed */}
      <div className="mb-8">
        <LiveCryptoFeed />
      </div>

      {/* Storage Distribution */}
      <div className="bg-panel border border-border rounded-custom overflow-hidden flex flex-col mb-6">
        <div className="p-4 border-b border-border flex justify-between items-center bg-surface/50">
          <h3 className="text-sm font-bold text-white uppercase tracking-wider">Storage Distribution</h3>
          <span className="text-xs text-gray-500">Row counts · refreshes every 30s</span>
        </div>
        <div className="p-6 flex flex-col gap-4">
          {!stats && !statsErr && (
            <p className="text-gray-500 text-sm">Loading…</p>
          )}
          {statsErr && (
            <p className="text-red-400 text-sm">Could not load stats — is the backend running?</p>
          )}
          {stats && stats.distribution.map((row) => (
            <div key={row.table} className="w-full">
              <div className="flex justify-between text-xs mb-1">
                <span className="text-gray-300 font-medium">
                  {TABLE_LABEL[row.table] ?? row.table}
                </span>
                <span className="text-gray-500 font-mono">
                  {fmt(row.rows)} rows ({row.pct}%)
                </span>
              </div>
              <div className="w-full bg-surface rounded-full h-2">
                <div
                  className={`${TABLE_COLORS[row.table] ?? 'bg-gray-500'} h-2 rounded-full transition-all`}
                  style={{ width: `${Math.max(row.pct, 0.5)}%` }}
                />
              </div>
            </div>
          ))}
          {stats && stats.distribution.length === 0 && (
            <p className="text-gray-500 text-sm italic">No data collected yet.</p>
          )}

          {/* Footer: collection status + purge */}
          <div className="mt-4 pt-4 border-t border-border flex justify-between items-start gap-4">
            <div className="flex flex-col gap-1">
              <p className="text-xs text-gray-400">
                ML Data collection is{' '}
                <span className="text-green-400 font-bold">ALWAYS ACTIVE</span>
                {' '}— managed automatically alongside the trading engine.
              </p>
              {purgeMsg && (
                <p className={`text-xs font-mono ${purgeMsg.startsWith('Purge failed') ? 'text-red-400' : 'text-green-400'}`}>
                  {purgeMsg}
                </p>
              )}
            </div>
            <button
              onClick={handlePurge}
              disabled={purging}
              className="shrink-0 text-xs text-red-500 hover:text-red-400 transition-colors underline disabled:opacity-50"
            >
              {purging ? 'Purging…' : 'Purge Simulation Logs'}
            </button>
          </div>
        </div>
      </div>
    </main>
  );
}
