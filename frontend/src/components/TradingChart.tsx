import { useEffect, useRef } from 'react';
import { createChart, ColorType, CandlestickSeries } from 'lightweight-charts';
import type { IChartApi, ISeriesApi, Time } from 'lightweight-charts';

export function TradingChart({ 
  symbol,
  data, // Optional initial historical data: { time: string, open: number, high: number, low: number, close: number }[]
  latestTick // The newest websocket tick to append: { price: number, time: Date }
}: {
  symbol: string;
  data?: any[];
  latestTick?: { price: number, time: Date };
}) {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const currentCandleRef = useRef<any>(null);

  useEffect(() => {
    if (!chartContainerRef.current) return;

    // Initialize the Chart
    const chart = createChart(chartContainerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: '#9ca3af', // text-gray-400
      },
      grid: {
        vertLines: { color: '#2d3748' }, // border border-border
        horzLines: { color: '#2d3748' },
      },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 12,
        barSpacing: 15,
      },
      rightPriceScale: {
        borderVisible: false,
      }
    });

    // Create the Candlestick Series
    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#4ade80', // text-green-400
      downColor: '#f87171', // text-red-400
      borderVisible: false,
      wickUpColor: '#4ade80',
      wickDownColor: '#f87171',
    });

    chartRef.current = chart;
    seriesRef.current = series;

    // Load initial historical data if provided
    if (data && data.length > 0) {
      series.setData(data);
      // Grab the last candle to continue mutating it
      currentCandleRef.current = { ...data[data.length - 1] };
    } else {
      // Create a dummy start candle if no history exists (just for immediate visualization)
      const now = Math.floor(Date.now() / 1000) as Time;
      const startPrice = latestTick?.price || 50000;
      const initialCandle = {
        time: now,
        open: startPrice,
        high: startPrice,
        low: startPrice,
        close: startPrice
      };
      series.setData([initialCandle]);
      currentCandleRef.current = initialCandle;
    }

    // Handle window resizing
    const handleResize = () => {
      if (chartContainerRef.current && chartRef.current) {
        chartRef.current.applyOptions({ 
          width: chartContainerRef.current.clientWidth,
          height: chartContainerRef.current.clientHeight 
        });
      }
    };

    window.addEventListener('resize', handleResize);
    // Trigger initial resize
    handleResize();

    return () => {
      window.removeEventListener('resize', handleResize);
      if (chartRef.current) {
        chartRef.current.remove();
      }
    };
  }, []);

  // Update logic: This runs every time a new tick arrives
  useEffect(() => {
    if (!seriesRef.current || !latestTick || !currentCandleRef.current) return;

    const tickTimeSeconds = Math.floor(latestTick.time.getTime() / 1000) as Time;
    const price = latestTick.price;
    const currentCandle = currentCandleRef.current;

    // Determine if we need to start a new 1-minute candle
    // Modulo 60 means a new minute just started
    const isNewMinute = (tickTimeSeconds as number) % 60 === 0 && (tickTimeSeconds as number) > (currentCandle.time as number);

    if (isNewMinute) {
      // Start a brand new candle
      const newCandle = {
        time: tickTimeSeconds,
        open: price,
        high: price,
        low: price,
        close: price
      };
      seriesRef.current.update(newCandle);
      currentCandleRef.current = newCandle;
    } else {
      // Mutate the existing open candle
      const updatedCandle = {
        ...currentCandle,
        close: price,
        high: Math.max(currentCandle.high, price),
        low: Math.min(currentCandle.low, price)
      };
      seriesRef.current.update(updatedCandle);
      currentCandleRef.current = updatedCandle;
    }

  }, [latestTick]);

  return (
    <div className="w-full h-full min-h-[300px] flex flex-col">
      <div className="flex justify-between items-center mb-2 px-1">
        <h3 className="text-sm font-bold text-white uppercase tracking-wider">{symbol} - Live 1m Chart</h3>
        <span className="text-xs text-gray-500 bg-surface px-2 py-1 rounded border border-border">Local Rendering Engine</span>
      </div>
      <div 
        ref={chartContainerRef} 
        className="flex-1 w-full border border-border rounded overflow-hidden" 
      />
    </div>
  );
}
