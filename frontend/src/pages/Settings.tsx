import { useCallback, useEffect, useState } from 'react';
import { API_BASE } from '../config';
import { useRealtimeStore, type TradingSettings } from '../stores/realtimeStore';

export default function Settings() {
  const settings = useRealtimeStore((state) => state.tradingSettings) as TradingSettings | null;
  const loadTradingSettings = useRealtimeStore((state) => state.loadTradingSettings);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [liveModalOpen, setLiveModalOpen] = useState(false);
  const [confirmInput, setConfirmInput] = useState('');
  const [actionError, setActionError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const res = await fetch(`${API_BASE}/api/settings/trading`);
      if (!res.ok) throw new Error('Failed to load settings');
      const data = await res.json();
      useRealtimeStore.setState({
        tradingSettings: data,
        settingsLoaded: true,
        apiOk: true,
      });
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : 'Load failed');
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const useTestnet = settings?.execution_mode === 'testnet';

  const applyTestnet = async () => {
    if (!settings) return;
    setSaving(true);
    setActionError(null);
    try {
      const res = await fetch(`${API_BASE}/api/settings/trading`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ execution_mode: 'testnet', confirmation_phrase: null }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not switch to testnet');
      await loadTradingSettings();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Request failed');
    } finally {
      setSaving(false);
    }
  };

  const applyLive = async () => {
    if (!settings) return;
    setSaving(true);
    setActionError(null);
    try {
      const res = await fetch(`${API_BASE}/api/settings/trading`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          execution_mode: 'live',
          confirmation_phrase: confirmInput.trim(),
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not enable live trading');
      setLiveModalOpen(false);
      setConfirmInput('');
      await loadTradingSettings();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Request failed');
    } finally {
      setSaving(false);
    }
  };

  const setHalt = async (halted: boolean) => {
    if (halted && !window.confirm('Halt all bots now? Running bots will stop placing orders.')) return;
    setSaving(true);
    setActionError(null);
    try {
      const res = await fetch(`${API_BASE}/api/settings/trading/halt`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ halted }),
      });
      if (!res.ok) throw new Error('Could not update halt state');
      await loadTradingSettings();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Request failed');
    } finally {
      setSaving(false);
    }
  };

  return (
    <main className="flex-1 flex overflow-hidden p-6">
      <div className="w-full max-w-3xl">
        <h1 className="text-2xl font-bold text-white mb-6">Settings</h1>

        {loadError && (
          <div className="mb-6 p-4 rounded-custom border border-red-500/40 bg-red-500/10 text-red-300 text-sm">
            {loadError}
          </div>
        )}
        {actionError && (
          <div className="mb-6 p-4 rounded-custom border border-amber-500/40 bg-amber-500/10 text-amber-200 text-sm">
            {actionError}
          </div>
        )}

        <div className="space-y-6">
          <div className="bg-panel border border-border rounded-custom p-6">
            <h3 className="text-lg font-bold text-white mb-4">Binance API Keys</h3>
            <p className="text-sm text-gray-400 mb-4">
              Keys are read from your server <code className="bg-black/40 px-1 rounded">.env</code> files — not stored in
              the browser.
            </p>
            <div className="space-y-4">
              <div>
                <label className="block text-xs text-gray-500 mb-1">API Key</label>
                <input
                  type="password"
                  value="••••••••••••••"
                  readOnly
                  className="w-full bg-surface border border-border rounded px-3 py-2 text-white text-sm focus:outline-none"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">Secret Key</label>
                <input
                  type="password"
                  value="••••••••••••••"
                  readOnly
                  className="w-full bg-surface border border-border rounded px-3 py-2 text-white text-sm focus:outline-none"
                />
              </div>
            </div>
          </div>

          <div className="bg-panel border border-border rounded-custom p-6">
            <h3 className="text-lg font-bold text-white mb-4">Trading Preferences</h3>
            <div className="flex items-center justify-between py-2 border-b border-border">
              <div>
                <div className="font-bold text-white text-sm">Use Binance Spot Testnet (recommended)</div>
                <div className="text-xs text-gray-500">
                  Orders and balances use testnet (virtual funds). Turn off only when you intend to trade on mainnet.
                </div>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={useTestnet}
                disabled={!settings || saving}
                onClick={() => {
                  if (!settings) return;
                  if (useTestnet) {
                    setConfirmInput('');
                    setLiveModalOpen(true);
                  } else {
                    applyTestnet();
                  }
                }}
                className={`relative w-12 h-7 rounded-full transition-colors shrink-0 ${
                  useTestnet ? 'bg-primary' : 'bg-gray-600'
                } ${!settings || saving ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}`}
              >
                <span
                  className={`absolute top-1 w-5 h-5 bg-white rounded-full transition-all ${
                    useTestnet ? 'left-6' : 'left-1'
                  }`}
                />
              </button>
            </div>

            {settings && (
              <p className="text-xs text-gray-500 mt-3">
                Current mode:{' '}
                <span className={settings.execution_mode === 'live' ? 'text-red-400' : 'text-blue-400'}>
                  {settings.execution_mode === 'live' ? 'Mainnet (real funds)' : 'Testnet (simulated exchange funds)'}
                </span>
              </p>
            )}

            <div className="flex items-center justify-between py-2 mt-4">
              <div>
                <div className="font-bold text-white text-sm">Global killswitch</div>
                <div className="text-xs text-gray-500">When on, no bot may enter the running state.</div>
              </div>
              <div className="flex items-center gap-3">
                {settings?.global_trading_halted ? (
                  <button
                    type="button"
                    disabled={saving}
                    onClick={() => setHalt(false)}
                    className="px-4 py-1.5 bg-emerald-600/20 text-emerald-400 border border-emerald-900/50 rounded text-sm font-bold hover:bg-emerald-600 hover:text-white transition-colors"
                  >
                    RESUME
                  </button>
                ) : (
                  <button
                    type="button"
                    disabled={saving}
                    onClick={() => setHalt(true)}
                    className="px-4 py-1.5 bg-red-600/20 text-red-500 border border-red-900/50 rounded text-sm font-bold hover:bg-red-600 hover:text-white transition-colors"
                  >
                    HALT ALL
                  </button>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>

      {liveModalOpen && settings && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
          <div className="bg-panel border border-border rounded-custom max-w-md w-full p-6 shadow-xl">
            <h3 className="text-lg font-bold text-white mb-2">Enable mainnet trading?</h3>
            <p className="text-sm text-gray-400 mb-4">
              This routes the backend to <span className="text-red-400">Binance mainnet</span> for wallet queries and
              bot orders. Use testnet keys only on testnet — use separate API keys for mainnet.
            </p>
            <p className="text-xs text-gray-500 mb-2">
              Type exactly: <code className="text-primary">{settings.live_confirmation_phrase}</code>
            </p>
            <input
              type="text"
              value={confirmInput}
              onChange={(e) => setConfirmInput(e.target.value)}
              className="w-full bg-surface border border-border rounded px-3 py-2 text-white text-sm mb-4 focus:outline-none focus:border-primary"
              placeholder="Confirmation phrase"
              autoComplete="off"
            />
            <div className="flex justify-end gap-2">
              <button
                type="button"
                className="px-4 py-2 text-sm text-gray-300 hover:text-white"
                onClick={() => {
                  setLiveModalOpen(false);
                  setConfirmInput('');
                }}
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={saving}
                className="px-4 py-2 rounded-custom text-sm font-bold bg-red-600 text-white hover:bg-red-700 disabled:opacity-50"
                onClick={() => applyLive()}
              >
                Enable live
              </button>
            </div>
          </div>
        </div>
      )}
    </main>
  );
}
