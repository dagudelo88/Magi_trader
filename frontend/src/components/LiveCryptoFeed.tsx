import { useRealtimeStore } from "../stores/realtimeStore";

type PriceData = {
  price: string;
  trend: 'up' | 'down' | 'flat';
};

export default function LiveCryptoFeed() {
  const marketTickers = useRealtimeStore((state) => state.marketTickers);
  const streamIds = useRealtimeStore((state) => state.trackedStreamIds);
  const marketUpdatedAt = useRealtimeStore((state) => state.marketUpdatedAt);
  const lastUpdated = marketUpdatedAt ? new Date(marketUpdatedAt) : null;

  const prices = streamIds.reduce<Record<string, PriceData>>((acc, pair) => {
    const symbol = pair.toUpperCase();
    const ticker = marketTickers[symbol];
    if (!ticker) return acc;
    const change = Number(ticker.change);
    acc[symbol] = {
      price: ticker.price,
      trend: change > 0 ? 'up' : change < 0 ? 'down' : 'flat',
    };
    return acc;
  }, {});

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
          Live Market Feed
        </h3>
        
        <div className="text-xs text-gray-400 flex items-center gap-4 bg-surface px-3 py-1.5 rounded-full border border-border">
          <div className="font-mono flex gap-1 items-center">
            <svg className="w-3.5 h-3.5 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth="2"><path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
            {lastUpdated ? formatTime(lastUpdated) : "Syncing..."}
          </div>
        </div>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 overflow-y-auto">
        {streamIds.map((pair) => {
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
