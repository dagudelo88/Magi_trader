import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { Copy, ChevronDown } from 'lucide-react';
import { BotTacticalChart } from '../components/BotTacticalChart';
import { API_BASE, CHART_OHLCV_POLL_INTERVAL_MS } from '../config';

/** Pixels from bottom to consider the user "at" the latest log line. */
const LOG_BOTTOM_THRESHOLD_PX = 72;

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

interface StrategyHealth {
  realized_pnl_quote: number;
  unrealized_pnl_quote: number | null;
  open_base_position: number;
  open_cost_basis_quote: number;
  closed_trades: number;
  winning_trades: number;
  losing_trades: number;
  breakeven_trades: number;
  win_rate_pct: number | null;
  max_drawdown_quote: number;
  max_drawdown_pct: number | null;
  quote_currency: string;
  mark_price: number | null;
  total_pnl_quote: number;
  initial_budget_quote: number | null;
  current_capital_quote: number | null;
  pnl_return_on_budget_pct: number | null;
  max_drawdown_vs_budget_pct: number | null;
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
  display_price?: number | null;
  display_status?: string;
}

function formatLogTime(ms: number) {
  try {
    return new Date(ms).toISOString().replace('T', ' ').slice(0, 19);
  } catch {
    return String(ms);
  }
}

function formatLogTimeExec(ms: number) {
  try {
    return new Date(ms).toISOString().replace('T', ' ').slice(0, 23);
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

export default function BotDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [bot, setBot] = useState<BotRecord | null>(null);
  const [logs, setLogs] = useState<BotLogRow[]>([]);
  const [orderStats, setOrderStats] = useState<BotOrderStats | null>(null);
  const [orders, setOrders] = useState<BotOrderRow[]>([]);
  const [strategyHealth, setStrategyHealth] = useState<StrategyHealth | null>(null);
  const [executionMode, setExecutionMode] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [budgetDraft, setBudgetDraft] = useState('');
  const [budgetBusy, setBudgetBusy] = useState(false);
  const [forkApplyBudget, setForkApplyBudget] = useState(false);
  const [forkBusy, setForkBusy] = useState(false);
  const [forkNameDraft, setForkNameDraft] = useState('');
  const [followLogBottom, setFollowLogBottom] = useState(true);
  const [logsCopied, setLogsCopied] = useState(false);
  const logScrollRef = useRef<HTMLDivElement>(null);
  const logScrollRafRef = useRef<number | null>(null);

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
      setStrategyHealth(data.strategy_health ?? null);
      setExecutionMode(data.execution_mode ?? null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Load failed');
    }
  }, [id]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    const b = strategyHealth?.initial_budget_quote;
    if (b != null && b > 0) setBudgetDraft(String(b));
    else setBudgetDraft('');
  }, [strategyHealth?.initial_budget_quote, id]);

  useEffect(() => {
    setFollowLogBottom(true);
  }, [id]);

  useEffect(() => {
    if (!id || bot?.status !== 'running') return;
    const t = window.setInterval(refresh, 4000);
    return () => window.clearInterval(t);
  }, [id, bot?.status, refresh]);

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

  const forkNewBotInstance = async () => {
    if (!id) return;
    const msg =
      'Create a new bot instance from this one? This bot\u2019s orders and logs stay here forever; the new bot gets a new id and starts with empty history. The exchange is unchanged.';
    if (!window.confirm(msg)) return;
    setForkBusy(true);
    setError(null);
    try {
      const payload: Record<string, unknown> = {};
      if (forkNameDraft.trim()) payload.name = forkNameDraft.trim();
      if (forkApplyBudget) {
        const trimmed = budgetDraft.trim();
        if (trimmed === '') payload.initial_budget_quote = null;
        else {
          const n = Number.parseFloat(trimmed);
          if (!Number.isFinite(n) || n < 0) {
            setError('Budget must be empty (clear) or a non-negative number when applying on fork.');
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
      setError(e instanceof Error ? e.message : 'Fork failed');
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
      setError('Initial budget must be a number');
      return;
    }
    if (
      trimmed !== '' &&
      typeof body.initial_budget_quote === 'number' &&
      body.initial_budget_quote < 0
    ) {
      setError('Initial budget cannot be negative');
      return;
    }
    setBudgetBusy(true);
    setError(null);
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
      setError(e instanceof Error ? e.message : 'Save failed');
    } finally {
      setBudgetBusy(false);
    }
  };

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

  const liveLabel =
    executionMode === 'live' ? 'LIVE TRADING' : executionMode === 'testnet' ? 'TESTNET' : 'OFFLINE';
  const strategyTag = bot?.strategy?.toUpperCase().replace(/-/g, '_') ?? '—';
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

        {/* ── LEFT COLUMN ───────────────────────────────────── */}
        <div className="col-span-1 flex min-h-0 min-w-0 flex-col overflow-y-auto overflow-x-hidden border-r border-magi-grid/15 lg:col-span-8">

          {/* Compact header */}
          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b border-magi-grid/30 px-4 py-3 sm:px-6">
            <div className="flex min-w-0 flex-wrap items-center gap-3">
              <h1 className="font-headline text-2xl font-black uppercase italic leading-none tracking-tighter text-magi-primary phosphor-amber sm:text-3xl">
                {symbolHeadline(bot?.symbol)}
              </h1>
              <span
                className={`font-label inline-flex items-center border px-2 py-0.5 text-[10px] font-bold uppercase tracking-widest ${
                  executionMode === 'live'
                    ? 'border-magi-tertiary/30 bg-magi-tertiary/10 text-magi-tertiary phosphor-green'
                    : 'border-blue-400/30 bg-blue-500/10 text-blue-300'
                }`}
              >
                <span
                  className={`mr-1.5 h-1.5 w-1.5 rounded-full ${
                    bot?.status === 'running' ? 'animate-pulse bg-magi-tertiary' : 'bg-magi-muted/50'
                  }`}
                />
                {liveLabel}
              </span>
              <span className="font-label text-[10px] uppercase tracking-widest text-magi-muted/50">
                {strategyTag} · {bot?.name ?? '…'}
              </span>
            </div>
            <p className="font-headline text-xl font-bold tracking-tight text-magi-on-bg sm:text-2xl">
              {(bot?.status ?? '—').toUpperCase()}
            </p>
          </div>

          {error && (
            <div className="mx-4 mt-3 border border-red-500/50 bg-red-950/20 p-3 text-sm text-red-300 sm:mx-6">
              {error}
            </div>
          )}

          {/* Chart — flush, full width of left column */}
          {bot?.symbol ? (
            <BotTacticalChart
              symbol={bot.symbol}
              timeframe={chartConfig.timeframe}
              limit={chartConfig.limit}
              fastPeriod={chartConfig.fastPeriod}
              slowPeriod={chartConfig.slowPeriod}
              liveOhlcvPollMs={CHART_OHLCV_POLL_INTERVAL_MS}
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
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <h3 className="font-label text-[11px] font-bold uppercase tracking-widest text-magi-muted">
                Execution History
              </h3>
              <span className="font-label text-[9px] tracking-tight text-magi-muted/40">
                {orderStats?.total_orders ?? 0} fills
              </span>
            </div>
            <div className="max-h-[240px] overflow-auto">
              <table className="w-full min-w-[36rem] text-left font-label text-[10px] sm:text-[11px]">
                <thead className="sticky top-0 border-b border-magi-grid/10 bg-magi-bg uppercase text-magi-muted/40">
                  <tr>
                    <th className="py-2 pr-2 font-normal">Timestamp</th>
                    <th className="py-2 pr-2 font-normal">Action</th>
                    <th className="py-2 pr-2 text-right font-normal">Price</th>
                    <th className="py-2 pr-2 text-right font-normal">Size</th>
                    <th className="py-2 text-right font-normal">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-magi-grid/5">
                  {orders.length === 0 && (
                    <tr>
                      <td colSpan={5} className="py-4 italic text-magi-muted/60">
                        No fills yet — appears here after the first accepted buy/sell.
                      </td>
                    </tr>
                  )}
                  {orders.map((o) => {
                    const action = o.side === 'buy' ? 'BUY_LONG' : 'SELL_SHORT';
                    const px =
                      o.display_price != null
                        ? o.display_price
                        : o.average != null
                          ? o.average
                          : null;
                    const price = px != null ? formatExecPrice(px) : '—';
                    const size =
                      o.side === 'sell' && o.amount != null
                        ? `${o.amount}`
                        : o.side === 'buy' && o.cost != null
                          ? `${o.cost} quote`
                          : o.filled != null
                            ? String(o.filled)
                            : '—';
                    const st = (o.display_status ?? o.status ?? 'FILLED').toUpperCase();
                    return (
                      <tr key={o.order_row_id} className="text-magi-on-bg/80">
                        <td className="py-2 font-mono">{formatLogTimeExec(o.created_at)}</td>
                        <td
                          className={`py-2 font-bold ${
                            o.side === 'buy' ? 'text-magi-tertiary' : 'text-magi-secondary'
                          }`}
                        >
                          {action}
                        </td>
                        <td className="py-2 text-right font-mono">{price}</td>
                        <td className="py-2 text-right font-mono">{size}</td>
                        <td className="py-2 text-right text-magi-tertiary">[{st}]</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>

          {/* Collapsible utility panels */}
          <div className="flex flex-col gap-2 px-4 py-3 sm:px-6 sm:py-4">
            <details className="border border-magi-grid/20 bg-magi-container-low open:border-magi-primary/20">
              <summary className="cursor-pointer px-4 py-3 font-label text-[11px] font-bold uppercase tracking-widest text-magi-primary/80 hover:text-magi-primary">
                Capital &amp; New Instance
              </summary>
              <div className="border-t border-magi-grid/20 px-4 py-4">
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
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
                      <span className="text-magi-muted/50">New instance name (optional)</span>
                      <input
                        type="text"
                        placeholder={`Default: ${bot?.name ?? 'Bot'} (copy)`}
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
                      <span>Apply budget from field above on fork</span>
                    </label>
                    <button
                      type="button"
                      disabled={forkBusy}
                      onClick={() => void forkNewBotInstance()}
                      className="rounded border border-magi-tertiary/50 bg-magi-tertiary/15 px-3 py-2 text-[11px] font-bold uppercase tracking-widest text-magi-tertiary hover:bg-magi-tertiary/25 disabled:opacity-40"
                    >
                      {forkBusy ? '…' : 'New bot instance (keep this history)'}
                    </button>
                  </div>
                </div>
              </div>
            </details>

            <details className="border border-magi-grid/20 bg-magi-container-low open:border-magi-primary/20">
              <summary className="cursor-pointer px-4 py-3 font-label text-[11px] font-bold uppercase tracking-widest text-magi-primary/60 hover:text-magi-primary">
                Strategy Params (raw)
              </summary>
              <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all border-t border-magi-grid/20 p-4 font-mono text-[10px] text-magi-on-bg/80">
                {formatParamsJson(bot?.strategy_params_json ?? null)}
              </pre>
            </details>
          </div>
        </div>

        {/* ── RIGHT COLUMN (log) ────────────────────────────── */}
        <aside className="col-span-1 flex min-h-[240px] min-w-0 flex-col overflow-hidden border-t border-magi-grid/20 bg-magi-container-low sm:min-h-[280px] lg:col-span-4 lg:min-h-0 lg:border-t-0">
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
                  ● polling every 4s — {new Date().toISOString().slice(0, 19)}Z
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

      <footer className="flex shrink-0 flex-col items-stretch justify-between gap-3 border-t-2 border-green-900/30 bg-[#131313] px-2 py-2 shadow-[0_0_10px_rgba(0,231,58,0.08)] sm:h-12 sm:flex-row sm:items-center sm:px-4">
        <div className="flex flex-wrap items-center gap-4 md:gap-6">
          <span className="font-label font-mono text-[9px] font-semibold uppercase tracking-widest text-green-500">
            MAGI_OS_CORE · SYSTEM_{bot?.status === 'running' ? 'STABLE' : 'IDLE'}
          </span>
          <div className="flex flex-wrap gap-3 md:gap-4">
            <span className="font-label text-[9px] uppercase tracking-widest text-green-900">
              NODE: {bot?.status === 'running' ? 'GREEN' : 'AMBER'}
            </span>
            <span className="font-label text-[9px] font-bold uppercase tracking-widest text-green-400">
              BACKEND: {executionMode?.toUpperCase() ?? '—'}
            </span>
          </div>
        </div>
        <div className="flex min-h-10 w-full flex-wrap gap-px sm:h-full sm:min-h-0 sm:w-auto sm:flex-nowrap">
          {bot?.status !== 'running' ? (
            <button
              type="button"
              disabled={busy}
              onClick={() => setStatus('running')}
              className="font-headline min-h-10 min-w-0 flex-1 bg-magi-tertiary px-3 text-[9px] font-black uppercase tracking-widest text-black hover:brightness-110 disabled:opacity-50 sm:flex-none sm:px-6 sm:text-[10px]"
            >
              START
            </button>
          ) : (
            <button
              type="button"
              disabled={busy}
              onClick={() => setStatus('paused')}
              className="font-headline min-h-10 min-w-0 flex-1 bg-yellow-500 px-3 text-[9px] font-black uppercase tracking-widest text-black hover:brightness-110 disabled:opacity-50 sm:flex-none sm:px-6 sm:text-[10px]"
            >
              PAUSE
            </button>
          )}
          {bot?.status === 'paused' && (
            <button
              type="button"
              disabled={busy}
              onClick={() => setStatus('running')}
              className="font-headline min-h-10 min-w-0 flex-1 border-l border-black/20 bg-magi-tertiary/80 px-3 text-[9px] font-black uppercase tracking-widest text-black hover:brightness-110 disabled:opacity-50 sm:flex-none sm:px-6 sm:text-[10px]"
            >
              RESUME
            </button>
          )}
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              if (window.confirm('Terminate this bot?')) setStatus('stopped');
            }}
            className="font-headline min-h-10 min-w-0 flex-1 bg-magi-hot px-3 text-[9px] font-black uppercase tracking-widest text-black hover:brightness-110 disabled:opacity-50 sm:flex-none sm:px-6 sm:text-[10px]"
          >
            TERMINATE
          </button>
          <Link
            to="/bots"
            className="font-headline flex min-h-10 min-w-0 flex-1 items-center justify-center border-l-2 border-black/20 px-3 text-center text-[9px] font-black uppercase tracking-widest text-black hover:brightness-110 sm:flex-none sm:px-6 sm:text-[10px] warning-stripe"
          >
            BOT_LIST
          </Link>
        </div>
      </footer>
    </div>
  );
}
