import { useCallback, useEffect, useState } from 'react';
import { API_BASE } from '../config';
import { useRealtimeStore, type TradingSettings } from '../stores/realtimeStore';
import {
  cloneRiskSettings,
  validateRiskSettings,
  type DrawdownAction,
  type RiskSettings,
} from '../riskSettings';

type RiskDraftSetter = (next: RiskSettings) => void;

function numberValue(value: string): number | null {
  if (value.trim() === '') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function RiskFields({ value, onChange }: { value: RiskSettings; onChange: RiskDraftSetter }) {
  const set = <K extends keyof RiskSettings>(key: K, next: RiskSettings[K]) => {
    onChange({ ...value, [key]: next });
  };
  const inputClass = 'w-full bg-surface border border-border rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-primary';
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 sm:grid-cols-4 gap-3">
        <label className="block">
          <span className="block text-xs text-gray-500 mb-1">Base risk %</span>
          <input
            type="number"
            min="0.1"
            step="0.1"
            value={value.base_risk_pct}
            onChange={(e) => set('base_risk_pct', Number(e.target.value))}
            className={inputClass}
          />
        </label>
        <label className="block">
          <span className="block text-xs text-gray-500 mb-1">Daily loss %</span>
          <input
            type="number"
            min="0.1"
            step="0.1"
            value={value.daily_loss_limit_pct}
            onChange={(e) => set('daily_loss_limit_pct', Number(e.target.value))}
            className={inputClass}
          />
        </label>
        <label className="block">
          <span className="block text-xs text-gray-500 mb-1">Max drawdown %</span>
          <input
            type="number"
            min="0.1"
            step="0.1"
            value={value.max_drawdown_pct}
            onChange={(e) => set('max_drawdown_pct', Number(e.target.value))}
            className={inputClass}
          />
        </label>
        <label className="block">
          <span className="block text-xs text-gray-500 mb-1">Loss streak</span>
          <input
            type="number"
            min="1"
            step="1"
            value={value.consecutive_loss_limit}
            onChange={(e) => set('consecutive_loss_limit', Number.parseInt(e.target.value, 10))}
            className={inputClass}
          />
        </label>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-4 gap-3">
        {value.dynamic_tiers.map((tier, idx) => (
          <label key={`${tier.min_score ?? 'min'}-${tier.max_score ?? 'max'}`} className="block">
            <span className="block text-xs text-gray-500 mb-1">
              {idx === 0 ? '< 0.40' : idx === 1 ? '0.40 - 0.70' : idx === 2 ? '0.70 - 0.85' : '> 0.85'} multiplier
            </span>
            <input
              type="number"
              min="0.1"
              step="0.05"
              value={tier.multiplier}
              onChange={(e) => {
                const dynamic_tiers = value.dynamic_tiers.map((candidate, tierIdx) =>
                  tierIdx === idx ? { ...candidate, multiplier: Number(e.target.value) } : candidate,
                );
                onChange({ ...value, dynamic_tiers });
              }}
              className={inputClass}
            />
          </label>
        ))}
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <label className="block">
          <span className="block text-xs text-gray-500 mb-1">Drawdown action</span>
          <select
            value={value.drawdown_action}
            onChange={(e) => set('drawdown_action', e.target.value as DrawdownAction)}
            className={inputClass}
          >
            <option value="reduce">Reduce position size</option>
            <option value="pause">Pause bot</option>
          </select>
        </label>
        <label className="block">
          <span className="block text-xs text-gray-500 mb-1">Drawdown reduce factor</span>
          <input
            type="number"
            min="0.05"
            max="1"
            step="0.05"
            value={value.drawdown_reduce_factor}
            onChange={(e) => set('drawdown_reduce_factor', Number(e.target.value))}
            className={inputClass}
          />
        </label>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs text-gray-400">
        {[
          ['enable_dynamic_sizing', 'Dynamic sizing'],
          ['enable_daily_loss_limit', 'Daily loss limit'],
          ['enable_drawdown_protection', 'Drawdown protection'],
          ['enable_consecutive_loss', 'Consecutive loss breaker'],
          ['enable_volatility_pause', 'Volatility pause'],
        ].map(([key, label]) => (
          <label key={key} className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={Boolean(value[key as keyof RiskSettings])}
              onChange={(e) => set(key as keyof RiskSettings, e.target.checked as never)}
            />
            {label}
          </label>
        ))}
        <label className="block">
          <span className="block text-xs text-gray-500 mb-1">Volatility threshold %</span>
          <input
            type="number"
            min="0.1"
            step="0.1"
            value={value.volatility_threshold ?? ''}
            onChange={(e) => set('volatility_threshold', numberValue(e.target.value))}
            className={inputClass}
            placeholder="optional"
          />
        </label>
      </div>
    </div>
  );
}

export default function Settings() {
  const settings = useRealtimeStore((state) => state.tradingSettings) as TradingSettings | null;
  const bots = useRealtimeStore((state) => state.bots);
  const loadTradingSettings = useRealtimeStore((state) => state.loadTradingSettings);
  const loadBots = useRealtimeStore((state) => state.loadBots);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [liveModalOpen, setLiveModalOpen] = useState(false);
  const [confirmInput, setConfirmInput] = useState('');
  const [actionError, setActionError] = useState<string | null>(null);
  const [globalRisk, setGlobalRisk] = useState<RiskSettings | null>(null);
  const [botRisk, setBotRisk] = useState<RiskSettings | null>(null);
  const [riskSource, setRiskSource] = useState<string | null>(null);
  const [selectedBotId, setSelectedBotId] = useState('');
  const [riskSaving, setRiskSaving] = useState(false);
  const [riskError, setRiskError] = useState<string | null>(null);

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
      const riskRes = await fetch(`${API_BASE}/api/settings/risk-defaults`);
      if (!riskRes.ok) throw new Error('Failed to load risk defaults');
      const riskData = await riskRes.json();
      setGlobalRisk(riskData.risk_settings as RiskSettings);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : 'Load failed');
    }
  }, []);

  useEffect(() => {
    load();
    void loadBots();
  }, [load, loadBots]);

  useEffect(() => {
    if (!selectedBotId && bots.length > 0) setSelectedBotId(bots[0].bot_id);
  }, [bots, selectedBotId]);

  useEffect(() => {
    if (!selectedBotId) {
      setBotRisk(null);
      setRiskSource(null);
      return;
    }
    let cancelled = false;
    setRiskError(null);
    fetch(`${API_BASE}/api/bots/${selectedBotId}/risk-settings`)
      .then(async (res) => {
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Failed to load bot risk');
        if (!cancelled) {
          setBotRisk(data.risk_settings as RiskSettings);
          setRiskSource(typeof data.source === 'string' ? data.source : null);
        }
      })
      .catch((e) => {
        if (!cancelled) setRiskError(e instanceof Error ? e.message : 'Failed to load bot risk');
      });
    return () => {
      cancelled = true;
    };
  }, [selectedBotId]);

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

  const saveGlobalRisk = async () => {
    if (!globalRisk) return;
    const validation = validateRiskSettings(globalRisk);
    if (validation) {
      setRiskError(validation);
      return;
    }
    setRiskSaving(true);
    setRiskError(null);
    try {
      const res = await fetch(`${API_BASE}/api/settings/risk-defaults`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(globalRisk),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not save global risk');
      setGlobalRisk(data.risk_settings as RiskSettings);
    } catch (e) {
      setRiskError(e instanceof Error ? e.message : 'Risk save failed');
    } finally {
      setRiskSaving(false);
    }
  };

  const saveBotRisk = async () => {
    if (!botRisk || !selectedBotId) return;
    const validation = validateRiskSettings(botRisk);
    if (validation) {
      setRiskError(validation);
      return;
    }
    setRiskSaving(true);
    setRiskError(null);
    try {
      const res = await fetch(`${API_BASE}/api/bots/${selectedBotId}/risk-settings`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(botRisk),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not save bot risk');
      setBotRisk(data.risk_settings as RiskSettings);
      setRiskSource('bot');
    } catch (e) {
      setRiskError(e instanceof Error ? e.message : 'Risk save failed');
    } finally {
      setRiskSaving(false);
    }
  };

  const resetBotRisk = async (source: 'global' | 'template') => {
    if (!selectedBotId) return;
    setRiskSaving(true);
    setRiskError(null);
    try {
      const res = await fetch(`${API_BASE}/api/bots/${selectedBotId}/risk-settings/reset`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not reset bot risk');
      setBotRisk(data.risk_settings as RiskSettings);
      setRiskSource(source);
    } catch (e) {
      setRiskError(e instanceof Error ? e.message : 'Risk reset failed');
    } finally {
      setRiskSaving(false);
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

          <div className="bg-panel border border-border rounded-custom p-6">
            <div className="flex items-start justify-between gap-4 mb-4">
              <div>
                <h3 className="text-lg font-bold text-white">Risk Management &amp; Position Sizing</h3>
                <p className="text-xs text-gray-500 mt-1">
                  Global defaults seed new bots; per-bot profiles control live sizing and protections.
                </p>
              </div>
              {riskSource && (
                <span className="text-[10px] uppercase tracking-widest text-primary/80 border border-primary/20 bg-primary/10 px-2 py-1 rounded">
                  Bot source: {riskSource}
                </span>
              )}
            </div>

            {riskError && (
              <div className="mb-4 p-3 rounded-custom border border-amber-500/40 bg-amber-500/10 text-amber-200 text-xs">
                {riskError}
              </div>
            )}

            <div className="space-y-6">
              <section className="border border-border/70 bg-surface/40 rounded-custom p-4">
                <div className="flex items-center justify-between gap-3 mb-4">
                  <div>
                    <h4 className="text-sm font-bold text-white">Global Default Risk Settings</h4>
                    <p className="text-xs text-gray-500">
                      Used when a bot is created without a template-specific profile.
                    </p>
                  </div>
                  <button
                    type="button"
                    disabled={!globalRisk || riskSaving}
                    onClick={() => void saveGlobalRisk()}
                    className="px-3 py-2 rounded-custom bg-primary text-black text-xs font-bold uppercase tracking-widest disabled:opacity-50"
                  >
                    Save defaults
                  </button>
                </div>
                {globalRisk ? (
                  <RiskFields
                    value={globalRisk}
                    onChange={(next) => setGlobalRisk(cloneRiskSettings(next))}
                  />
                ) : (
                  <p className="text-sm text-gray-500">Loading defaults…</p>
                )}
              </section>

              <section className="border border-border/70 bg-surface/40 rounded-custom p-4">
                <div className="flex flex-col sm:flex-row sm:items-end justify-between gap-3 mb-4">
                  <div className="flex-1">
                    <h4 className="text-sm font-bold text-white">Per-Bot Risk Profile</h4>
                    <p className="text-xs text-gray-500 mb-2">
                      These settings apply only to the selected bot.
                    </p>
                    <select
                      value={selectedBotId}
                      onChange={(e) => setSelectedBotId(e.target.value)}
                      className="w-full bg-surface border border-border rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-primary"
                    >
                      {bots.length === 0 ? (
                        <option value="">No bots available</option>
                      ) : (
                        bots.map((bot) => (
                          <option key={bot.bot_id} value={bot.bot_id}>
                            {bot.name} · {bot.symbol}
                          </option>
                        ))
                      )}
                    </select>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <button
                      type="button"
                      disabled={!botRisk || !selectedBotId || riskSaving}
                      onClick={() => void resetBotRisk('global')}
                      className="px-3 py-2 rounded-custom border border-border text-xs text-gray-300 hover:text-white disabled:opacity-50"
                    >
                      Reset to Global Defaults
                    </button>
                    <button
                      type="button"
                      disabled={!botRisk || !selectedBotId || riskSaving}
                      onClick={() => void resetBotRisk('template')}
                      className="px-3 py-2 rounded-custom border border-primary/40 text-xs text-primary hover:bg-primary/10 disabled:opacity-50"
                    >
                      Reset to Template Defaults
                    </button>
                    <button
                      type="button"
                      disabled={!botRisk || !selectedBotId || riskSaving}
                      onClick={() => void saveBotRisk()}
                      className="px-3 py-2 rounded-custom bg-primary text-black text-xs font-bold uppercase tracking-widest disabled:opacity-50"
                    >
                      Save bot risk
                    </button>
                  </div>
                </div>
                {botRisk ? (
                  <RiskFields value={botRisk} onChange={(next) => setBotRisk(cloneRiskSettings(next))} />
                ) : (
                  <p className="text-sm text-gray-500">Select a bot to edit its risk profile.</p>
                )}
              </section>
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
