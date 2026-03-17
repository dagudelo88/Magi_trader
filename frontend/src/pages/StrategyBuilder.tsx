export default function StrategyBuilder() {
  return (
    <main className="flex-1 flex overflow-hidden p-6">
      <div className="w-full flex flex-col">
        <h1 className="text-2xl font-bold text-white mb-6">Strategy Builder</h1>
        <div className="flex-1 flex gap-6">
          <div className="flex-1 bg-panel border border-border rounded-custom p-6 flex flex-col">
            <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400 mb-4">Code Editor</h3>
            <textarea 
              className="flex-1 bg-surface border border-border rounded p-4 text-gray-300 font-mono text-sm focus:outline-none focus:border-primary resize-none"
              defaultValue="# Write your Python strategy here...&#10;def on_tick(state):&#10;    pass"
            />
          </div>
          <div className="w-[300px] bg-panel border border-border rounded-custom p-6">
            <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400 mb-4">Configuration</h3>
            <div className="space-y-4">
              <div>
                <label className="block text-xs text-gray-500 mb-1">Strategy Name</label>
                <input type="text" className="w-full bg-surface border border-border rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-primary" placeholder="My Strategy" />
              </div>
              <button className="w-full py-2 bg-primary text-white rounded font-bold text-sm hover:bg-primary/90 transition-colors">
                Save Strategy
              </button>
            </div>
          </div>
        </div>
      </div>
    </main>
  );
}
