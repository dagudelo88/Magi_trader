import { useState, useEffect, useCallback } from "react";
import LiveCryptoFeed from "../components/LiveCryptoFeed";
import { API_BASE } from "../config";

interface TableDist {
  table: string;
  rows: number;
  pct: number;
}

interface MetamagiStats {
  total_rows: number;
  unlabeled_rows: number;
}

interface DbStats {
  file_size_mb: number;
  total_ticks: number;
  total_orders: number;
  distribution: TableDist[];
  metamagi?: MetamagiStats;
}

interface MetamagiCatchupResponse {
  type?: string;
  batches_run: number;
  selected_rows: number;
  updated_label_cells: number;
  updated_forward_roc_30s: number;
  updated_forward_roc_5m: number;
  stopped_reason: string;
  elapsed_ms: number;
  lookback_minutes: number | null;
  lookback_scan?: string;
  max_seconds_cap: number | null;
  max_batches_cap: number | null;
  unlabeled_remaining_at_end?: number;
}

const TABLE_COLORS: Record<string, string> = {
  market_ticks:   "bg-primary",
  bot_orders:     "bg-green-500",
  bot_logs:       "bg-yellow-500",
  bot_decisions:  "bg-purple-500",
  voter_feedback: "bg-orange-400",
  market_depth:   "bg-blue-400",
  bots:           "bg-gray-400",
};

const TABLE_LABEL: Record<string, string> = {
  market_ticks:   "Market Ticks",
  bot_orders:     "Bot Orders",
  bot_logs:       "Bot Logs",
  bot_decisions:  "Bot Decisions",
  voter_feedback: "Voter Feedback",
  market_depth:   "Market Depth",
  bots:           "Bots",
};

function fmt(n: number): string {
  return n.toLocaleString("en-US");
}

/** Extract FastAPI `detail` from JSON error body when present. */
function parseFastApiErrorBody(raw: string): string {
  const trimmed = raw.trim();
  if (!trimmed) return "(empty response body)";
  try {
    const j = JSON.parse(trimmed) as { detail?: unknown };
    const d = j.detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d))
      return d
        .map((item) =>
          typeof item === "object" && item !== null && "msg" in item
            ? String((item as { msg: unknown }).msg)
            : JSON.stringify(item)
        )
        .join("; ");
    if (d != null) return JSON.stringify(d);
  } catch {
    /* not JSON */
  }
  return trimmed;
}

export default function Database() {
  const [stats, setStats]       = useState<DbStats | null>(null);
  const [statsErr, setStatsErr] = useState(false);
  const [purging, setPurging]   = useState(false);
  const [purgeMsg, setPurgeMsg] = useState<string | null>(null);
  const [labeling, setLabeling] = useState(false);
  const [labelMsg, setLabelMsg] = useState<string | null>(null);
  const [liveUnlabeled, setLiveUnlabeled] = useState<number | null>(null);
  const [liveBatch, setLiveBatch] = useState(0);
  const [livePhase, setLivePhase] = useState<string | null>(null);

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

  const handleMetamagiCatchup = async () => {
    if (
      !confirm(
        "Run MetaMagi label catch-up? This fills every missing forward ROC on voter_feedback using local market_ticks (no exchange calls). It runs until the backlog is cleared or rows cannot be labeled — large DBs may take several minutes. Progress (rows left, batch count) updates live in the UI and in the backend terminal (logger metamagi_catchup)."
      )
    ) {
      return;
    }
    setLabeling(true);
    setLabelMsg(null);
    setLiveUnlabeled(null);
    setLiveBatch(0);
    setLivePhase("connecting");
    try {
      const res = await fetch(`${API_BASE}/api/data/metamagi-label-catchup`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/x-ndjson",
        },
        body: "{}",
      });
      if (!res.ok) {
        const raw = await res.text();
        const detail = parseFastApiErrorBody(raw);
        console.error("[MetaMagi catch-up] HTTP error", res.status, detail);
        setLabelMsg(`MetaMagi catch-up failed (${res.status}): ${detail.slice(0, 500)}`);
        return;
      }
      const reader = res.body?.getReader();
      if (!reader) {
        setLabelMsg("MetaMagi catch-up failed: response had no readable stream.");
        console.error("[MetaMagi catch-up] missing body reader");
        return;
      }

      const acc = {
        streamHadError: false,
        summary: null as MetamagiCatchupResponse | null,
      };

      const handleEvent = (ev: Record<string, unknown>) => {
        const ty = ev.type;
        if (ty === "start") {
          setLivePhase("labeling");
          if (typeof ev.unlabeled_remaining === "number") {
            setLiveUnlabeled(ev.unlabeled_remaining);
          }
          return;
        }
        if (ty === "progress") {
          setLivePhase("labeling");
          if (typeof ev.unlabeled_remaining === "number") {
            setLiveUnlabeled(ev.unlabeled_remaining);
          }
          if (typeof ev.batches_run === "number") {
            setLiveBatch(ev.batches_run);
          }
          return;
        }
        if (ty === "db_busy") {
          const n = ev.consecutive_busy;
          setLivePhase(
            typeof n === "number"
              ? `waiting for database (retry ${n})`
              : "waiting for database",
          );
          if (typeof ev.unlabeled_remaining === "number") {
            setLiveUnlabeled(ev.unlabeled_remaining);
          }
          return;
        }
        if (ty === "error") {
          acc.streamHadError = true;
          const detail =
            typeof ev.detail === "string"
              ? ev.detail
              : JSON.stringify(ev.detail ?? ev);
          console.error("[MetaMagi catch-up] stream error", detail);
          setLabelMsg(`MetaMagi catch-up failed: ${detail.slice(0, 500)}`);
          return;
        }
        if (ty === "done") {
          acc.summary = ev as unknown as MetamagiCatchupResponse;
        }
      };

      const dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split("\n");
        buf = lines.pop() ?? "";
        for (const line of lines) {
          const t = line.trim();
          if (!t) continue;
          try {
            handleEvent(JSON.parse(t) as Record<string, unknown>);
          } catch {
            console.error("[MetaMagi catch-up] invalid NDJSON line", t);
          }
        }
      }
      const tail = buf.trim();
      if (tail) {
        try {
          handleEvent(JSON.parse(tail) as Record<string, unknown>);
        } catch {
          console.error("[MetaMagi catch-up] invalid trailing NDJSON", tail);
        }
      }

      if (acc.streamHadError) {
        loadStats();
        return;
      }
      if (acc.summary) {
        const d = acc.summary;
        console.info("[MetaMagi catch-up] OK", d.stopped_reason, d.batches_run);
        const remaining =
          typeof d.unlabeled_remaining_at_end === "number"
            ? d.unlabeled_remaining_at_end
            : null;
        setLabelMsg(
          `MetaMagi catch-up (${d.stopped_reason}): +${fmt(d.updated_forward_roc_30s)} / +${fmt(
            d.updated_forward_roc_5m,
          )} ROC fields (30s / 5m), ${fmt(d.batches_run)} batches, ${Math.round(d.elapsed_ms)} ms · scan ${
            d.lookback_minutes == null ? "full table" : `${Math.round(d.lookback_minutes / 60)} h window`
          } · ${
            d.max_seconds_cap != null && d.max_seconds_cap > 0
              ? `time cap ${d.max_seconds_cap}s`
              : "no time cap"
          }${remaining !== null ? ` · ~${fmt(remaining)} row(s) still missing ROC at end` : ""}`,
        );
        loadStats();
      } else {
        setLabelMsg(
          "MetaMagi catch-up finished without a summary event — check backend logs (metamagi_catchup).",
        );
        loadStats();
      }
    } catch (e: unknown) {
      console.error("[MetaMagi catch-up] request failed", e);
      const msg = e instanceof Error ? e.message : String(e);
      setLabelMsg(`MetaMagi catch-up failed: ${msg}. Check backend logs (logger metamagi_catchup).`);
    } finally {
      setLabeling(false);
      setLiveUnlabeled(null);
      setLiveBatch(0);
      setLivePhase(null);
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
              {stats?.metamagi != null && stats.metamagi.total_rows > 0 && (
                <p className="text-xs text-gray-500 font-mono">
                  voter_feedback: {fmt(stats.metamagi.total_rows)} rows ·{' '}
                  <span className={stats.metamagi.unlabeled_rows > 0 ? 'text-amber-400' : 'text-green-400'}>
                    {fmt(stats.metamagi.unlabeled_rows)} missing ROC label(s)
                  </span>
                </p>
              )}
              {labeling && (
                <>
                  <p className="text-xs text-gray-500 font-mono">
                    NDJSON stream · detailed logs:{' '}
                    <span className="text-gray-400">metamagi_catchup</span>
                  </p>
                  {livePhase === "connecting" && liveUnlabeled === null && (
                    <p className="text-xs text-gray-400 font-mono">Connecting…</p>
                  )}
                  {liveUnlabeled !== null && (
                    <p className="text-xs text-orange-200 font-mono font-semibold tabular-nums">
                      ~{fmt(liveUnlabeled)} row(s) still missing ROC · labeling batch pass{' '}
                      {fmt(liveBatch)}
                      {livePhase != null &&
                      livePhase !== "labeling" &&
                      livePhase !== "connecting"
                        ? ` · ${livePhase}`
                        : ""}
                    </p>
                  )}
                </>
              )}
              {labelMsg && (
                <p
                  className={`text-xs font-mono ${
                    labelMsg.includes('failed') ? 'text-red-400' : 'text-cyan-400'
                  }`}
                >
                  {labelMsg}
                </p>
              )}
              {purgeMsg && (
                <p className={`text-xs font-mono ${purgeMsg.startsWith('Purge failed') ? 'text-red-400' : 'text-green-400'}`}>
                  {purgeMsg}
                </p>
              )}
            </div>
            <div className="flex flex-col items-end gap-2 shrink-0">
              <button
                type="button"
                onClick={handleMetamagiCatchup}
                disabled={labeling}
                className="text-xs px-3 py-1.5 rounded-custom border border-orange-500/60 text-orange-300 hover:bg-orange-500/10 transition-colors disabled:opacity-50 font-semibold"
              >
                {labeling ? 'Labeling…' : 'MetaMagi label catch-up'}
              </button>
              <button
                type="button"
                onClick={handlePurge}
                disabled={purging}
                className="text-xs text-red-500 hover:text-red-400 transition-colors underline disabled:opacity-50"
              >
                {purging ? 'Purging…' : 'Purge Simulation Logs'}
              </button>
            </div>
          </div>
        </div>
      </div>
    </main>
  );
}
