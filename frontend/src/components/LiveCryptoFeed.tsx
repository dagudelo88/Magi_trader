import { useState, useEffect, useRef } from "react";

const TOP_10_PAIRS = [
  "btcusdt", "ethusdt", "adausdt", "solusdt", 
  "xrpusdt", "bnbusdt", "dogeusdt", "trxusdt"
];

const UPDATE_INTERVAL_MS = 1000;

type PriceData = {
  price: string;
  trend: 'up' | 'down' | 'flat';
};

export default function LiveCryptoFeed() {
  const [prices, setPrices] = useState<Record<string, PriceData>>({});
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [timeToNext, setTimeToNext] = useState(UPDATE_INTERVAL_MS);
  
  // Use a ref to buffer incoming WebSocket updates without triggering re-renders
  const pendingPrices = useRef<Record<string, string>>({});
  const previousPrices = useRef<Record<string, string>>({});

  useEffect(() => {
    let ignore = false;
    // Connect to Binance Public WebSocket using @trade to get every single actual trade that happens instantly
    const streams = TOP_10_PAIRS.map(pair => `${pair}@trade`).join("/");
    const ws = new WebSocket(`wss://stream.binance.com:9443/stream?streams=${streams}`);

    ws.onopen = () => {
      if (!ignore) {
        console.log("Connected to Binance WebSocket (Frontend - Trades)");
      }
    };

    ws.onmessage = (event) => {
      if (ignore) return;
      try {
        const data = JSON.parse(event.data);
        if (data.stream && data.data) {
          const symbol = data.data.s; // e.g. "BTCUSDT"
          // 'p' in the @trade payload is the actual executed trade price
          const lastPrice = parseFloat(data.data.p).toFixed(6);
          pendingPrices.current[symbol] = lastPrice;
        }
      } catch (e) {
        console.error("WebSocket message parsing error", e);
      }
    };

    ws.onerror = (err) => {
      if (!ignore) {
        if (ws.readyState === WebSocket.CLOSING || ws.readyState === WebSocket.CLOSED) return;
        console.error("WebSocket error:", err);
      }
    };

    return () => {
      ignore = true;
      if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
        ws.close();
      }
    };
  }, []);

  // Timer and Batch Update Loop
  useEffect(() => {
    const flushInterval = setInterval(() => {
      // Flush buffered prices to state once per second
      if (Object.keys(pendingPrices.current).length > 0) {
        setPrices(prev => {
          const nextState: Record<string, PriceData> = { ...prev };
          
          // Force update all pairs we are tracking
          TOP_10_PAIRS.forEach(pair => {
            const symbol = pair.toUpperCase();
            const newPrice = pendingPrices.current[symbol] || previousPrices.current[symbol];
            
            if (newPrice) {
              const oldPrice = previousPrices.current[symbol];
              let trend: 'up' | 'down' | 'flat' = 'flat';
              
              if (oldPrice) {
                const newNum = parseFloat(newPrice);
                const oldNum = parseFloat(oldPrice);
                if (newNum > oldNum) trend = 'up';
                else if (newNum < oldNum) trend = 'down';
              }
              
              nextState[symbol] = { price: newPrice, trend };
              previousPrices.current[symbol] = newPrice;
            }
          });
          
          return nextState;
        });
        setLastUpdated(new Date());
      }
      // Reset visual timer
      setTimeToNext(UPDATE_INTERVAL_MS);
    }, UPDATE_INTERVAL_MS);

    // Visual progress bar for the next batch update
    const timerInterval = setInterval(() => {
      setTimeToNext(prev => Math.max(0, prev - 50));
    }, 50);

    return () => {
      clearInterval(flushInterval);
      clearInterval(timerInterval);
    };
  }, []);

  const formatTime = (date: Date) => {
    const hhmmss = date.toLocaleTimeString(navigator.language, { hour12: false });
    const ms = date.getMilliseconds().toString().padStart(3, '0');
    return `${hhmmss}.${ms}`;
  };

  return (
    <div className="bg-panel border border-border rounded-custom p-4 flex flex-col h-full">
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center mb-4 gap-3">
        <h3 className="text-sm font-bold text-white uppercase tracking-wider flex items-center gap-2">
          <span className="relative flex h-3 w-3">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75"></span>
            <span className="relative inline-flex rounded-full h-3 w-3 bg-green-500"></span>
          </span>
          Live Market Feed (Batch Sync)
        </h3>
        
        <div className="text-xs text-gray-400 flex items-center gap-4 bg-surface px-3 py-1.5 rounded-full border border-border">
          <div className="font-mono flex gap-1 items-center">
            <svg className="w-3.5 h-3.5 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth="2"><path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
            {lastUpdated ? formatTime(lastUpdated) : "Syncing..."}
          </div>
          <div className="w-px h-3 bg-border"></div>
          <div className="flex items-center gap-2">
            <span className="text-gray-500 font-semibold">T-MINUS</span>
            <div className="w-16 h-1.5 bg-panel rounded-full overflow-hidden">
              <div 
                className="h-full bg-primary transition-all ease-linear" 
                style={{ 
                  width: `${(timeToNext / UPDATE_INTERVAL_MS) * 100}%`,
                  transitionDuration: '50ms'
                }}
              ></div>
            </div>
          </div>
        </div>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 overflow-y-auto">
        {TOP_10_PAIRS.map((pair) => {
          const symbol = pair.toUpperCase();
          const data = prices[symbol];
          
          let colorClass = "text-white";
          if (data?.trend === 'up') colorClass = "text-green-400";
          if (data?.trend === 'down') colorClass = "text-red-400";
          
          return (
            <div key={pair} className="bg-surface border border-border p-3 rounded-lg flex flex-col items-center justify-center relative overflow-hidden group">
              <span className="text-xs text-gray-500 font-bold tracking-wider relative z-10">{symbol.replace('USDT', '')}</span>
              <span className={`text-sm font-mono mt-1 relative z-10 transition-colors duration-300 ${colorClass}`}>
                {data ? `$${data.price}` : "..."}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
