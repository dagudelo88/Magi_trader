export default function Settings() {
  return (
    <main className="flex-1 flex overflow-hidden p-6">
      <div className="w-full max-w-3xl">
        <h1 className="text-2xl font-bold text-white mb-6">Settings</h1>
        
        <div className="space-y-6">
          <div className="bg-panel border border-border rounded-custom p-6">
            <h3 className="text-lg font-bold text-white mb-4">Binance API Keys</h3>
            <div className="space-y-4">
              <div>
                <label className="block text-xs text-gray-500 mb-1">API Key</label>
                <input type="password" value="********************************" readOnly className="w-full bg-surface border border-border rounded px-3 py-2 text-white text-sm focus:outline-none" />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">Secret Key</label>
                <input type="password" value="********************************" readOnly className="w-full bg-surface border border-border rounded px-3 py-2 text-white text-sm focus:outline-none" />
              </div>
            </div>
          </div>
          
          <div className="bg-panel border border-border rounded-custom p-6">
            <h3 className="text-lg font-bold text-white mb-4">Trading Preferences</h3>
            <div className="flex items-center justify-between py-2 border-b border-border">
              <div>
                <div className="font-bold text-white text-sm">Use Testnet</div>
                <div className="text-xs text-gray-500">Route all live trades to Binance Testnet</div>
              </div>
              <div className="w-10 h-6 bg-primary rounded-full relative">
                <div className="w-4 h-4 bg-white rounded-full absolute right-1 top-1"></div>
              </div>
            </div>
            <div className="flex items-center justify-between py-2 mt-2">
              <div>
                <div className="font-bold text-white text-sm">Global Killswitch</div>
                <div className="text-xs text-gray-500">Instantly halt all live trading bots</div>
              </div>
              <button className="px-4 py-1.5 bg-red-600/20 text-red-500 border border-red-900/50 rounded text-sm font-bold hover:bg-red-600 hover:text-white transition-colors">
                HALT ALL
              </button>
            </div>
          </div>
        </div>
      </div>
    </main>
  );
}
