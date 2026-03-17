export default function Performance() {
  return (
    <main className="flex-1 flex flex-col overflow-hidden p-6">
      <div className="w-full mb-6">
        <h1 className="text-2xl font-bold text-white">System Performance</h1>
        <p className="text-sm text-gray-400">Aggregate analytics across all bots (Simulated vs Live)</p>
      </div>
      
      <div className="flex-1 grid grid-cols-2 gap-6">
        <div className="bg-panel border border-border rounded-custom p-6 flex flex-col">
          <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400 mb-6 flex justify-between">
            <span>Live Equity</span>
            <span className="text-green-400">+$12,450.82</span>
          </h3>
          <div className="flex-1 flex items-center justify-center border-2 border-dashed border-border rounded-custom text-gray-600">
            Live Chart
          </div>
        </div>
        
        <div className="bg-panel border border-border rounded-custom p-6 flex flex-col">
          <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400 mb-6 flex justify-between">
            <span>Simulated Equity</span>
            <span className="text-blue-400">+$3,210.55</span>
          </h3>
          <div className="flex-1 flex items-center justify-center border-2 border-dashed border-border rounded-custom text-gray-600">
            Sim Chart
          </div>
        </div>
      </div>
    </main>
  );
}
