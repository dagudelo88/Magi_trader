import { Link } from 'react-router-dom';

export default function BotsList() {
  return (
    <main className="flex-1 flex flex-col overflow-hidden p-6">
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-2xl font-bold text-white">Your Bots</h1>
        <button className="px-4 py-2 bg-primary text-white rounded-custom text-sm font-bold hover:bg-primary/90 transition-all">
          + Create New Bot
        </button>
      </div>
      
      <div className="grid grid-cols-1 gap-4">
        {/* Mock Bot Item */}
        <Link to="/bots/1" className="bg-panel border border-border p-4 rounded-custom hover:border-primary transition-colors flex justify-between items-center group">
          <div>
            <div className="flex items-center gap-3 mb-1">
              <h3 className="text-lg font-bold text-white">Alpha_Trend_v4</h3>
              <span className="text-[10px] font-bold tracking-widest uppercase bg-blue-500/20 text-blue-400 px-2 py-0.5 rounded">Live</span>
            </div>
            <p className="text-sm text-gray-400">Strategy: Mean Reversion Scalper | Pair: BTC/USDT</p>
          </div>
          <div className="text-right">
            <div className="text-xl font-mono font-bold text-green-400">+$12,450.82</div>
            <div className="text-xs text-gray-500 uppercase tracking-wider font-semibold">Total PnL</div>
          </div>
        </Link>
        
        {/* Mock Bot Item 2 */}
        <Link to="/bots/2" className="bg-panel border border-border p-4 rounded-custom hover:border-primary transition-colors flex justify-between items-center group">
          <div>
            <div className="flex items-center gap-3 mb-1">
              <h3 className="text-lg font-bold text-white">Eth_Momentum_Sim</h3>
              <span className="text-[10px] font-bold tracking-widest uppercase bg-gray-500/20 text-gray-400 px-2 py-0.5 rounded">Sim</span>
            </div>
            <p className="text-sm text-gray-400">Strategy: Breakout Pro | Pair: ETH/USDT</p>
          </div>
          <div className="text-right">
            <div className="text-xl font-mono font-bold text-green-400">+$450.12</div>
            <div className="text-xs text-gray-500 uppercase tracking-wider font-semibold">Sim PnL</div>
          </div>
        </Link>
      </div>
    </main>
  );
}
