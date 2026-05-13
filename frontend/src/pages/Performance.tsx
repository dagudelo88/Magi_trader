import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  ColorType,
  HistogramSeries,
  LineSeries,
  createChart,
} from 'lightweight-charts';
import type { IChartApi, UTCTimestamp } from 'lightweight-charts';
import { API_BASE } from '../config';
import { useRealtimeStore, type BotRow, type StrategyHealth } from '../stores/realtimeStore';

function fmtAmt(n: number | null | undefined, currency: string): string {
  if (n == null || Number.isNaN(n)) return '—';
  const abs = Math.abs(n);
  const digits = abs >= 1 ? 4 : abs >= 0.0001 ? 6 : 8;
  const s = n.toFixed(digits);
  return `${s} ${currency}`;
}

function fmtPct(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return '—';
  return `${n.toFixed(2)}%`;
}

const LB_STATUS_BADGE: Record<string, string> = {
  running: 'bg-green-500/15 text-green-400 border border-green-500/25',
  paused: 'bg-amber-500/15 text-amber-400 border border-amber-500/25',
  stopped: 'bg-gray-500/15 text-gray-400 border border-gray-500/25',
};

function leaderboardStatusClass(s: string) {
  return LB_STATUS_BADGE[s] ?? LB_STATUS_BADGE.stopped;
}

type LeaderboardSort = 'testnet' | 'live' | 'combined';

function pnlFromMetrics(m: StrategyHealth | undefined): number {
  return m?.realized_pnl_quote ?? 0;
}

function capitalFromMetrics(m: StrategyHealth | undefined): number | null {
  const v = m?.current_capital_quote;
  return v != null && Number.isFinite(v) ? v : null;
}

function BotsLeaderboard(props: {
  bots: BotRow[];
  selectedId: string;
  onSelectBot: (botId: string) => void;
}) {
  const { bots, selectedId, onSelectBot } = props;
  const [sortBy, setSortBy] = useState<LeaderboardSort>('combined');

  const quoteCcy = useMemo(() => {
    const first = bots[0];
    const tn = first?.metrics?.testnet?.quote_currency;
    const lv = first?.metrics?.live?.quote_currency;
    return tn ?? lv ?? 'USDT';
  }, [bots]);

  const rows = useMemo(() => {
    const enriched = bots.map((bot) => {
      const tn = bot.metrics?.testnet;
      const lv = bot.metrics?.live;
      const pnlTestnet = pnlFromMetrics(tn);
      const pnlLive = pnlFromMetrics(lv);
      const combined = pnlTestnet + pnlLive;
      const score =
        sortBy === 'testnet' ? pnlTestnet : sortBy === 'live' ? pnlLive : combined;
      const tradesTn = tn?.closed_trades ?? 0;
      const tradesLv = lv?.closed_trades ?? 0;
      const capTn = capitalFromMetrics(tn);
      const capLv = capitalFromMetrics(lv);
      const capCombined =
        capTn == null && capLv == null ? null : (capTn ?? 0) + (capLv ?? 0);
      return {
        bot,
        pnlTestnet,
        pnlLive,
        combined,
        score,
        tradesTn,
        tradesLv,
        capTn,
        capLv,
        capCombined,
      };
    });
    enriched.sort((a, b) => {
      const d = b.score - a.score;
      if (Math.abs(d) > 1e-12) return d;
      const tb = (b.tradesTn + b.tradesLv) - (a.tradesTn + a.tradesLv);
      if (tb !== 0) return tb;
      return a.bot.name.localeCompare(b.bot.name);
    });
    return enriched;
  }, [bots, sortBy]);

  const footerTotals = useMemo(() => {
    let pnlTn = 0;
    let pnlLv = 0;
    let capTn = 0;
    let capLv = 0;
    let tradesTn = 0;
    let tradesLv = 0;
    for (const bot of bots) {
      const tn = bot.metrics?.testnet;
      const lv = bot.metrics?.live;
      pnlTn += pnlFromMetrics(tn);
      pnlLv += pnlFromMetrics(lv);
      tradesTn += tn?.closed_trades ?? 0;
      tradesLv += lv?.closed_trades ?? 0;
      const ct = capitalFromMetrics(tn);
      const cl = capitalFromMetrics(lv);
      if (ct != null) capTn += ct;
      if (cl != null) capLv += cl;
    }
    return {
      pnlTn,
      pnlLv,
      combined: pnlTn + pnlLv,
      capTn,
      capLv,
      capSum: capTn + capLv,
      tradesTn,
      tradesLv,
    };
  }, [bots]);

  const sortButtons: { id: LeaderboardSort; label: string }[] = [
    { id: 'combined', label: 'Combined profit' },
    { id: 'testnet', label: 'Testnet profit' },
    { id: 'live', label: 'Live profit' },
  ];

  return (
    <section className="bg-panel border border-border rounded-custom p-5 mb-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between mb-4">
        <div>
          <h2 className="text-sm font-black uppercase tracking-widest text-white">Leaderboard</h2>
          <p className="text-[11px] text-gray-500 mt-1">
            Rankings use realized profit from filled orders. Capital is current equity in {quoteCcy} per network.
            Click a row to load charts below.
          </p>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {sortButtons.map(({ id, label }) => (
            <button
              key={id}
              type="button"
              onClick={() => setSortBy(id)}
              className={`rounded px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider transition-colors ${
                sortBy === id
                  ? 'bg-primary text-black'
                  : 'border border-border bg-black/30 text-gray-400 hover:border-primary/40 hover:text-gray-200'
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="overflow-x-auto rounded border border-border/80">
        <table className="w-full min-w-[1020px] text-left text-[11px]">
          <thead>
            <tr className="border-b border-border bg-black/25 text-[10px] font-black uppercase tracking-widest text-gray-500">
              <th rowSpan={2} className="px-3 py-2 w-11 align-bottom">
                Rank
              </th>
              <th rowSpan={2} className="px-3 py-2 align-bottom">
                Bot
              </th>
              <th rowSpan={2} className="px-3 py-2 align-bottom">
                Pair
              </th>
              <th rowSpan={2} className="px-3 py-2 align-bottom">
                Status
              </th>
              <th rowSpan={2} className="px-3 py-2 align-bottom max-w-[100px]">
                Trading mode
              </th>
              <th
                colSpan={3}
                className="px-3 py-2 text-center border-l border-border/60 bg-orange-950/10 text-orange-200/90"
              >
                Realized profit ({quoteCcy})
              </th>
              <th
                colSpan={3}
                className="px-3 py-2 text-center border-l border-border/60 bg-sky-950/15 text-sky-200/90"
              >
                Capital · equity ({quoteCcy})
              </th>
              <th rowSpan={2} className="px-3 py-2 text-right align-bottom font-sans normal-case tracking-normal">
                <span className="block uppercase tracking-widest text-[10px] font-black text-gray-500">
                  Closed trades
                </span>
                <span className="block text-[9px] font-normal font-mono text-gray-600 mt-1">
                  testnet / live
                </span>
              </th>
            </tr>
            <tr className="border-b border-border bg-black/20 text-[10px] font-bold uppercase tracking-wide text-gray-400">
              <th className="px-3 py-2 text-right font-mono border-l border-border/60 bg-orange-950/10">
                Testnet
              </th>
              <th className="px-3 py-2 text-right font-mono bg-orange-950/10">Live</th>
              <th className="px-3 py-2 text-right font-mono bg-orange-950/10">Total</th>
              <th className="px-3 py-2 text-right font-mono border-l border-border/60 bg-sky-950/15 text-blue-300/95">
                Testnet
              </th>
              <th className="px-3 py-2 text-right font-mono bg-sky-950/15 text-blue-300/95">
                Live
              </th>
              <th className="px-3 py-2 text-right font-mono bg-sky-950/15 text-blue-200">
                Total
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map(
              (
                {
                  bot,
                  pnlTestnet,
                  pnlLive,
                  combined,
                  tradesTn,
                  tradesLv,
                  capTn,
                  capLv,
                  capCombined,
                },
                idx,
              ) => {
              const rank = idx + 1;
              const active = bot.bot_id === selectedId;
              return (
                <tr
                  key={bot.bot_id}
                  className={`border-b border-border/60 cursor-pointer transition-colors ${
                    active ? 'bg-primary/10 hover:bg-primary/15' : 'hover:bg-white/[0.03]'
                  }`}
                  onClick={() => onSelectBot(bot.bot_id)}
                >
                  <td className="px-3 py-2.5 font-mono text-gray-500 tabular-nums">
                    <span className={rank <= 3 ? 'text-primary font-bold' : ''}>{rank}</span>
                  </td>
                  <td className="px-3 py-2.5 font-semibold text-white">
                    <Link
                      to={`/bots/${bot.bot_id}`}
                      className="hover:text-primary hover:underline"
                      onClick={(e) => e.stopPropagation()}
                    >
                      {bot.name}
                    </Link>
                  </td>
                  <td className="px-3 py-2.5 font-mono text-gray-400">{bot.symbol}</td>
                  <td className="px-3 py-2.5">
                    <span
                      className={`inline-block rounded px-2 py-0.5 text-[9px] font-bold uppercase tracking-wide ${leaderboardStatusClass(bot.status)}`}
                    >
                      {bot.status}
                    </span>
                  </td>
                  <td className="px-3 py-2.5 font-mono text-[10px] uppercase text-gray-500">
                    {bot.execution_mode === 'live' ? 'Live' : 'Testnet'}
                  </td>
                  <td
                    className={`px-3 py-2.5 text-right font-mono ${
                      pnlTestnet >= 0 ? 'text-green-400' : 'text-red-400'
                    }`}
                  >
                    {fmtAmt(pnlTestnet, quoteCcy)}
                  </td>
                  <td
                    className={`px-3 py-2.5 text-right font-mono ${
                      pnlLive >= 0 ? 'text-green-400' : 'text-red-400'
                    }`}
                  >
                    {fmtAmt(pnlLive, quoteCcy)}
                  </td>
                  <td
                    className={`px-3 py-2.5 text-right font-mono font-bold ${
                      combined >= 0 ? 'text-green-400' : 'text-red-400'
                    }`}
                  >
                    {fmtAmt(combined, quoteCcy)}
                  </td>
                  <td className="px-3 py-2.5 text-right font-mono text-blue-300/90">
                    {capTn != null ? fmtAmt(capTn, quoteCcy) : '—'}
                  </td>
                  <td className="px-3 py-2.5 text-right font-mono text-blue-300/90">
                    {capLv != null ? fmtAmt(capLv, quoteCcy) : '—'}
                  </td>
                  <td className="px-3 py-2.5 text-right font-mono font-semibold text-blue-200">
                    {capCombined != null ? fmtAmt(capCombined, quoteCcy) : '—'}
                  </td>
                  <td className="px-3 py-2.5 text-right font-mono text-gray-400">
                    {tradesTn}/{tradesLv}
                  </td>
                </tr>
              );
            })}
          </tbody>
          <tfoot>
            <tr className="border-t-2 border-border bg-black/35 text-[11px]">
              <td colSpan={5} className="px-3 py-2.5 font-black uppercase tracking-wider text-gray-400">
                Totals ({bots.length} bots)
              </td>
              <td
                className={`px-3 py-2.5 text-right font-mono font-bold ${
                  footerTotals.pnlTn >= 0 ? 'text-green-400' : 'text-red-400'
                }`}
              >
                {fmtAmt(footerTotals.pnlTn, quoteCcy)}
              </td>
              <td
                className={`px-3 py-2.5 text-right font-mono font-bold ${
                  footerTotals.pnlLv >= 0 ? 'text-green-400' : 'text-red-400'
                }`}
              >
                {fmtAmt(footerTotals.pnlLv, quoteCcy)}
              </td>
              <td
                className={`px-3 py-2.5 text-right font-mono font-bold ${
                  footerTotals.combined >= 0 ? 'text-green-400' : 'text-red-400'
                }`}
              >
                {fmtAmt(footerTotals.combined, quoteCcy)}
              </td>
              <td className="px-3 py-2.5 text-right font-mono font-bold text-blue-300/90">
                {fmtAmt(footerTotals.capTn, quoteCcy)}
              </td>
              <td className="px-3 py-2.5 text-right font-mono font-bold text-blue-300/90">
                {fmtAmt(footerTotals.capLv, quoteCcy)}
              </td>
              <td className="px-3 py-2.5 text-right font-mono font-bold text-blue-200">
                {fmtAmt(footerTotals.capSum, quoteCcy)}
              </td>
              <td className="px-3 py-2.5 text-right font-mono text-gray-300">
                {footerTotals.tradesTn}/{footerTotals.tradesLv}
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
    </section>
  );
}

type ClosedTrade = {
  timestamp: number | null;
  quantity: number;
  entry_price: number | null;
  exit_price: number | null;
  cost_basis_quote: number;
  proceeds_quote: number;
  realized_pnl: number;
  outcome: string;
  quote_currency: string;
};

type ExecutionHistorySlice = {
  mode: string;
  active_execution_mode?: string;
  trades: ClosedTrade[];
  metrics: StrategyHealth;
  realized_pnl_quote: number;
  win_rate_pct: number | null;
  best_trade_pnl: number | null;
  worst_trade_pnl: number | null;
};

type ExecutionHistoryBoth = {
  mode: 'both';
  active_execution_mode: string;
  histories: {
    testnet: ExecutionHistorySlice;
    live: ExecutionHistorySlice;
  };
};

function sortedTrades(trades: ClosedTrade[]): ClosedTrade[] {
  return [...trades].sort((a, b) => {
    const ta = a.timestamp ?? 0;
    const tb = b.timestamp ?? 0;
    return ta - tb;
  });
}

function cumulativeSeries(trades: ClosedTrade[]): Array<{ timeMs: number; cumulative: number }> {
  const sorted = sortedTrades(trades);
  let cum = 0;
  return sorted.map((t, i) => {
    cum += t.realized_pnl;
    const ms =
      t.timestamp != null && t.timestamp > 1e11
        ? t.timestamp
        : t.timestamp != null && t.timestamp > 1e9
          ? t.timestamp * 1000
          : null;
    const timeMs = ms ?? i;
    return { timeMs, cumulative: cum };
  });
}

/** Map cumulative samples to strictly increasing UTC seconds for lightweight-charts. */
function toLineData(points: Array<{ timeMs: number; cumulative: number }>): {
  time: UTCTimestamp;
  value: number;
}[] {
  const FALLBACK_BASE = 1_600_000_000;
  let lastSec = 0;
  return points.map((d, i) => {
    let sec: number;
    if (d.timeMs >= 1e12) sec = Math.floor(d.timeMs / 1000);
    else if (d.timeMs >= 1e9) sec = Math.floor(d.timeMs);
    else sec = FALLBACK_BASE + i;
    if (sec <= lastSec) sec = lastSec + 1;
    lastSec = sec;
    return { time: sec as UTCTimestamp, value: d.cumulative };
  });
}

/**
 * Quote equity after each closed trade: baseline + cumulative realized P&L.
 * Prepends a point just before the first exit (baseline). Optionally extends one step to
 * `reportedCapital` when it differs (e.g. open position / unrealized included in API capital).
 */
function capitalEquityLineData(
  cumData: Array<{ timeMs: number; cumulative: number }>,
  lineData: { time: UTCTimestamp; value: number }[],
  baselineRaw: number | null | undefined,
  reportedCapital: number | null | undefined,
): { time: UTCTimestamp; value: number }[] {
  if (lineData.length === 0) return [];
  const baseline = baselineRaw != null && Number.isFinite(baselineRaw) ? baselineRaw : 0;

  const firstSec = lineData[0].time as number;
  const startSec = Math.max(1, firstSec - 1) as UTCTimestamp;

  const pts: { time: UTCTimestamp; value: number }[] = [{ time: startSec, value: baseline }];

  lineData.forEach((pt, i) => {
    pts.push({
      time: pt.time,
      value: baseline + (cumData[i]?.cumulative ?? 0),
    });
  });

  const realizedEnd =
    baseline + (cumData[cumData.length - 1]?.cumulative ?? 0);
  if (
    reportedCapital != null &&
    Number.isFinite(reportedCapital) &&
    Math.abs(reportedCapital - realizedEnd) > 1e-10
  ) {
    const lastT = pts[pts.length - 1]?.time as number;
    pts.push({ time: (lastT + 1) as UTCTimestamp, value: reportedCapital });
  }

  return pts;
}

const ANALYTICS_CHART_PANEL = {
  layout: {
    background: { type: ColorType.Solid, color: '#1c1b1b' },
    textColor: '#dcc2ae',
  },
  grid: {
    vertLines: { color: 'rgba(86, 67, 52, 0.35)' },
    horzLines: { color: 'rgba(86, 67, 52, 0.35)' },
  },
  timeScale: {
    timeVisible: true,
    secondsVisible: false,
    borderColor: '#564334',
    rightOffset: 4,
  },
  rightPriceScale: { borderColor: '#564334' },
  crosshair: {
    vertLine: { color: 'rgba(255, 145, 0, 0.35)' },
    horzLine: { color: 'rgba(255, 145, 0, 0.35)' },
  },
} as const;

function createAnalyticsChart(container: HTMLElement): IChartApi {
  return createChart(container, { ...ANALYTICS_CHART_PANEL });
}

/** Per-trade P&L bars aligned in exit time order (same ordering as cumulative curve). */
function tradeHistogramPoints(
  trades: ClosedTrade[],
): { time: UTCTimestamp; value: number; color: string }[] {
  const sorted = sortedTrades(trades);
  const FALLBACK_BASE = 1_600_000_000;
  let lastSec = 0;
  return sorted.map((t, i) => {
    let sec: number;
    const ms =
      t.timestamp != null && t.timestamp > 1e11
        ? t.timestamp
        : t.timestamp != null && t.timestamp > 1e9
          ? t.timestamp * 1000
          : null;
    if (ms != null) sec = Math.floor(ms / 1000);
    else sec = FALLBACK_BASE + i;
    if (sec <= lastSec) sec = lastSec + 1;
    lastSec = sec;
    return {
      time: sec as UTCTimestamp,
      value: t.realized_pnl,
      color: t.realized_pnl >= 0 ? '#00e73a' : '#ff5540',
    };
  });
}

function CumulativePnlLwChart({
  lineData,
}: {
  lineData: { time: UTCTimestamp; value: number }[];
}) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!containerRef.current || lineData.length === 0) return;

    const chart = createAnalyticsChart(containerRef.current);

    const line = chart.addSeries(LineSeries, {
      color: '#ff9100',
      lineWidth: 2,
      priceLineVisible: false,
    });
    line.setData(lineData);
    chart.timeScale().fitContent();

    const resize = () => {
      if (!containerRef.current) return;
      chart.applyOptions({
        width: containerRef.current.clientWidth,
        height: containerRef.current.clientHeight,
      });
    };
    const ro = new ResizeObserver(resize);
    ro.observe(containerRef.current);
    resize();

    return () => {
      ro.disconnect();
      chart.remove();
    };
  }, [lineData]);

  if (lineData.length === 0) return null;
  return <div ref={containerRef} className="h-52 w-full min-h-[208px]" />;
}

function CapitalEquityLwChart({
  lineData,
}: {
  lineData: { time: UTCTimestamp; value: number }[];
}) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!containerRef.current || lineData.length === 0) return;

    const chart = createAnalyticsChart(containerRef.current);

    const line = chart.addSeries(LineSeries, {
      color: '#38bdf8',
      lineWidth: 2,
      priceLineVisible: false,
    });
    line.setData(lineData);
    chart.timeScale().fitContent();

    chart.priceScale('right').applyOptions({
      scaleMargins: { top: 0.1, bottom: 0.1 },
    });

    const resize = () => {
      if (!containerRef.current) return;
      chart.applyOptions({
        width: containerRef.current.clientWidth,
        height: containerRef.current.clientHeight,
      });
    };
    const ro = new ResizeObserver(resize);
    ro.observe(containerRef.current);
    resize();

    return () => {
      ro.disconnect();
      chart.remove();
    };
  }, [lineData]);

  if (lineData.length === 0) return null;
  return <div ref={containerRef} className="h-52 w-full min-h-[208px]" />;
}

function PerTradePnlHistogram({
  histogramData,
}: {
  histogramData: { time: UTCTimestamp; value: number; color: string }[];
}) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!containerRef.current || histogramData.length === 0) return;

    const chart = createAnalyticsChart(containerRef.current);
    const n = histogramData.length;
    const barSpacing =
      n > 140 ? 1 : n > 80 ? 2 : n > 45 ? 4 : n > 22 ? 6 : n > 12 ? 8 : 10;

    chart.applyOptions({
      timeScale: {
        ...ANALYTICS_CHART_PANEL.timeScale,
        barSpacing,
        minBarSpacing: 0.5,
        fixLeftEdge: true,
        fixRightEdge: true,
      },
    });

    const hist = chart.addSeries(HistogramSeries, {
      base: 0,
      priceScaleId: 'right',
      priceFormat: {
        type: 'price',
        precision: 6,
        minMove: 0.000_001,
      },
      priceLineVisible: false,
    });
    hist.setData(histogramData);
    chart.timeScale().fitContent();

    chart.priceScale('right').applyOptions({
      scaleMargins: { top: 0.12, bottom: 0.12 },
      borderVisible: true,
    });

    const resize = () => {
      if (!containerRef.current) return;
      chart.applyOptions({
        width: containerRef.current.clientWidth,
        height: containerRef.current.clientHeight,
      });
    };
    const ro = new ResizeObserver(resize);
    ro.observe(containerRef.current);
    resize();

    return () => {
      ro.disconnect();
      chart.remove();
    };
  }, [histogramData]);

  if (histogramData.length === 0) return null;
  return <div ref={containerRef} className="h-56 w-full min-h-[224px]" />;
}

function ModeAnalyticsBlock(props: {
  title: string;
  subtitle: string;
  slice: ExecutionHistorySlice | undefined;
  loading: boolean;
}) {
  const { title, subtitle, slice, loading } = props;
  const qc = slice?.metrics.quote_currency ?? 'USDT';
  const trades = slice?.trades ?? [];
  const metrics = slice?.metrics;

  const cumData = useMemo(() => cumulativeSeries(trades), [trades]);
  const lineData = useMemo(() => toLineData(cumData), [cumData]);
  const capitalLineData = useMemo(
    () =>
      capitalEquityLineData(
        cumData,
        lineData,
        metrics?.adjusted_initial_capital_quote ?? metrics?.initial_budget_quote,
        metrics?.current_capital_quote,
      ),
    [
      cumData,
      lineData,
      metrics?.adjusted_initial_capital_quote,
      metrics?.initial_budget_quote,
      metrics?.current_capital_quote,
    ],
  );
  const histogramData = useMemo(() => tradeHistogramPoints(trades), [trades]);

  if (loading) {
    return (
      <section className="bg-panel border border-border rounded-custom p-5 flex flex-col gap-4 min-h-[320px]">
        <header>
          <h2 className="text-sm font-black uppercase tracking-widest text-white">{title}</h2>
          <p className="text-[11px] text-gray-500 mt-0.5">{subtitle}</p>
        </header>
        <div className="flex-1 flex items-center justify-center border border-dashed border-border rounded-custom text-gray-600 text-sm animate-pulse">
          Loading…
        </div>
      </section>
    );
  }

  if (!slice || !metrics) {
    return (
      <section className="bg-panel border border-border rounded-custom p-5 flex flex-col gap-4 min-h-[200px]">
        <header>
          <h2 className="text-sm font-black uppercase tracking-widest text-white">{title}</h2>
          <p className="text-[11px] text-gray-500 mt-0.5">{subtitle}</p>
        </header>
        <p className="text-sm text-gray-500 py-6 text-center">No data for this network.</p>
      </section>
    );
  }

  return (
    <section className="bg-panel border border-border rounded-custom p-5 flex flex-col gap-4">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-black uppercase tracking-widest text-white">{title}</h2>
          <p className="text-[11px] text-gray-500 mt-0.5">{subtitle}</p>
        </div>
      </header>

      <dl className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 text-[11px]">
        <div className="rounded border border-border bg-black/20 px-3 py-2">
          <dt className="text-gray-500 uppercase tracking-wider">Realized P&L</dt>
          <dd
            className={`font-mono font-bold mt-1 ${
              metrics.realized_pnl_quote >= 0 ? 'text-green-400' : 'text-red-400'
            }`}
          >
            {fmtAmt(metrics.realized_pnl_quote, qc)}
          </dd>
        </div>
        <div className="rounded border border-border bg-black/20 px-3 py-2">
          <dt className="text-gray-500 uppercase tracking-wider">Win rate</dt>
          <dd className="font-mono font-bold mt-1 text-primary">{fmtPct(metrics.win_rate_pct)}</dd>
        </div>
        <div className="rounded border border-border bg-black/20 px-3 py-2">
          <dt className="text-gray-500 uppercase tracking-wider">Closed trades</dt>
          <dd className="font-mono font-bold mt-1 text-white">{metrics.closed_trades}</dd>
        </div>
        <div className="rounded border border-border bg-black/20 px-3 py-2">
          <dt className="text-gray-500 uppercase tracking-wider">Max drawdown</dt>
          <dd className="font-mono font-bold mt-1 text-amber-400/90">
            {fmtAmt(metrics.max_drawdown_quote, qc)}
          </dd>
        </div>
        <div className="rounded border border-blue-900/30 bg-black/20 px-3 py-2">
          <dt className="text-gray-500 uppercase tracking-wider">Current capital</dt>
          <dd className="font-mono font-bold mt-1 text-blue-300">
            {fmtAmt(metrics.current_capital_quote, qc)}
          </dd>
        </div>
        <div className="rounded border border-blue-900/30 bg-black/20 px-3 py-2">
          <dt className="text-gray-500 uppercase tracking-wider">Adj. initial</dt>
          <dd className="font-mono font-bold mt-1 text-gray-200">
            {fmtAmt(metrics.adjusted_initial_capital_quote ?? metrics.initial_budget_quote, qc)}
          </dd>
        </div>
      </dl>

      <dl className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-[11px]">
        <div className="rounded border border-border bg-black/15 px-3 py-2">
          <dt className="text-gray-500 uppercase tracking-wider">Net flows</dt>
          <dd
            className={`font-mono font-bold mt-1 ${
              metrics.net_capital_flow_quote == null
                ? 'text-gray-500'
                : metrics.net_capital_flow_quote >= 0
                  ? 'text-sky-400'
                  : 'text-orange-400'
            }`}
          >
            {metrics.net_capital_flow_quote != null
              ? fmtAmt(metrics.net_capital_flow_quote, qc)
              : '—'}
          </dd>
        </div>
        <div className="rounded border border-border bg-black/15 px-3 py-2">
          <dt className="text-gray-500 uppercase tracking-wider">Return on budget</dt>
          <dd className="font-mono font-bold mt-1 text-primary">
            {fmtPct(metrics.pnl_return_on_budget_pct)}
          </dd>
        </div>
        <div className="rounded border border-border bg-black/15 px-3 py-2">
          <dt className="text-gray-500 uppercase tracking-wider">Total P&L (incl. unreal.)</dt>
          <dd
            className={`font-mono font-bold mt-1 ${
              metrics.total_pnl_quote >= 0 ? 'text-green-400/90' : 'text-red-400/90'
            }`}
          >
            {fmtAmt(metrics.total_pnl_quote, qc)}
          </dd>
        </div>
        <div className="rounded border border-border bg-black/15 px-3 py-2">
          <dt className="text-gray-500 uppercase tracking-wider">Unrealized P&L</dt>
          <dd
            className={`font-mono font-bold mt-1 ${
              metrics.unrealized_pnl_quote == null
                ? 'text-gray-500'
                : metrics.unrealized_pnl_quote >= 0
                  ? 'text-green-400/80'
                  : 'text-red-400/80'
            }`}
          >
            {fmtAmt(metrics.unrealized_pnl_quote, qc)}
          </dd>
        </div>
      </dl>

      {trades.length === 0 ? (
        <p className="text-sm text-gray-500 py-8 text-center border border-dashed border-border rounded-custom">
          No closed trades recorded for this network yet.
        </p>
      ) : (
        <>
          <div>
            <h3 className="text-[10px] font-bold uppercase tracking-widest text-gray-500 mb-2">
              Cumulative realized P&L ({qc})
            </h3>
            <div className="min-w-0 rounded border border-border/60 overflow-hidden">
              <CumulativePnlLwChart lineData={lineData} />
            </div>
          </div>

          <div>
            <h3 className="text-[10px] font-bold uppercase tracking-widest text-gray-500 mb-2">
              Capital ({qc})
            </h3>
            <p className="text-[10px] text-gray-600 mb-2">
              Equity over time: adjusted initial capital plus cumulative realized P&L after each exit.
              If reported current capital differs (e.g. open position / mark-to-market), the series adds one step at the end.
            </p>
            <div className="min-w-0 rounded border border-border/60 overflow-hidden">
              <CapitalEquityLwChart lineData={capitalLineData} />
            </div>
          </div>

          <div>
            <h3 className="text-[10px] font-bold uppercase tracking-widest text-gray-500 mb-2">
              Per-trade realized P&L ({qc})
            </h3>
            <p className="text-[10px] text-gray-600 mb-2">
              Bars grow from zero — green wins, red losses. Time axis follows exit time (oldest → newest).
              Use the crosshair to read exact P&L.
            </p>
            <div className="min-w-0 rounded border border-border/60 overflow-hidden">
              <PerTradePnlHistogram histogramData={histogramData} />
            </div>
          </div>

          {(slice.best_trade_pnl != null || slice.worst_trade_pnl != null) && (
            <p className="text-[10px] text-gray-600 font-mono">
              Best exit {fmtAmt(slice.best_trade_pnl, qc)} · Worst exit{' '}
              {fmtAmt(slice.worst_trade_pnl, qc)}
            </p>
          )}
        </>
      )}
    </section>
  );
}

async function fetchExecutionHistoryBoth(botId: string): Promise<ExecutionHistoryBoth> {
  const res = await fetch(`${API_BASE}/api/bots/${encodeURIComponent(botId)}/execution-history?mode=both`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    const d = err.detail;
    const msg =
      typeof d === 'string' ? d : d != null ? JSON.stringify(d) : `HTTP ${res.status}`;
    throw new Error(msg);
  }
  const data = await res.json();
  if (data?.mode !== 'both' || !data.histories?.testnet || !data.histories?.live) {
    throw new Error('Unexpected analytics payload');
  }
  return data as ExecutionHistoryBoth;
}

export default function Performance() {
  const bots = useRealtimeStore((s) => s.bots);
  const loadBots = useRealtimeStore((s) => s.loadBots);

  const [selectedId, setSelectedId] = useState<string>('');

  useEffect(() => {
    if (bots.length === 0) void loadBots();
  }, [bots.length, loadBots]);

  useEffect(() => {
    if (!selectedId && bots.length > 0) {
      setSelectedId(bots[0].bot_id);
    }
  }, [bots, selectedId]);

  const selectedBot: BotRow | undefined = useMemo(
    () => bots.find((b) => b.bot_id === selectedId),
    [bots, selectedId],
  );

  const query = useQuery({
    queryKey: ['execution-history-both', selectedId],
    queryFn: () => fetchExecutionHistoryBoth(selectedId),
    enabled: Boolean(selectedId),
    staleTime: 45_000,
  });

  return (
    <main className="flex-1 flex flex-col overflow-auto p-4 sm:p-6">
      <div className="w-full max-w-[1600px] mx-auto mb-6 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Analytics</h1>
          <p className="text-sm text-gray-400 mt-1">
            Leaderboard with P&L and capital per network; charts use stored fills and strategy metrics per bot.
          </p>
        </div>

        <label className="flex flex-col gap-1.5 min-w-[220px]">
          <span className="text-[10px] font-bold uppercase tracking-widest text-gray-500">Bot</span>
          <select
            className="rounded border border-border bg-black/40 px-3 py-2 text-sm text-white focus:border-primary/60 focus:outline-none font-mono"
            value={selectedId}
            onChange={(e) => setSelectedId(e.target.value)}
            disabled={bots.length === 0}
          >
            {bots.length === 0 ? (
              <option value="">No bots</option>
            ) : (
              bots.map((b) => (
                <option key={b.bot_id} value={b.bot_id}>
                  {b.name} · {b.symbol} ({b.execution_mode})
                </option>
              ))
            )}
          </select>
        </label>
      </div>

      {bots.length > 0 && (
        <div className="w-full max-w-[1600px] mx-auto">
          <BotsLeaderboard bots={bots} selectedId={selectedId} onSelectBot={setSelectedId} />
        </div>
      )}

      {query.error && (
        <div className="mb-4 rounded border border-red-500/40 bg-red-950/20 px-4 py-3 text-red-300 text-sm">
          {(query.error as Error).message}
        </div>
      )}

      {bots.length === 0 && !query.isFetching ? (
        <p className="text-gray-500 text-sm">Create a bot first to view analytics.</p>
      ) : (
        <div className="w-full max-w-[1600px] mx-auto grid grid-cols-1 xl:grid-cols-2 gap-6 pb-8">
          <ModeAnalyticsBlock
            title="Testnet"
            subtitle={selectedBot ? `${selectedBot.name} · ${selectedBot.symbol}` : ''}
            slice={query.data?.histories.testnet}
            loading={Boolean(selectedId) && query.isLoading}
          />
          <ModeAnalyticsBlock
            title="Live (mainnet)"
            subtitle={selectedBot ? `${selectedBot.name} · ${selectedBot.symbol}` : ''}
            slice={query.data?.histories.live}
            loading={Boolean(selectedId) && query.isLoading}
          />
        </div>
      )}
    </main>
  );
}
