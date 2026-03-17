export default function Dashboard() {
  return (
    <main className="flex-1 flex overflow-hidden p-6">
      <div className="w-full">
        <h1 className="text-2xl font-bold text-white mb-6">Dashboard</h1>
        <div className="grid grid-cols-3 gap-6 mb-8">
          <div className="bg-panel border border-border p-6 rounded-custom">
            <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400 mb-2">Total Value</h3>
            <div className="text-3xl font-mono font-bold text-white">$45,231.89</div>
          </div>
          <div className="bg-panel border border-border p-6 rounded-custom">
            <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400 mb-2">Active Bots</h3>
            <div className="text-3xl font-mono font-bold text-white">4 <span className="text-sm text-green-500 font-sans ml-2">2 Live / 2 Sim</span></div>
          </div>
          <div className="bg-panel border border-border p-6 rounded-custom">
            <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400 mb-2">24h PnL</h3>
            <div className="text-3xl font-mono font-bold text-green-400">+$412.50</div>
          </div>
        </div>
        <div className="bg-panel border border-border p-6 rounded-custom min-h-[400px]">
          <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400 mb-6">Portfolio Equity Curve</h3>
          <div className="flex items-center justify-center h-full text-gray-600 border-2 border-dashed border-border rounded-custom">
            Chart Placeholder
          </div>
        </div>
      </div>
    </main>
  );
}
