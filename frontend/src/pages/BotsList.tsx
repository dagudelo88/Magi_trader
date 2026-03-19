import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { API_BASE } from '../config';

interface BotRow {
  bot_id: string;
  name: string;
  symbol: string;
  strategy: string;
  status: string;
}

export default function BotsList() {
  const [bots, setBots] = useState<BotRow[]>([]);
  const [executionMode, setExecutionMode] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = async () => {
      try {
        const [botsRes, settingsRes] = await Promise.all([
          fetch(`${API_BASE}/api/bots`),
          fetch(`${API_BASE}/api/settings/trading`),
        ]);
        if (!botsRes.ok) throw new Error('Failed to load bots');
        const b = await botsRes.json();
        setBots(b.bots || []);
        if (settingsRes.ok) {
          const s = await settingsRes.json();
          setExecutionMode(s.execution_mode);
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed to load');
      }
    };
    load();
  }, []);

  return (
    <main className="flex-1 flex flex-col overflow-hidden p-6">
      <div className="flex justify-between items-center mb-6">
        <div>
          <h1 className="text-2xl font-bold text-white">Your Bots</h1>
          {executionMode && (
            <p className="text-xs text-gray-500 mt-1">
              Global execution:{' '}
              <span className={executionMode === 'live' ? 'text-red-400' : 'text-blue-400'}>
                {executionMode === 'live' ? 'Mainnet' : 'Testnet'}
              </span>
            </p>
          )}
        </div>
        <button
          type="button"
          className="px-4 py-2 bg-primary text-white rounded-custom text-sm font-bold hover:bg-primary/90 transition-all opacity-60 cursor-not-allowed"
          title="Create flow not wired yet — use seeded bots 1 and 2"
        >
          + Create New Bot
        </button>
      </div>

      {error && (
        <div className="mb-4 p-3 rounded border border-red-500/40 text-red-300 text-sm">{error}</div>
      )}

      <div className="grid grid-cols-1 gap-4">
        {bots.length === 0 && !error && (
          <p className="text-gray-500 text-sm">No bots in database — restart backend to run migrations/seed.</p>
        )}
        {bots.map((bot) => (
          <Link
            key={bot.bot_id}
            to={`/bots/${bot.bot_id}`}
            className="bg-panel border border-border p-4 rounded-custom hover:border-primary transition-colors flex justify-between items-center group"
          >
            <div>
              <div className="flex items-center gap-3 mb-1">
                <h3 className="text-lg font-bold text-white">{bot.name}</h3>
                <span
                  className={`text-[10px] font-bold tracking-widest uppercase px-2 py-0.5 rounded ${
                    bot.status === 'running'
                      ? 'bg-green-500/20 text-green-400'
                      : bot.status === 'paused'
                        ? 'bg-amber-500/20 text-amber-400'
                        : 'bg-gray-500/20 text-gray-400'
                  }`}
                >
                  {bot.status}
                </span>
              </div>
              <p className="text-sm text-gray-400">
                Strategy: {bot.strategy} | Pair: {bot.symbol}
              </p>
            </div>
            <div className="text-right text-gray-500 text-xs uppercase">Manage →</div>
          </Link>
        ))}
      </div>
    </main>
  );
}
