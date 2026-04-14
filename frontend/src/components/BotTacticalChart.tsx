import { useEffect, useRef } from 'react';
import {
  CandlestickSeries,
  ColorType,
  LineSeries,
  createChart,
} from 'lightweight-charts';
import type { IChartApi, ISeriesApi, UTCTimestamp } from 'lightweight-charts';
import { API_BASE, CHART_OHLCV_POLL_INTERVAL_MS } from '../config';

type RemoteCandle = { t: number; o: number; h: number; l: number; c: number; v: number };

function rollingSma(closes: number[], period: number): (number | null)[] {
  const out: (number | null)[] = [];
  for (let i = 0; i < closes.length; i++) {
    if (i + 1 < period) {
      out.push(null);
      continue;
    }
    const slice = closes.slice(i - period + 1, i + 1);
    out.push(slice.reduce((a, b) => a + b, 0) / period);
  }
  return out;
}

type Props = {
  symbol: string;
  timeframe: string;
  limit: number;
  fastPeriod: number;
  slowPeriod: number;
  /** OHLCV + SMA refresh interval (default: one minute — see `CHART_OHLCV_POLL_INTERVAL_MS`). */
  liveOhlcvPollMs?: number;
};

export function BotTacticalChart({
  symbol,
  timeframe,
  limit,
  fastPeriod,
  slowPeriod,
  liveOhlcvPollMs = CHART_OHLCV_POLL_INTERVAL_MS,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const fastRef = useRef<ISeriesApi<'Line'> | null>(null);
  const slowRef = useRef<ISeriesApi<'Line'> | null>(null);
  const ohlcvFirstFitRef = useRef(true);

  useEffect(() => {
    if (!containerRef.current || !symbol) return;

    ohlcvFirstFitRef.current = true;

    const chart = createChart(containerRef.current, {
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
    });

    const candle = chart.addSeries(CandlestickSeries, {
      upColor: '#00e73a',
      downColor: '#ff5540',
      borderVisible: false,
      wickUpColor: '#00e73a',
      wickDownColor: '#ff5540',
    });
    const fastLine = chart.addSeries(LineSeries, {
      color: '#ff9100',
      lineWidth: 2,
      title: `SMA ${fastPeriod}`,
      priceLineVisible: false,
    });
    const slowLine = chart.addSeries(LineSeries, {
      color: '#38bdf8',
      lineWidth: 2,
      title: `SMA ${slowPeriod}`,
      priceLineVisible: false,
    });

    chartRef.current = chart;
    candleRef.current = candle;
    fastRef.current = fastLine;
    slowRef.current = slowLine;

    const resize = () => {
      if (!containerRef.current || !chartRef.current) return;
      chartRef.current.applyOptions({
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
      chartRef.current = null;
      candleRef.current = null;
      fastRef.current = null;
      slowRef.current = null;
    };
  }, [fastPeriod, slowPeriod, symbol]);

  useEffect(() => {
    if (!candleRef.current || !fastRef.current || !slowRef.current || !chartRef.current || !symbol) {
      return;
    }

    let cancelled = false;

    const load = async () => {
      const u = new URL(`${API_BASE}/api/market/ohlcv`);
      u.searchParams.set('symbol', symbol);
      u.searchParams.set('timeframe', timeframe);
      u.searchParams.set('limit', String(limit));
      const res = await fetch(u.toString());
      if (!res.ok || cancelled) return;
      const payload = await res.json();
      const candles: RemoteCandle[] = payload.candles || [];
      if (!candles.length || cancelled) return;

      const candle = candleRef.current;
      const fastLine = fastRef.current;
      const slowLine = slowRef.current;
      const chart = chartRef.current;
      if (!candle || !fastLine || !slowLine || !chart || cancelled) return;

      const candleData = candles.map((c) => ({
        time: Math.floor(c.t / 1000) as UTCTimestamp,
        open: c.o,
        high: c.h,
        low: c.l,
        close: c.c,
      }));

      const closes = candles.map((c) => c.c);
      const fast = rollingSma(closes, fastPeriod);
      const slow = rollingSma(closes, slowPeriod);

      const fastPts = candleData
        .map((d, i) => (fast[i] != null ? { time: d.time, value: fast[i]! } : null))
        .filter((x): x is { time: UTCTimestamp; value: number } => x != null);
      const slowPts = candleData
        .map((d, i) => (slow[i] != null ? { time: d.time, value: slow[i]! } : null))
        .filter((x): x is { time: UTCTimestamp; value: number } => x != null);

      candle.setData(candleData);
      fastLine.setData(fastPts);
      slowLine.setData(slowPts);
      if (ohlcvFirstFitRef.current) {
        chart.timeScale().fitContent();
        ohlcvFirstFitRef.current = false;
      } else {
        chart.timeScale().scrollToRealTime();
      }
    };

    void load();

    const intervalId =
      liveOhlcvPollMs > 0
        ? window.setInterval(() => void load(), liveOhlcvPollMs)
        : 0;

    return () => {
      cancelled = true;
      if (intervalId) window.clearInterval(intervalId);
    };
  }, [symbol, timeframe, limit, fastPeriod, slowPeriod, liveOhlcvPollMs]);

  return (
    <div className="relative h-[min(500px,58vh)] w-full overflow-hidden border border-magi-grid/20 bg-magi-container-low sm:h-[min(560px,60vh)] 2xl:h-[min(640px,64vh)]">
      <div className="pointer-events-none absolute inset-0 z-10 opacity-15 scanline" />
      <div
        className="absolute inset-0 z-0 opacity-[0.07]"
        style={{
          backgroundImage:
            'linear-gradient(to right, #564334 1px, transparent 1px), linear-gradient(to bottom, #564334 1px, transparent 1px)',
          backgroundSize: '40px 40px',
        }}
      />
      <div className="absolute left-3 top-3 z-20 flex flex-wrap gap-2">
        <span className="border border-magi-grid/40 bg-magi-surface-dim/90 px-2 py-1 font-label text-[9px] text-magi-muted">
          {timeframe}
        </span>
        <span className="border border-magi-primary/40 bg-magi-primary/15 px-2 py-1 font-label text-[9px] text-magi-primary">
          SMA_{fastPeriod}
        </span>
        <span className="border border-blue-500/40 bg-blue-500/15 px-2 py-1 font-label text-[9px] text-blue-400">
          SMA_{slowPeriod}
        </span>
        <span className="border border-magi-grid/40 bg-black/50 px-2 py-1 font-label text-[9px] text-magi-muted/80">
          OHLCV · {symbol}
        </span>
      </div>
      {/* containerRef fills the outer wrapper exactly — ResizeObserver always gets correct dimensions */}
      <div ref={containerRef} className="absolute inset-0 z-[5]" />
    </div>
  );
}
