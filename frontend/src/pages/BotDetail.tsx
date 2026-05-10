import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { Copy, ChevronDown, Info } from 'lucide-react';
import { BotTacticalChart } from '../components/BotTacticalChart';
import { API_BASE, CHART_OHLCV_POLL_INTERVAL_MS } from '../config';
import { useMagiWebSocket, type MagiWebSocketMessage } from '../hooks/useMagiWebSocket';
import { useRealtimeStore } from '../stores/realtimeStore';

/** Pixels from bottom to consider the user "at" the latest log line. */
const LOG_BOTTOM_THRESHOLD_PX = 72;

interface BotLogRow {
  log_id: number;
  bot_id: string;
  created_at: number;
  level: string;
  execution_mode: string;
  message: string;
}

const EMPTY_LOGS: BotLogRow[] = [];

const _pad = (n: number, len = 2) => String(n).padStart(len, '0');

function localDateTimeStr(d: Date, withMs = false): string {
  const base =
    `${d.getFullYear()}-${_pad(d.getMonth() + 1)}-${_pad(d.getDate())} ` +
    `${_pad(d.getHours())}:${_pad(d.getMinutes())}:${_pad(d.getSeconds())}`;
  return withMs ? `${base}.${_pad(d.getMilliseconds(), 3)}` : base;
}

function formatLogTime(ms: number) {
  try {
    return localDateTimeStr(new Date(ms));
  } catch {
    return String(ms);
  }
}

function formatLogTimeExec(ms: number) {
  try {
    return localDateTimeStr(new Date(ms), true);
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

function symbolHeadline(symbol: string | undefined) {
  if (!symbol) return '…';
  return symbol.replace('/', '_').toUpperCase();
}

function logLineClass(level: string) {
  switch (level) {
    case 'error':
      return 'text-red-400 phosphor-red';
    case 'warn':
      return 'text-magi-primary phosphor-amber';
    case 'debug':
      return 'opacity-50 text-magi-muted';
    case 'info':
    default:
      return 'opacity-80 text-magi-tertiary';
  }
}

function formatExecPrice(n: number) {
  return n.toLocaleString(undefined, { maximumFractionDigits: 8 });
}

function formatQuoteAmount(n: number, maxFrac = 6) {
  return n.toLocaleString(undefined, { maximumFractionDigits: maxFrac });
}

function pnlToneClass(n: number) {
  if (n > 1e-8) return 'text-magi-tertiary phosphor-green';
  if (n < -1e-8) return 'text-red-400';
  return 'text-magi-on-bg';
}

function formatLogLinePlain(log: BotLogRow) {
  return `[${formatLogTime(log.created_at)}] [${log.execution_mode}] [${log.level}] ${log.message}`;
}

// ── Voter display metadata ──────────────────────────────────────────────────

const VOTER_META: Record<string, { label: string; role: string }> = {
  macd_rsi:      { label: 'MACD + RSI',     role: 'Momentum' },
  stochastic:    { label: 'Stochastic',      role: 'Mean-Rev' },
  cci:           { label: 'CCI',             role: 'Mean-Rev' },
  bb_breakout:   { label: 'BB Breakout',     role: 'Breakout' },
  rsi_cross:     { label: 'RSI Cross',       role: 'Mean-Rev' },
  supertrend:    { label: 'Supertrend',      role: 'Trend' },
  dual_ema:      { label: 'Dual EMA',        role: 'Trend' },
  bb_rsi:        { label: 'BB + RSI',        role: 'Mean-Rev' },
  ema_ribbon:    { label: 'EMA Ribbon',      role: 'Trend' },
  parabolic_sar: { label: 'Parabolic SAR',   role: 'Trend' },
  donchian:      { label: 'Donchian',        role: 'Breakout' },
  tema:          { label: 'TEMA',            role: 'Trend' },
  sma_cross:     { label: 'SMA Cross',       role: 'Trend' },
  obv_price:     { label: 'OBV + Price',     role: 'Volume' },
  price_breakout:{ label: 'Price Breakout',  role: 'Breakout' },
  // Lag-specialized voters
  btc_lead_detector:    { label: 'BTC Lead',        role: 'Lag' },
  roc_divergence:       { label: 'ROC Divergence',  role: 'Lag' },
  lag_correlation:      { label: 'Lag Correlation', role: 'Lag' },
  ratio_mean_reversion: { label: 'Ratio MR',        role: 'Lag' },
};

const ROLE_COLORS: Record<string, string> = {
  'Trend':    'text-blue-300   border-blue-400/30   bg-blue-500/8',
  'Mean-Rev': 'text-purple-300 border-purple-400/30 bg-purple-500/8',
  'Momentum': 'text-magi-primary border-magi-primary/30 bg-magi-primary/8',
  'Breakout': 'text-green-300  border-green-400/30  bg-green-500/8',
  'Volume':   'text-cyan-300   border-cyan-400/30   bg-cyan-500/8',
  'Lag':      'text-yellow-300 border-yellow-400/30 bg-yellow-500/8',
};

interface EnsembleParams {
  voters: string[];
  voterWeights: Record<string, number>;
  consensusMode: string;
  consensusThreshold: number;
}

function parseEnsembleParams(raw: string | null): EnsembleParams | null {
  if (!raw) return null;
  try {
    const p = JSON.parse(raw) as Record<string, unknown>;
    const voters = Array.isArray(p.voters) ? (p.voters as string[]) : null;
    if (!voters || voters.length === 0) return null;
    return {
      voters,
      voterWeights: (p.voter_weights as Record<string, number>) ?? {},
      consensusMode: typeof p.consensus_mode === 'string' ? p.consensus_mode : 'directional_net',
      consensusThreshold: typeof p.consensus_threshold === 'number'
        ? p.consensus_threshold : 0.15,
    };
  } catch {
    return null;
  }
}

// Live signal returned by /api/bots/:id/voter-signals
interface LiveVoterSignal {
  voter_name: string;
  voter_signal: 'buy' | 'sell' | 'hold';
  confidence: number | null;
  consensus_score: number | null;
  timestamp: number;
}

const SIGNAL_STYLES: Record<string, string> = {
  buy:  'bg-emerald-500/20 border-emerald-400/50 text-emerald-300',
  sell: 'bg-red-500/20    border-red-400/50    text-red-300',
  hold: 'bg-magi-grid/10  border-magi-grid/30  text-magi-muted/60',
};

const SIGNAL_DOT: Record<string, string> = {
  buy:  'bg-emerald-400 animate-pulse',
  sell: 'bg-red-400 animate-pulse',
  hold: 'bg-magi-muted/30',
};

interface VoterCouncilProps {
  ensemble: EnsembleParams;
  liveSignals: LiveVoterSignal[];
  lastUpdated: number | null;
}

function VoterCouncil({ ensemble, liveSignals, lastUpdated }: VoterCouncilProps) {
  const { voters, voterWeights, consensusMode, consensusThreshold } = ensemble;
  const maxWeight = Math.max(...voters.map((v) => voterWeights[v] ?? 1.0), 1);
  const isDirectionalNet = consensusMode === 'directional_net';

  const signalMap = Object.fromEntries(liveSignals.map((s) => [s.voter_name, s]));
  const hasLiveData = liveSignals.length > 0;

  // Compute weighted buy/sell/hold totals from live signals
  const weightedTotals = { buy: 0, sell: 0, hold: 0 };
  voters.forEach((v) => {
    const sig = signalMap[v]?.voter_signal;
    if (sig) weightedTotals[sig] += (voterWeights[v] ?? 1.0);
  });
  const totalWeight = weightedTotals.buy + weightedTotals.sell + weightedTotals.hold;

  // directional_net: net = (buy_w - sell_w) / total_w
  const net = totalWeight > 0 ? (weightedTotals.buy - weightedTotals.sell) / totalWeight : 0;
  const consensusSignal: 'buy' | 'sell' | 'hold' = isDirectionalNet
    ? net > consensusThreshold ? 'buy' : net < -consensusThreshold ? 'sell' : 'hold'
    : weightedTotals.buy >= weightedTotals.sell && weightedTotals.buy / totalWeight >= consensusThreshold
      ? 'buy'
      : weightedTotals.sell / totalWeight >= consensusThreshold ? 'sell' : 'hold';

  // Threshold label is mode-specific
  const thresholdLabel = isDirectionalNet
    ? `net>${(consensusThreshold * 100).toFixed(0)}%`
    : `≥${(consensusThreshold * 100).toFixed(0)}%`;

  return (
    <div className="border-b border-magi-grid/15 px-4 py-4">
      {/* Section header */}
      <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
        <div className="flex items-center gap-2">
          <span className="font-label text-[9px] uppercase tracking-widest text-magi-muted/50">
            Voter Council
          </span>
          <span className="font-label text-[9px] font-bold text-magi-primary/70 border border-magi-primary/20 bg-magi-primary/8 px-1.5 py-0.5 rounded">
            {voters.length} voters
          </span>
        </div>
        <div className="flex items-center gap-2">
          <span className="font-label text-[9px] uppercase tracking-widest text-magi-muted/40">
            {consensusMode}
          </span>
          <span className="font-mono text-[9px] text-magi-muted/40">
            {thresholdLabel}
          </span>
        </div>
      </div>

      {/* Consensus visualisation */}
      {hasLiveData && (
        <div className="mb-3">
          {isDirectionalNet ? (
            // directional_net: centered bar showing (buy_w - sell_w) / total_w
            // Center = 0, left = sell pressure, right = buy pressure
            <div>
              <div className="relative h-2 w-full rounded-full bg-magi-grid/20 overflow-hidden">
                {/* center baseline */}
                <div className="absolute inset-y-0 left-1/2 w-px bg-magi-grid/60" />
                {/* threshold markers */}
                <div
                  className="absolute inset-y-0 w-px bg-magi-primary/30"
                  style={{ left: `${(0.5 + consensusThreshold / 2) * 100}%` }}
                />
                <div
                  className="absolute inset-y-0 w-px bg-magi-primary/30"
                  style={{ left: `${(0.5 - consensusThreshold / 2) * 100}%` }}
                />
                {/* net bar */}
                {net > 0 ? (
                  <div
                    className="absolute inset-y-0 bg-emerald-400/80 transition-all"
                    style={{ left: '50%', width: `${Math.min(Math.abs(net) / 2, 0.5) * 100}%` }}
                  />
                ) : net < 0 ? (
                  <div
                    className="absolute inset-y-0 bg-red-400/80 transition-all"
                    style={{ right: '50%', width: `${Math.min(Math.abs(net) / 2, 0.5) * 100}%` }}
                  />
                ) : null}
              </div>
              <div className="mt-1 flex items-center justify-between font-label text-[8px] text-magi-muted/50">
                <span className="text-red-400/80">SELL</span>
                <span className={`font-black ${
                  consensusSignal === 'buy' ? 'text-emerald-400' :
                  consensusSignal === 'sell' ? 'text-red-400' : 'text-magi-muted/60'
                }`}>
                  net {net >= 0 ? '+' : ''}{(net * 100).toFixed(1)}% → {consensusSignal.toUpperCase()}
                </span>
                <span className="text-emerald-400/80">BUY</span>
              </div>
            </div>
          ) : (
            // Classic modes: show raw vote share bar
            <div>
              <div className="flex h-1.5 w-full overflow-hidden rounded-full">
                {weightedTotals.buy > 0 && (
                  <div className="bg-emerald-400 transition-all"
                    style={{ width: `${(weightedTotals.buy / totalWeight) * 100}%` }} />
                )}
                {weightedTotals.hold > 0 && (
                  <div className="bg-magi-muted/20 transition-all"
                    style={{ width: `${(weightedTotals.hold / totalWeight) * 100}%` }} />
                )}
                {weightedTotals.sell > 0 && (
                  <div className="bg-red-400 transition-all"
                    style={{ width: `${(weightedTotals.sell / totalWeight) * 100}%` }} />
                )}
              </div>
              <div className="mt-1 flex justify-between font-label text-[8px] text-magi-muted/50">
                <span className="text-emerald-400/80">
                  B {totalWeight > 0 ? ((weightedTotals.buy / totalWeight) * 100).toFixed(0) : 0}%
                </span>
                <span className={`font-black ${
                  consensusSignal === 'buy' ? 'text-emerald-400' :
                  consensusSignal === 'sell' ? 'text-red-400' : 'text-magi-muted/60'
                }`}>
                  {consensusSignal.toUpperCase()}
                </span>
                <span className="text-red-400/80">
                  S {totalWeight > 0 ? ((weightedTotals.sell / totalWeight) * 100).toFixed(0) : 0}%
                </span>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Voter grid */}
      <div className="grid grid-cols-2 gap-1.5">
        {voters.map((voterId) => {
          const meta = VOTER_META[voterId] ?? { label: voterId, role: 'Other' };
          const roleColor = ROLE_COLORS[meta.role] ?? 'text-magi-muted border-magi-grid/30 bg-magi-grid/5';
          const weight = voterWeights[voterId] ?? 1.0;
          const weightPct = maxWeight > 0 ? (weight / maxWeight) * 100 : 100;
          const live = signalMap[voterId];
          const sig = live?.voter_signal ?? null;
          const signalStyle = sig ? SIGNAL_STYLES[sig] : null;

          return (
            <div
              key={voterId}
              className={`relative flex flex-col gap-1.5 rounded border px-2.5 py-2 transition-colors ${
                signalStyle ?? roleColor
              }`}
            >
              {sig && (
                <span className={`absolute top-1.5 right-1.5 h-1.5 w-1.5 rounded-full ${SIGNAL_DOT[sig]}`} />
              )}
              <div className="flex items-start justify-between gap-1 min-w-0 pr-3">
                <p className="font-label text-[10px] font-black truncate leading-tight">
                  {meta.label}
                </p>
              </div>
              {sig ? (
                <span className={`self-start font-label text-[9px] font-black uppercase tracking-wider px-1.5 py-0.5 rounded border ${signalStyle}`}>
                  {sig}
                  {live.confidence != null && (
                    <span className="ml-1 opacity-70 font-normal normal-case">
                      {(live.confidence * 100).toFixed(0)}%
                    </span>
                  )}
                </span>
              ) : (
                <span className="font-label text-[8px] font-bold uppercase tracking-wide opacity-50">
                  {meta.role}
                </span>
              )}
              <div className="flex items-center gap-1.5">
                <div className="flex-1 h-0.5 rounded-full bg-current opacity-20 overflow-hidden">
                  <div
                    className="h-full rounded-full bg-current opacity-80 transition-all"
                    style={{ width: `${weightPct}%` }}
                  />
                </div>
                <span className="font-mono text-[8px] opacity-50 shrink-0">
                  {weight.toFixed(1)}×
                </span>
              </div>
            </div>
          );
        })}
      </div>

      {lastUpdated != null && (
        <p className="mt-2 font-label text-[8px] text-magi-muted/30 text-right">
          updated {new Date(lastUpdated).toLocaleTimeString()}
        </p>
      )}
    </div>
  );
}

interface PortfolioDistributionProps {
  baseSym: string;
  quoteSym: string;
  baseValueQuote: number | null;
  quoteRemaining: number | null;
  baseAllocPct: number | null;
  quoteAllocPct: number | null;
  openBasePosition: number;
  markPrice: number | null;
}

function PortfolioDistribution({
  baseSym,
  quoteSym,
  baseValueQuote,
  quoteRemaining,
  baseAllocPct,
  quoteAllocPct,
  openBasePosition,
  markPrice,
}: PortfolioDistributionProps) {
  const basePct = baseAllocPct ?? 0;
  const quotePct = quoteAllocPct ?? 100;
  const hasPosition = openBasePosition > 1e-12;

  return (
    <div className="border-b border-magi-grid/15 bg-magi-container-low/60 px-4 py-3 sm:px-6">
      <div className="flex items-center justify-between mb-2">
        <p className="font-label text-[9px] uppercase tracking-widest text-magi-muted/50">
          Portfolio Allocation
        </p>
        {markPrice != null && hasPosition && (
          <p className="font-label text-[9px] text-magi-muted/40">
            mark {markPrice.toLocaleString(undefined, { maximumFractionDigits: 2 })}
          </p>
        )}
      </div>

      {/* Split bar */}
      <div className="flex h-2.5 w-full overflow-hidden rounded-sm mb-3 bg-magi-grid/20">
        {hasPosition && basePct > 0 && (
          <div
            className="h-full bg-magi-tertiary/70 transition-all duration-500"
            style={{ width: `${basePct}%` }}
          />
        )}
        <div
          className="h-full bg-blue-500/40 transition-all duration-500"
          style={{ width: `${quotePct}%` }}
        />
      </div>

      {/* Legend row */}
      <div className="flex items-start justify-between gap-4">
        {/* Base asset */}
        <div className="flex flex-col gap-0.5 min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-sm bg-magi-tertiary/70 shrink-0" />
            <span className="font-label text-[11px] font-bold text-magi-tertiary">
              {baseSym}
            </span>
            <span className="font-label text-[10px] text-magi-tertiary/80">
              {basePct.toFixed(1)}%
            </span>
          </div>
          {hasPosition ? (
            <>
              <p className="font-mono text-[11px] text-magi-on-bg pl-3.5">
                {openBasePosition.toLocaleString(undefined, { maximumFractionDigits: 8, minimumFractionDigits: 5 })} {baseSym}
              </p>
              {baseValueQuote != null && (
                <p className="font-mono text-[10px] text-magi-muted/50 pl-3.5">
                  ≈ {baseValueQuote.toLocaleString(undefined, { maximumFractionDigits: 2 })} {quoteSym}
                </p>
              )}
            </>
          ) : (
            <p className="font-mono text-[11px] text-magi-muted/40 pl-3.5">0.00 {baseSym}</p>
          )}
        </div>

        {/* Divider */}
        <div className="h-10 w-px bg-magi-grid/20 shrink-0 self-center" />

        {/* Quote asset */}
        <div className="flex flex-col gap-0.5 min-w-0 text-right">
          <div className="flex items-center gap-1.5 justify-end">
            <span className="font-label text-[10px] text-blue-300/80">
              {quotePct.toFixed(1)}%
            </span>
            <span className="font-label text-[11px] font-bold text-blue-300">
              {quoteSym}
            </span>
            <span className="h-2 w-2 rounded-sm bg-blue-500/40 shrink-0" />
          </div>
          <p className="font-mono text-[11px] text-magi-on-bg">
            {quoteRemaining != null
              ? quoteRemaining.toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 })
              : '—'} {quoteSym}
          </p>
          <p className="font-mono text-[10px] text-magi-muted/40">available</p>
        </div>
      </div>
    </div>
  );
}

export default function BotDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const detail = useRealtimeStore((state) => (id ? state.botDetailsById[id] : undefined));
  const loadBotDetail = useRealtimeStore((state) => state.loadBotDetail);
  const loadTradeSummary = useRealtimeStore((state) => state.loadTradeSummary);
  const loadVoterSignals = useRealtimeStore((state) => state.loadVoterSignals);
  const handleBotDetailMessage = useRealtimeStore((state) => state.handleBotDetailMessage);
  const setChannelStatus = useRealtimeStore((state) => state.setChannelStatus);
  const bot = detail?.bot ?? null;
  const logs = detail?.logs ?? EMPTY_LOGS;
  const orderStats = detail?.orderStats ?? null;
  const orders = detail?.orders ?? [];
  const strategyHealth = detail?.strategyHealth ?? null;
  const executionMode = detail?.executionMode ?? null;
  const [actionError, setActionError] = useState<string | null>(null);
  const error = actionError ?? detail?.error ?? null;
  const [busy, setBusy] = useState(false);
  const [budgetDraft, setBudgetDraft] = useState('');
  const [budgetBusy, setBudgetBusy] = useState(false);
  const [forkApplyBudget, setForkApplyBudget] = useState(false);
  const [forkBusy, setForkBusy] = useState(false);
  const [forkNameDraft, setForkNameDraft] = useState('');
  const [showPromoteModal, setShowPromoteModal] = useState(false);
  const [promoteBusy, setPromoteBusy] = useState(false);
  const [followLogBottom, setFollowLogBottom] = useState(true);
  const [logsCopied, setLogsCopied] = useState(false);
  const logScrollRef = useRef<HTMLDivElement>(null);
  const logScrollRafRef = useRef<number | null>(null);

  // Trade summary (FIFO per-trade PnL)
  const [historyView, setHistoryView] = useState<'fills' | 'summary'>('fills');
  const tradeSummary = detail?.tradeSummary ?? null;
  const tradeSummaryLoading = detail?.tradeSummaryLoading ?? false;
  const liveVoterSignals = detail?.liveVoterSignals ?? [];
  const voterSignalsUpdatedAt = detail?.voterSignalsUpdatedAt ?? null;

  const refresh = useCallback(async () => {
    if (!id) return;
    setActionError(null);
    await loadBotDetail(id);
  }, [id, loadBotDetail]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const fetchTradeSummary = useCallback(async () => {
    if (!id) return;
    await loadTradeSummary(id);
  }, [id, loadTradeSummary]);

  const detailWs = useMagiWebSocket({
    path: `/ws/bot/${id}`,
    enabled: Boolean(id),
    onMessage: (message: MagiWebSocketMessage<Record<string, unknown>>) => {
      if (id) handleBotDetailMessage(id, message);
    },
  });

  useEffect(() => {
    if (!id) return;
    setChannelStatus(`/ws/bot/${id}`, detailWs.status);
  }, [detailWs.status, id, setChannelStatus]);

  useEffect(() => {
    if (historyView === 'summary') {
      void fetchTradeSummary();
    }
  }, [historyView, fetchTradeSummary]);

  // WebSocket fallback only: refresh trade summary alongside orders at low cadence.
  useEffect(() => {
    if (!detailWs.isFallbackPolling || !id || bot?.status !== 'running' || historyView !== 'summary') return;
    const t = window.setInterval(() => void fetchTradeSummary(), 30_000);
    return () => window.clearInterval(t);
  }, [detailWs.isFallbackPolling, id, bot?.status, historyView, fetchTradeSummary]);

  useEffect(() => {
    const b = strategyHealth?.initial_budget_quote;
    if (b != null && b > 0) setBudgetDraft(String(b));
    else setBudgetDraft('');
  }, [strategyHealth?.initial_budget_quote, id]);

  useEffect(() => {
    setFollowLogBottom(true);
  }, [id]);

  useEffect(() => {
    if (!detailWs.isFallbackPolling || !id || bot?.status !== 'running') return;
    const t = window.setInterval(refresh, 30_000);
    return () => window.clearInterval(t);
  }, [detailWs.isFallbackPolling, id, bot?.status, refresh]);

  const logsChronological = useMemo(() => [...logs].reverse(), [logs]);

  const logsPlainText = useMemo(
    () => logsChronological.map((log) => formatLogLinePlain(log)).join('\n'),
    [logsChronological],
  );

  const scrollLogsToBottom = useCallback((behavior: ScrollBehavior = 'auto') => {
    const el = logScrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior });
  }, []);

  const onLogScroll = useCallback(() => {
    const el = logScrollRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setFollowLogBottom(distanceFromBottom <= LOG_BOTTOM_THRESHOLD_PX);
  }, []);

  useEffect(() => {
    if (!followLogBottom) return;
    if (logScrollRafRef.current != null) cancelAnimationFrame(logScrollRafRef.current);
    logScrollRafRef.current = requestAnimationFrame(() => {
      logScrollRafRef.current = null;
      scrollLogsToBottom();
    });
    return () => {
      if (logScrollRafRef.current != null) cancelAnimationFrame(logScrollRafRef.current);
    };
  }, [logs, followLogBottom, scrollLogsToBottom]);

  const jumpToLatestLogs = useCallback(() => {
    setFollowLogBottom(true);
    requestAnimationFrame(() => scrollLogsToBottom('smooth'));
  }, [scrollLogsToBottom]);

  const copyLogs = useCallback(async () => {
    if (!logsPlainText) return;
    try {
      await navigator.clipboard.writeText(logsPlainText);
    } catch {
      try {
        const ta = document.createElement('textarea');
        ta.value = logsPlainText;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      } catch {
        return;
      }
    }
    setLogsCopied(true);
    window.setTimeout(() => setLogsCopied(false), 2000);
  }, [logsPlainText]);

  const chartConfig = useMemo(() => {
    let p: Record<string, unknown> = {};
    const raw = bot?.strategy_params_json;
    if (raw) {
      try {
        p = JSON.parse(raw) as Record<string, unknown>;
      } catch {
        p = {};
      }
    }
    const lim = Number(p.ohlcv_limit);
    return {
      timeframe: String(p.ohlcv_timeframe ?? '5m'),
      limit: Math.min(500, Math.max(10, Number.isFinite(lim) ? lim : 100)),
      fastPeriod: Math.max(2, Number(p.fast_period) || 5),
      slowPeriod: Math.max(3, Number(p.slow_period) || 15),
    };
  }, [bot?.strategy_params_json]);

  const isEnsemble =
    (bot?.strategy?.startsWith('magi_ensemble') ||
     bot?.strategy?.startsWith('magi_lag_ensemble')) ?? false;
  const ensembleParams = useMemo(
    () => (isEnsemble ? parseEnsembleParams(bot?.strategy_params_json ?? null) : null),
    [isEnsemble, bot?.strategy_params_json],
  );

  useEffect(() => {
    if (!id || !isEnsemble) return;
    void loadVoterSignals(id);
    if (!detailWs.isFallbackPolling) return;
    const timer = setInterval(() => void loadVoterSignals(id), 30_000);
    return () => clearInterval(timer);
  }, [id, isEnsemble, detailWs.isFallbackPolling, loadVoterSignals]);

  const forkNewBotInstance = async () => {
    if (!id) return;
    const msg =
      'Create a new bot instance from this one? This bot\u2019s orders and logs stay here forever; the new bot gets a new id and starts with empty history. The exchange is unchanged.';
    if (!window.confirm(msg)) return;
    setForkBusy(true);
    setActionError(null);
    try {
      const payload: Record<string, unknown> = {};
      if (forkNameDraft.trim()) payload.name = forkNameDraft.trim();
      if (forkApplyBudget) {
        const trimmed = budgetDraft.trim();
        if (trimmed === '') payload.initial_budget_quote = null;
        else {
          const n = Number.parseFloat(trimmed);
          if (!Number.isFinite(n) || n < 0) {
            setActionError('Budget must be empty (clear) or a non-negative number when applying on fork.');
            setForkBusy(false);
            return;
          }
          payload.initial_budget_quote = n === 0 ? null : n;
        }
      }
      const res = await fetch(`${API_BASE}/api/bots/${id}/fork`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok)
        throw new Error(typeof data.detail === 'string' ? data.detail : 'Fork failed');
      const newId = data.new_bot_id as string | undefined;
      if (!newId) throw new Error('No new_bot_id in response');
      navigate(`/bots/${newId}`);
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Fork failed');
    } finally {
      setForkBusy(false);
    }
  };

  const saveInitialBudget = async () => {
    if (!id) return;
    const trimmed = budgetDraft.trim();
    const body =
      trimmed === ''
        ? { initial_budget_quote: null }
        : { initial_budget_quote: Number.parseFloat(trimmed) };
    if (trimmed !== '' && !Number.isFinite(body.initial_budget_quote as number)) {
      setActionError('Initial budget must be a number');
      return;
    }
    if (
      trimmed !== '' &&
      typeof body.initial_budget_quote === 'number' &&
      body.initial_budget_quote < 0
    ) {
      setActionError('Initial budget cannot be negative');
      return;
    }
    setBudgetBusy(true);
    setActionError(null);
    try {
      const res = await fetch(`${API_BASE}/api/bots/${id}/strategy-params`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok)
        throw new Error(typeof data.detail === 'string' ? data.detail : 'Failed to save budget');
      await refresh();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Save failed');
    } finally {
      setBudgetBusy(false);
    }
  };

  const promoteBot = async (targetMode: 'testnet' | 'live') => {
    if (!id) return;
    setPromoteBusy(true);
    setActionError(null);
    try {
      const res = await fetch(`${API_BASE}/api/bots/${id}/execution-mode`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ execution_mode: targetMode }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Update failed');
      setShowPromoteModal(false);
      await refresh();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Update failed');
    } finally {
      setPromoteBusy(false);
    }
  };

  const confirmRiskOverride = () =>
    window.confirm(
      'Resume bot trading?\n\nThis manually overrides active risk protections and resets the daily loss, drawdown, and consecutive-loss baselines from the current account state. Continue?',
    );

  const setStatus = async (
    status: 'running' | 'stopped' | 'paused',
    options: { resetRiskProtections?: boolean } = {},
  ) => {
    if (!id) return;
    setBusy(true);
    setActionError(null);
    try {
      const res = await fetch(`${API_BASE}/api/bots/${id}/status`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          status,
          reset_risk_protections: options.resetRiskProtections ?? false,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Update failed');
      await refresh();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Update failed');
    } finally {
      setBusy(false);
    }
  };

  if (!id) return null;

  // Bot's own execution_mode drives the badge (not the global setting)
  const botExecMode = bot?.execution_mode ?? executionMode ?? 'testnet';
  const liveLabel =
    botExecMode === 'live' ? 'LIVE TRADING' : botExecMode === 'testnet' ? 'TESTNET' : 'OFFLINE';
  const strategyTag = (() => {
    const s = bot?.strategy ?? '';
    if (s.startsWith('magi_lag_ensemble_')) {
      const freq = s.replace('magi_lag_ensemble_', '').toUpperCase();
      return `MAGI LAG · ${freq}`;
    }
    if (s.startsWith('magi_ensemble_')) {
      const freq = s.replace('magi_ensemble_', '').toUpperCase();
      return `MAGI · ${freq}`;
    }
    return s.toUpperCase().replace(/-/g, '_') || '—';
  })();
  const qc = strategyHealth?.quote_currency ?? 'USDT';
  const winRateLabel =
    strategyHealth?.win_rate_pct != null ? `${strategyHealth.win_rate_pct}%` : '—';
  const drawdownLabel =
    strategyHealth != null
      ? strategyHealth.max_drawdown_pct != null
        ? `${formatQuoteAmount(strategyHealth.max_drawdown_pct, 4)}%`
        : `${formatQuoteAmount(strategyHealth.max_drawdown_quote)} ${qc}`
      : '—';
  const netPnl = strategyHealth?.total_pnl_quote ?? null;
  const recordedOrderCount = orderStats?.total_orders ?? 0;

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-magi-bg text-magi-on-bg">
      <main className="grid min-h-0 flex-1 grid-cols-1 overflow-hidden lg:grid-cols-12">

        {/* ── LEFT SIDEBAR: Strategy / Voter config ─────────── */}
        <div className="col-span-1 flex min-h-0 flex-col overflow-y-auto border-b border-magi-grid/15 bg-magi-container-low/30 lg:col-span-3 lg:border-b-0 lg:border-r">

          {/* Bot identity strip */}
          <div className="flex flex-col gap-1 border-b border-magi-grid/20 px-4 py-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h1 className="font-headline text-xl font-black uppercase italic leading-none tracking-tighter text-magi-primary phosphor-amber">
                {symbolHeadline(bot?.symbol)}
              </h1>
              <p className="font-headline text-base font-bold tracking-tight text-magi-on-bg">
                {(bot?.status ?? '—').toUpperCase()}
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <span
                className={`font-label inline-flex items-center border px-2 py-0.5 text-[10px] font-bold uppercase tracking-widest ${
                  botExecMode === 'live'
                    ? 'border-red-400/40 bg-red-500/10 text-red-400'
                    : 'border-blue-400/30 bg-blue-500/10 text-blue-300'
                }`}
              >
                <span
                  className={`mr-1.5 h-1.5 w-1.5 rounded-full ${
                    bot?.status === 'running'
                      ? botExecMode === 'live' ? 'animate-pulse bg-red-400' : 'animate-pulse bg-magi-tertiary'
                      : 'bg-magi-muted/50'
                  }`}
                />
                {liveLabel}
              </span>
              <span className="font-label text-[10px] uppercase tracking-widest text-magi-muted/50">
                {strategyTag}
              </span>
            </div>
            {bot?.name && (
              <p className="font-label text-[10px] text-magi-muted/40 truncate">{bot.name}</p>
            )}
          </div>

          {error && (
            <div className="mx-3 mt-3 border border-red-500/50 bg-red-950/20 p-3 text-xs text-red-300">
              {error}
            </div>
          )}

          {/* Voter Council (ensemble) or strategy params label (non-ensemble) */}
          {ensembleParams ? (
            <VoterCouncil
              ensemble={ensembleParams}
              liveSignals={liveVoterSignals}
              lastUpdated={voterSignalsUpdatedAt}
            />
          ) : (
            <div className="border-b border-magi-grid/15 px-4 py-3">
              <p className="font-label text-[9px] uppercase tracking-widest text-magi-muted/40 mb-1">
                Strategy
              </p>
              <p className="font-label text-[11px] font-bold text-magi-on-bg/80">
                {bot?.strategy?.toUpperCase().replace(/_/g, ' ') ?? '—'}
              </p>
            </div>
          )}

          {/* Capital & Budget */}
          <details className="border-b border-magi-grid/20 open:border-magi-primary/20" open>
            <summary className="cursor-pointer px-4 py-3 font-label text-[11px] font-bold uppercase tracking-widest text-magi-primary/80 hover:text-magi-primary">
              Capital &amp; New Instance
            </summary>
            <div className="border-t border-magi-grid/20 px-4 py-4 flex flex-col gap-4">
              <div className="flex flex-col gap-3 font-label text-[11px] text-magi-muted/85">
                <label className="flex flex-col gap-1.5 uppercase tracking-wider">
                  <span className="text-magi-muted/50">Initial budget ({qc})</span>
                  <input
                    type="text"
                    inputMode="decimal"
                    placeholder="e.g. 1000"
                    value={budgetDraft}
                    onChange={(e) => setBudgetDraft(e.target.value)}
                    className="rounded border border-magi-grid/30 bg-magi-bg px-3 py-2 font-mono text-base text-magi-on-bg focus:border-magi-primary/50 focus:outline-none"
                  />
                </label>
                <button
                  type="button"
                  disabled={budgetBusy}
                  onClick={() => void saveInitialBudget()}
                  className="rounded border border-magi-primary/40 bg-magi-primary/15 px-4 py-2 text-[11px] font-bold uppercase tracking-widest text-magi-primary hover:bg-magi-primary/25 disabled:opacity-40"
                >
                  {budgetBusy ? '…' : 'Save budget'}
                </button>
              </div>
              <div className="flex flex-col gap-3 font-label text-[11px] text-magi-muted/85">
                <label className="flex flex-col gap-1.5 uppercase tracking-wider">
                  <span className="text-magi-muted/50">New instance name</span>
                  <input
                    type="text"
                    placeholder={`${bot?.name ?? 'Bot'} (copy)`}
                    value={forkNameDraft}
                    onChange={(e) => setForkNameDraft(e.target.value)}
                    className="rounded border border-magi-grid/30 bg-magi-bg px-3 py-2 font-mono text-base text-magi-on-bg focus:border-magi-primary/50 focus:outline-none"
                  />
                </label>
                <label className="flex cursor-pointer items-start gap-2 text-[10px] uppercase leading-snug tracking-wide text-magi-muted/70">
                  <input
                    type="checkbox"
                    checked={forkApplyBudget}
                    onChange={(e) => setForkApplyBudget(e.target.checked)}
                    className="mt-0.5 border-magi-grid/40"
                  />
                  <span>Apply budget on fork</span>
                </label>
                <button
                  type="button"
                  disabled={forkBusy}
                  onClick={() => void forkNewBotInstance()}
                  className="rounded border border-magi-tertiary/50 bg-magi-tertiary/15 px-3 py-2 text-[11px] font-bold uppercase tracking-widest text-magi-tertiary hover:bg-magi-tertiary/25 disabled:opacity-40"
                >
                  {forkBusy ? '…' : 'New instance →'}
                </button>
              </div>
            </div>
          </details>

          {/* Strategy Params raw */}
          <details className="border-b border-magi-grid/20 open:border-magi-primary/20">
            <summary className="cursor-pointer px-4 py-3 font-label text-[11px] font-bold uppercase tracking-widest text-magi-primary/60 hover:text-magi-primary">
              Strategy Params (raw)
            </summary>
            <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-all border-t border-magi-grid/20 p-4 font-mono text-[10px] text-magi-on-bg/80">
              {formatParamsJson(bot?.strategy_params_json ?? null)}
            </pre>
          </details>
        </div>

        {/* ── CENTER: Chart + Stats + Execution history ─────── */}
        <div className="col-span-1 flex min-h-0 min-w-0 flex-col overflow-y-auto overflow-x-hidden border-r border-magi-grid/15 lg:col-span-6">

          {/* Chart — flush, full width of center column */}
          {bot?.symbol ? (
            <BotTacticalChart
              symbol={bot.symbol}
              timeframe={chartConfig.timeframe}
              limit={chartConfig.limit}
              fastPeriod={chartConfig.fastPeriod}
              slowPeriod={chartConfig.slowPeriod}
              liveOhlcvPollMs={detailWs.isFallbackPolling ? CHART_OHLCV_POLL_INTERVAL_MS : 0}
            />
          ) : null}

          {/* Metrics strip — shows Current Capital when budget is set */}
          {strategyHealth?.initial_budget_quote != null && (
            <div className="border-b border-magi-grid/15 bg-magi-primary/5 px-4 py-3 flex flex-wrap items-center gap-x-6 gap-y-1">
              <div className="flex items-baseline gap-2">
                <span className="font-label text-[9px] uppercase tracking-widest text-magi-muted/60">
                  Budget
                </span>
                <span className="font-headline text-sm font-bold text-magi-muted/80">
                  {formatQuoteAmount(strategyHealth.initial_budget_quote, 2)} {qc}
                </span>
              </div>
              <span className="text-magi-grid/40 hidden sm:block">→</span>
              <div className="flex items-baseline gap-2">
                <span className="font-label text-[9px] uppercase tracking-widest text-magi-muted/60">
                  Current Capital
                </span>
                <span
                  className={`font-headline text-lg font-black ${
                    strategyHealth.current_capital_quote != null
                      ? pnlToneClass(strategyHealth.current_capital_quote - strategyHealth.initial_budget_quote)
                      : 'text-magi-on-bg'
                  }`}
                >
                  {strategyHealth.current_capital_quote != null
                    ? `${formatQuoteAmount(strategyHealth.current_capital_quote, 2)} ${qc}`
                    : '—'}
                </span>
              </div>
              {strategyHealth.pnl_return_on_budget_pct != null && (
                <span
                  className={`font-label text-[11px] font-bold px-2 py-0.5 rounded ${
                    strategyHealth.pnl_return_on_budget_pct >= 0
                      ? 'bg-green-500/15 text-green-400'
                      : 'bg-red-500/15 text-red-400'
                  }`}
                >
                  {strategyHealth.pnl_return_on_budget_pct >= 0 ? '+' : ''}
                  {formatQuoteAmount(strategyHealth.pnl_return_on_budget_pct, 2)}% ROI
                </span>
              )}
            </div>
          )}

          {/* Portfolio distribution bar */}
          {strategyHealth != null && (strategyHealth.base_alloc_pct != null || strategyHealth.open_base_position > 1e-12) && (
            <PortfolioDistribution
              baseSym={bot?.symbol?.split('/')[0] ?? 'BASE'}
              quoteSym={qc}
              baseValueQuote={strategyHealth.base_value_quote}
              quoteRemaining={strategyHealth.quote_remaining}
              baseAllocPct={strategyHealth.base_alloc_pct}
              quoteAllocPct={strategyHealth.quote_alloc_pct}
              openBasePosition={strategyHealth.open_base_position}
              markPrice={strategyHealth.mark_price}
            />
          )}

          {/* Stats grid */}
          <div className="grid grid-cols-2 gap-px border-b border-magi-grid/15 bg-magi-grid/10 sm:grid-cols-4">
            <div className="flex flex-col gap-1 bg-magi-container-low px-4 py-3">
              <p className="font-label text-[9px] uppercase tracking-widest text-magi-muted/50">Fills</p>
              <p className="font-headline text-2xl font-black text-magi-primary phosphor-amber">
                {recordedOrderCount}
              </p>
              <p className="font-label text-[9px] uppercase tracking-wide text-magi-muted/55">
                B {orderStats?.buy_count ?? 0} · S {orderStats?.sell_count ?? 0}
                {orderStats?.last_order_at_ms != null && (
                  <span className="mt-0.5 block normal-case text-magi-muted/40">
                    {formatLogTime(orderStats.last_order_at_ms)}
                  </span>
                )}
              </p>
            </div>

            <div className="flex flex-col gap-1 bg-magi-container-low px-4 py-3">
              <p className="font-label text-[9px] uppercase tracking-widest text-magi-muted/50">Win Rate</p>
              <p className="font-headline text-2xl font-black text-magi-on-bg">{winRateLabel}</p>
              <p className="font-label text-[9px] uppercase tracking-wide text-magi-muted/55">
                {strategyHealth != null
                  ? `${strategyHealth.winning_trades}W · ${strategyHealth.losing_trades}L / ${strategyHealth.closed_trades} exits`
                  : '—'}
              </p>
            </div>

            <div className="flex flex-col gap-1 bg-magi-container-low px-4 py-3">
              <p className="font-label text-[9px] uppercase tracking-widest text-magi-muted/50">Net PnL</p>
              <p
                className={`font-headline text-2xl font-black ${
                  netPnl != null ? pnlToneClass(netPnl) : 'text-magi-on-bg'
                }`}
              >
                {netPnl != null ? `${formatQuoteAmount(netPnl)} ${qc}` : '—'}
              </p>
              <p className="font-label text-[9px] tracking-wide text-magi-muted/55">
                {strategyHealth?.pnl_return_on_budget_pct != null
                  ? `ROI ${formatQuoteAmount(strategyHealth.pnl_return_on_budget_pct, 2)}% vs budget`
                  : strategyHealth != null
                    ? `R ${formatQuoteAmount(strategyHealth.realized_pnl_quote)} · U ${
                        strategyHealth.unrealized_pnl_quote != null
                          ? formatQuoteAmount(strategyHealth.unrealized_pnl_quote)
                          : '—'
                      }`
                    : '—'}
              </p>
            </div>

            <div className="flex flex-col gap-1 bg-magi-container-low px-4 py-3">
              <p className="font-label text-[9px] uppercase tracking-widest text-magi-muted/50">Max Drawdown</p>
              <p className="font-headline text-2xl font-black text-red-400">{drawdownLabel}</p>
              <p className="font-label text-[9px] tracking-wide text-magi-muted/55">
                {strategyHealth?.max_drawdown_vs_budget_pct != null
                  ? `${formatQuoteAmount(strategyHealth.max_drawdown_vs_budget_pct, 2)}% of budget`
                  : 'vs peak realized PnL'}
              </p>
            </div>
          </div>

          {/* Execution history table */}
          <div className="min-w-0 px-4 pt-4 pb-2 sm:px-6 sm:pt-5">
            {/* Header row: title + view toggle */}
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <h3 className="font-label text-[11px] font-bold uppercase tracking-widest text-magi-muted">
                Execution History
              </h3>
              <div className="flex items-center gap-2">
                <span className="font-label text-[9px] tracking-tight text-magi-muted/40">
                  {orderStats?.total_orders ?? 0} fills
                </span>
                {/* View toggle */}
                <div className="flex items-center rounded border border-magi-grid/30 overflow-hidden">
                  <button
                    type="button"
                    onClick={() => setHistoryView('fills')}
                    className={`px-2.5 py-1 font-label text-[9px] font-bold uppercase tracking-wider transition-colors ${
                      historyView === 'fills'
                        ? 'bg-magi-primary/20 text-magi-primary'
                        : 'text-magi-muted/50 hover:text-magi-muted/80'
                    }`}
                  >
                    Fills
                  </button>
                  <button
                    type="button"
                    onClick={() => setHistoryView('summary')}
                    className={`px-2.5 py-1 font-label text-[9px] font-bold uppercase tracking-wider border-l border-magi-grid/30 transition-colors ${
                      historyView === 'summary'
                        ? 'bg-magi-primary/20 text-magi-primary'
                        : 'text-magi-muted/50 hover:text-magi-muted/80'
                    }`}
                  >
                    Trade PnL
                  </button>
                </div>
              </div>
            </div>

            {historyView === 'fills' ? (
              /* ── RAW FILLS TABLE (unchanged) ── */
              <div className="max-h-[280px] overflow-auto">
                <table className="w-full min-w-[36rem] text-left font-label text-[10px] sm:text-[11px]">
                  <thead className="sticky top-0 border-b border-magi-grid/10 bg-magi-bg uppercase text-magi-muted/40">
                    <tr>
                      <th className="py-2 pr-3 font-normal">Timestamp</th>
                      <th className="py-2 pr-3 font-normal">Side</th>
                      <th className="py-2 pr-3 text-right font-normal">Spent / Sold</th>
                      <th className="py-2 pr-3 text-right font-normal">Received</th>
                      <th className="py-2 pr-3 text-right font-normal">Avg Price</th>
                      <th className="py-2 text-right font-normal">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-magi-grid/5">
                    {orders.length === 0 && (
                      <tr>
                        <td colSpan={6} className="py-4 italic text-magi-muted/60">
                          No fills yet — appears here after the first accepted buy/sell.
                        </td>
                      </tr>
                    )}
                    {orders.map((o) => {
                      const isBuy = o.side === 'buy';
                      const avgPx =
                        o.display_price != null ? o.display_price
                        : o.average != null ? o.average
                        : (o.cost != null && o.filled != null && o.filled > 0)
                          ? o.cost / o.filled
                          : null;

                      const baseSym = o.symbol?.split('/')[0] ?? 'BASE';
                      const quoteSym = o.symbol?.split('/')[1] ?? 'QUOTE';

                      const spentLabel = isBuy
                        ? o.cost != null ? `${formatQuoteAmount(o.cost, 4)} ${quoteSym}` : '—'
                        : o.filled != null ? `${formatQuoteAmount(o.filled, 8)} ${baseSym}` : '—';

                      const receivedLabel = isBuy
                        ? o.filled != null ? `${formatQuoteAmount(o.filled, 8)} ${baseSym}` : '—'
                        : o.cost != null ? `${formatQuoteAmount(o.cost, 4)} ${quoteSym}` : '—';

                      const st = (o.display_status ?? o.status ?? 'FILLED').toUpperCase();
                      return (
                        <tr key={o.order_row_id} className="text-magi-on-bg/80 hover:bg-white/[0.02]">
                          <td className="py-2 pr-3 font-mono text-magi-muted/60">{formatLogTimeExec(o.created_at)}</td>
                          <td className={`py-2 pr-3 font-black tracking-wider ${isBuy ? 'text-magi-tertiary' : 'text-magi-secondary'}`}>
                            {isBuy ? '▲ BUY' : '▼ SELL'}
                          </td>
                          <td className="py-2 pr-3 text-right font-mono">{spentLabel}</td>
                          <td className={`py-2 pr-3 text-right font-mono font-bold ${isBuy ? 'text-magi-tertiary' : 'text-magi-secondary'}`}>
                            {receivedLabel}
                          </td>
                          <td className="py-2 pr-3 text-right font-mono text-magi-muted/70">
                            {avgPx != null ? `${formatExecPrice(avgPx)}` : '—'}
                          </td>
                          <td className="py-2 text-right text-magi-muted/50">{st}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              /* ── TRADE SUMMARY / PnL TABLE ── */
              <div>
                {/* FIFO info banner */}
                <div className="mb-2 flex items-start gap-1.5 rounded border border-magi-grid/20 bg-magi-grid/5 px-3 py-2">
                  <Info className="mt-0.5 h-3 w-3 shrink-0 text-magi-muted/50" strokeWidth={2} />
                  <p className="font-label text-[9px] leading-snug text-magi-muted/50">
                    Each row is one closed trade — a sell matched against prior buys using{' '}
                    <span className="font-bold text-magi-muted/70">FIFO</span> cost accounting.
                    Entry price is the weighted average cost basis of the consumed lots.
                  </p>
                </div>
                <div className="max-h-[280px] overflow-auto">
                  {tradeSummaryLoading && tradeSummary === null ? (
                    <p className="py-4 font-label text-[10px] italic text-magi-muted/50">Loading…</p>
                  ) : (
                    <table className="w-full min-w-[42rem] text-left font-label text-[10px] sm:text-[11px]">
                      <thead className="sticky top-0 border-b border-magi-grid/10 bg-magi-bg uppercase text-magi-muted/40">
                        <tr>
                          <th className="py-2 pr-3 font-normal">Exit Time</th>
                          <th className="py-2 pr-3 text-right font-normal">Qty</th>
                          <th className="py-2 pr-3 text-right font-normal">Entry Price</th>
                          <th className="py-2 pr-3 text-right font-normal">Exit Price</th>
                          <th className="py-2 pr-3 text-right font-normal">Cost Basis</th>
                          <th className="py-2 pr-3 text-right font-normal">Proceeds</th>
                          <th className="py-2 text-right font-normal">Realized PnL</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-magi-grid/5">
                        {(tradeSummary ?? []).length === 0 && (
                          <tr>
                            <td colSpan={7} className="py-4 italic text-magi-muted/60">
                              No closed trades yet — PnL appears here after the first matched buy→sell pair.
                            </td>
                          </tr>
                        )}
                        {[...(tradeSummary ?? [])].reverse().map((t, i) => {
                          const qc = t.quote_currency;
                          const outcomeLabel =
                            t.outcome === 'win' ? '▲ W' : t.outcome === 'loss' ? '▼ L' : '= B';
                          const outcomeBadge =
                            t.outcome === 'win'
                              ? 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30'
                              : t.outcome === 'loss'
                              ? 'bg-red-500/15 text-red-400 border-red-500/30'
                              : 'bg-magi-grid/10 text-magi-muted/50 border-magi-grid/20';
                          return (
                            <tr
                              key={i}
                              className={`hover:bg-white/[0.02] ${
                                t.outcome === 'win'
                                  ? 'bg-emerald-500/[0.03]'
                                  : t.outcome === 'loss'
                                  ? 'bg-red-500/[0.03]'
                                  : ''
                              }`}
                            >
                              <td className="py-2 pr-3 font-mono text-magi-muted/60">
                                {t.timestamp != null ? formatLogTimeExec(t.timestamp) : '—'}
                              </td>
                              <td className="py-2 pr-3 text-right font-mono text-magi-on-bg/70">
                                {formatQuoteAmount(t.quantity, 6)}
                              </td>
                              <td className="py-2 pr-3 text-right font-mono text-magi-muted/70">
                                {t.entry_price != null ? formatExecPrice(t.entry_price) : '—'}
                              </td>
                              <td className="py-2 pr-3 text-right font-mono text-magi-muted/70">
                                {t.exit_price != null ? formatExecPrice(t.exit_price) : '—'}
                              </td>
                              <td className="py-2 pr-3 text-right font-mono text-magi-muted/50">
                                {formatQuoteAmount(t.cost_basis_quote, 4)} {qc}
                              </td>
                              <td className="py-2 pr-3 text-right font-mono text-magi-muted/50">
                                {formatQuoteAmount(t.proceeds_quote, 4)} {qc}
                              </td>
                              <td className="py-2 text-right">
                                <span className={`inline-flex items-center gap-1.5 rounded border px-1.5 py-0.5 font-mono text-[10px] font-bold ${outcomeBadge}`}>
                                  <span className="font-label text-[8px] tracking-wider">{outcomeLabel}</span>
                                  {t.realized_pnl >= 0 ? '+' : ''}
                                  {formatQuoteAmount(t.realized_pnl, 4)} {qc}
                                </span>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                      {(tradeSummary ?? []).length > 0 && (
                        <tfoot className="border-t border-magi-grid/20">
                          <tr>
                            <td colSpan={6} className="py-2 pr-3 font-label text-[9px] uppercase tracking-wider text-magi-muted/40">
                              {(tradeSummary ?? []).length} closed trades ·{' '}
                              {(tradeSummary ?? []).filter((t) => t.outcome === 'win').length}W ·{' '}
                              {(tradeSummary ?? []).filter((t) => t.outcome === 'loss').length}L ·{' '}
                              {(tradeSummary ?? []).filter((t) => t.outcome === 'flat').length}B
                            </td>
                            <td className="py-2 text-right">
                              {(() => {
                                const total = (tradeSummary ?? []).reduce((s, t) => s + t.realized_pnl, 0);
                                const qc = tradeSummary?.[0]?.quote_currency ?? 'USDT';
                                return (
                                  <span className={`font-mono text-[11px] font-black ${total >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                                    {total >= 0 ? '+' : ''}{formatQuoteAmount(total, 4)} {qc}
                                  </span>
                                );
                              })()}
                            </td>
                          </tr>
                        </tfoot>
                      )}
                    </table>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>

        {/* ── RIGHT COLUMN (log) ────────────────────────────── */}
        <aside className="col-span-1 flex min-h-[240px] min-w-0 flex-col overflow-hidden border-t border-magi-grid/20 bg-magi-container-low sm:min-h-[280px] lg:col-span-3 lg:min-h-0 lg:border-t-0">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-magi-grid/20 bg-magi-surface-dim px-3 py-2">
            <span className="font-label flex items-center gap-2 text-[10px] font-bold uppercase tracking-widest text-magi-tertiary">
              <span
                className={`h-2 w-2 shrink-0 rounded-full ${
                  bot?.status === 'running' ? 'animate-ping bg-magi-tertiary' : 'bg-magi-muted/40'
                }`}
              />
              EXECUTION_LOG · {logsChronological.length} lines
            </span>
            <button
              type="button"
              onClick={() => void copyLogs()}
              disabled={!logsPlainText}
              className="font-label flex items-center gap-1.5 rounded border border-magi-tertiary/30 bg-black/30 px-2 py-1 text-[9px] font-bold uppercase tracking-wider text-magi-tertiary transition-colors hover:border-magi-tertiary/60 hover:bg-magi-tertiary/10 disabled:cursor-not-allowed disabled:opacity-40"
              title="Copy all visible log lines to clipboard"
            >
              <Copy className="h-3 w-3" strokeWidth={2} />
              {logsCopied ? 'COPIED' : 'COPY LOGS'}
            </button>
          </div>
          <div className="relative flex min-h-0 flex-1 flex-col">
            <div
              ref={logScrollRef}
              onScroll={onLogScroll}
              className="phosphor-green min-h-0 flex-1 overflow-y-auto overflow-x-hidden bg-black/40 p-3 font-mono text-xs leading-snug text-magi-tertiary sm:p-4"
              data-purpose="execution-logs"
            >
              {logsChronological.length === 0 && (
                <p className="opacity-60">[ — ] NO_TELEMETRY — start bot after keys configured.</p>
              )}
              {logsChronological.map((log) => (
                <p key={log.log_id} className={`mb-1.5 break-words ${logLineClass(log.level)}`}>
                  [{formatLogTime(log.created_at)}] [{log.execution_mode}] [{log.level}] {log.message}
                </p>
              ))}
              {bot?.status === 'running' && (
                <p className="mt-4 font-mono text-[10px] text-magi-tertiary/50">
                  ● polling every 4s — {localDateTimeStr(new Date())}
                </p>
              )}
            </div>
            {!followLogBottom && (
              <button
                type="button"
                onClick={jumpToLatestLogs}
                className="font-label absolute bottom-3 right-3 z-10 flex items-center gap-1 rounded border border-magi-primary/50 bg-magi-primary/90 px-2.5 py-1.5 text-[10px] font-black uppercase tracking-widest text-black shadow-lg shadow-orange-900/40 hover:brightness-110"
              >
                <ChevronDown className="h-3.5 w-3.5" strokeWidth={2.5} />
                Latest
              </button>
            )}
          </div>
        </aside>
      </main>

      <footer className="flex shrink-0 flex-col gap-0 border-t-2 border-green-900/30 bg-[#131313] shadow-[0_0_10px_rgba(0,231,58,0.08)]">
        {/* Promote / Demote bar */}
        <div className={`flex items-center justify-between gap-3 px-4 py-2 border-b ${
          botExecMode === 'live'
            ? 'border-red-900/40 bg-red-950/20'
            : 'border-blue-900/30 bg-blue-950/10'
        }`}>
          <div className="flex items-center gap-2">
            <span className={`h-2 w-2 rounded-full ${botExecMode === 'live' ? 'bg-red-400 animate-pulse' : 'bg-blue-400'}`} />
            <span className={`font-label text-[10px] font-bold uppercase tracking-widest ${botExecMode === 'live' ? 'text-red-400' : 'text-blue-300'}`}>
              {botExecMode === 'live' ? '⚠ LIVE SPOT TRADING — real funds at risk' : 'Testnet — virtual funds, safe to run'}
            </span>
          </div>
          {botExecMode !== 'live' ? (
            <button
              type="button"
              disabled={bot?.status === 'running' || promoteBusy}
              onClick={() => setShowPromoteModal(true)}
              title={bot?.status === 'running' ? 'Stop the bot first' : 'Promote to Live Spot trading'}
              className="font-headline px-4 py-1.5 text-[9px] font-black uppercase tracking-widest bg-red-600/80 hover:bg-red-500 text-white rounded disabled:opacity-40 transition-colors"
            >
              PROMOTE TO LIVE →
            </button>
          ) : (
            <button
              type="button"
              disabled={bot?.status === 'running' || promoteBusy}
              onClick={() => {
                if (window.confirm('Demote this bot back to Testnet? It will stop trading real funds.'))
                  void promoteBot('testnet');
              }}
              title={bot?.status === 'running' ? 'Stop the bot first' : 'Demote to Testnet'}
              className="font-headline px-4 py-1.5 text-[9px] font-black uppercase tracking-widest bg-blue-700/60 hover:bg-blue-600 text-blue-200 rounded disabled:opacity-40 transition-colors"
            >
              ← DEMOTE TO TESTNET
            </button>
          )}
        </div>

        {/* Bot controls */}
        <div className="flex flex-col items-stretch justify-between gap-3 px-2 py-2 sm:h-12 sm:flex-row sm:items-center sm:px-4">
          <div className="flex flex-wrap items-center gap-4 md:gap-6">
            <span className="font-label font-mono text-[9px] font-semibold uppercase tracking-widest text-green-500">
              MAGI_OS_CORE · SYSTEM_{bot?.status === 'running' ? 'STABLE' : 'IDLE'}
            </span>
            <div className="flex flex-wrap gap-3 md:gap-4">
              <span className="font-label text-[9px] uppercase tracking-widest text-green-900">
                NODE: {bot?.status === 'running' ? 'GREEN' : 'AMBER'}
              </span>
              <span className={`font-label text-[9px] font-bold uppercase tracking-widest ${botExecMode === 'live' ? 'text-red-400' : 'text-blue-300'}`}>
                {botExecMode.toUpperCase()}
              </span>
            </div>
          </div>
          <div className="flex min-h-10 w-full flex-wrap gap-px sm:h-full sm:min-h-0 sm:w-auto sm:flex-nowrap">
            {bot?.status === 'stopped' ? (
              <button type="button" disabled={busy} onClick={() => setStatus('running')}
                className="font-headline min-h-10 min-w-0 flex-1 bg-magi-tertiary px-3 text-[9px] font-black uppercase tracking-widest text-black hover:brightness-110 disabled:opacity-50 sm:flex-none sm:px-6 sm:text-[10px]">
                START
              </button>
            ) : bot?.status === 'paused' ? (
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  if (!confirmRiskOverride()) return;
                  void setStatus('running', { resetRiskProtections: true });
                }}
                className="font-headline min-h-10 min-w-0 flex-1 bg-magi-tertiary px-3 text-[9px] font-black uppercase tracking-widest text-black hover:brightness-110 disabled:opacity-50 sm:flex-none sm:px-6 sm:text-[10px]"
              >
                RESUME + RESET RISK
              </button>
            ) : (
              <button type="button" disabled={busy} onClick={() => setStatus('paused')}
                className="font-headline min-h-10 min-w-0 flex-1 bg-yellow-500 px-3 text-[9px] font-black uppercase tracking-widest text-black hover:brightness-110 disabled:opacity-50 sm:flex-none sm:px-6 sm:text-[10px]">
                PAUSE
              </button>
            )}
            <button type="button" disabled={busy}
              onClick={() => { if (window.confirm('Terminate this bot?')) void setStatus('stopped'); }}
              className="font-headline min-h-10 min-w-0 flex-1 bg-magi-hot px-3 text-[9px] font-black uppercase tracking-widest text-black hover:brightness-110 disabled:opacity-50 sm:flex-none sm:px-6 sm:text-[10px]">
              TERMINATE
            </button>
            <Link to="/bots"
              className="font-headline flex min-h-10 min-w-0 flex-1 items-center justify-center border-l-2 border-black/20 px-3 text-center text-[9px] font-black uppercase tracking-widest text-black hover:brightness-110 sm:flex-none sm:px-6 sm:text-[10px] warning-stripe">
              BOT_LIST
            </Link>
          </div>
        </div>
      </footer>

      {/* ── PROMOTE TO LIVE MODAL ──────────────────────────────────────── */}
      {showPromoteModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 backdrop-blur-sm p-4">
          <div className="bg-[#161616] border border-red-900/60 rounded-xl w-full max-w-lg shadow-2xl shadow-red-950/40">
            <div className="px-6 py-5 border-b border-red-900/40">
              <h2 className="text-sm font-black uppercase tracking-widest text-red-400">
                ⚠ Promote to Live Spot Trading
              </h2>
            </div>
            <div className="px-6 py-5 flex flex-col gap-4">
              <div className="rounded-lg border border-red-900/40 bg-red-950/20 px-4 py-3 text-sm text-red-300 leading-relaxed">
                <p className="font-bold mb-2">This will switch the bot to real Binance Spot orders.</p>
                <ul className="list-disc list-inside space-y-1 text-[12px] text-red-300/80">
                  <li>The exact same strategy runs — only the exchange endpoint changes</li>
                  <li>Orders will use your <strong>real API keys</strong> on <code className="text-red-200">api.binance.com</code></li>
                  <li>Real USDT/BTC from your live Spot wallet will be at risk</li>
                  <li>You can demote back to Testnet at any time (bot must be stopped)</li>
                </ul>
              </div>
              <div className="rounded-lg border border-border bg-black/20 px-4 py-3 text-[11px] text-gray-400">
                <span className="font-bold text-white">Bot:</span> {bot?.name} · {bot?.symbol}<br />
                <span className="font-bold text-white">Strategy:</span> {bot?.strategy?.toUpperCase()}<br />
                <span className="font-bold text-white">Budget:</span>{' '}
                {strategyHealth?.initial_budget_quote != null
                  ? `${strategyHealth.initial_budget_quote.toLocaleString()} USDT`
                  : 'not set — set a budget before going live'}
              </div>
              {error && (
                <p className="text-red-400 text-xs border border-red-500/40 bg-red-950/20 rounded p-2">{error}</p>
              )}
              <div className="flex gap-2 pt-1">
                <button type="button" disabled={promoteBusy}
                  onClick={() => void promoteBot('live')}
                  className="flex-1 py-3 bg-red-600 hover:bg-red-500 text-white text-[11px] font-black uppercase tracking-widest rounded disabled:opacity-40 transition-all">
                  {promoteBusy ? 'Promoting…' : 'Yes, Go Live with Real Funds'}
                </button>
                <button type="button" onClick={() => { setShowPromoteModal(false); setActionError(null); }}
                  className="px-4 py-3 border border-border text-gray-400 text-[11px] font-bold uppercase tracking-widest rounded hover:border-gray-500 transition-all">
                  Cancel
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
