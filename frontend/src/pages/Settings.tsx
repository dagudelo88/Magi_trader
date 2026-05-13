import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { API_BASE } from '../config';
import { useRealtimeStore, type TradingSettings } from '../stores/realtimeStore';
import {
  cloneRiskSettings,
  validateRiskSettings,
  type DrawdownAction,
  type RiskSettings,
} from '../riskSettings';

type RiskDraftSetter = (next: RiskSettings) => void;

const RISK_INPUT_CLASS =
  'w-full rounded border border-border bg-surface px-3 py-2 text-sm text-white focus:border-primary focus:outline-none';

function numberValue(value: string): number | null {
  if (value.trim() === '') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function SubsectionLabel({ children }: { children: ReactNode }) {
  return (
    <p className="font-label mb-3 text-[10px] font-bold uppercase tracking-widest text-gray-500">{children}</p>
  );
}

function RiskFields({ value, onChange }: { value: RiskSettings; onChange: RiskDraftSetter }) {
  const set = <K extends keyof RiskSettings>(key: K, next: RiskSettings[K]) => {
    onChange({ ...value, [key]: next });
  };
  return (
    <div className="space-y-6">
      <div>
        <SubsectionLabel>Core limits</SubsectionLabel>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <label className="block">
            <span className="mb-1 block text-xs text-gray-500">Base risk %</span>
            <input
              type="number"
              min="0.1"
              step="0.1"
              value={value.base_risk_pct}
              onChange={(e) => set('base_risk_pct', Number(e.target.value))}
              className={RISK_INPUT_CLASS}
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-gray-500">Daily loss %</span>
            <input
              type="number"
              min="0.1"
              step="0.1"
              value={value.daily_loss_limit_pct}
              onChange={(e) => set('daily_loss_limit_pct', Number(e.target.value))}
              className={RISK_INPUT_CLASS}
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-gray-500">Max drawdown %</span>
            <input
              type="number"
              min="0.1"
              step="0.1"
              value={value.max_drawdown_pct}
              onChange={(e) => set('max_drawdown_pct', Number(e.target.value))}
              className={RISK_INPUT_CLASS}
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-gray-500">Loss streak limit</span>
            <input
              type="number"
              min="1"
              step="1"
              value={value.consecutive_loss_limit}
              onChange={(e) => set('consecutive_loss_limit', Number.parseInt(e.target.value, 10))}
              className={RISK_INPUT_CLASS}
            />
          </label>
        </div>
      </div>

      <div>
        <SubsectionLabel>Dynamic sizing (by consensus score)</SubsectionLabel>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {value.dynamic_tiers.map((tier, idx) => (
            <label
              key={`${tier.min_score ?? 'min'}-${tier.max_score ?? 'max'}-${idx}`}
              className="block"
            >
              <span className="mb-1 block text-xs text-gray-500">
                {idx === 0 ? '< 0.40' : idx === 1 ? '0.40 – 0.70' : idx === 2 ? '0.70 – 0.85' : '> 0.85'} multiplier
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
                className={RISK_INPUT_CLASS}
              />
            </label>
          ))}
        </div>
      </div>

      <div>
        <SubsectionLabel>Drawdown response</SubsectionLabel>
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <label className="block">
            <span className="mb-1 block text-xs text-gray-500">Action</span>
            <select
              value={value.drawdown_action}
              onChange={(e) => set('drawdown_action', e.target.value as DrawdownAction)}
              className={RISK_INPUT_CLASS}
            >
              <option value="reduce">Reduce position size</option>
              <option value="pause">Pause bot</option>
            </select>
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-gray-500">Reduce factor</span>
            <input
              type="number"
              min="0.05"
              max="1"
              step="0.05"
              value={value.drawdown_reduce_factor}
              onChange={(e) => set('drawdown_reduce_factor', Number(e.target.value))}
              className={RISK_INPUT_CLASS}
            />
          </label>
        </div>
      </div>

      <div>
        <SubsectionLabel>Protection toggles</SubsectionLabel>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {(
            [
              ['enable_dynamic_sizing', 'Dynamic sizing'],
              ['enable_daily_loss_limit', 'Daily loss limit'],
              ['enable_drawdown_protection', 'Drawdown protection'],
              ['enable_consecutive_loss', 'Consecutive loss breaker'],
            ] as const
          ).map(([key, label]) => (
            <label
              key={key}
              className="flex cursor-pointer items-center gap-3 rounded border border-border/60 bg-surface/40 px-3 py-2.5 text-xs text-gray-300"
            >
              <input
                type="checkbox"
                className="h-4 w-4 shrink-0 rounded border-border text-primary focus:ring-primary"
                checked={Boolean(value[key])}
                onChange={(e) => set(key, e.target.checked as never)}
              />
              {label}
            </label>
          ))}
        </div>
      </div>

      <div>
        <SubsectionLabel>Volatility filter</SubsectionLabel>
        <div className="flex flex-col gap-3 rounded border border-border/60 bg-surface/40 p-4 sm:flex-row sm:items-end sm:gap-6">
          <label className="flex cursor-pointer items-center gap-3 text-xs text-gray-300 sm:shrink-0">
            <input
              type="checkbox"
              className="h-4 w-4 shrink-0 rounded border-border text-primary focus:ring-primary"
              checked={value.enable_volatility_pause}
              onChange={(e) => set('enable_volatility_pause', e.target.checked)}
            />
            Pause on high volatility
          </label>
          <label className="min-w-0 flex-1">
            <span className="mb-1 block text-xs text-gray-500">Threshold % (optional)</span>
            <input
              type="number"
              min="0.1"
              step="0.1"
              value={value.volatility_threshold ?? ''}
              onChange={(e) => set('volatility_threshold', numberValue(e.target.value))}
              className={RISK_INPUT_CLASS}
              placeholder="e.g. 2.5"
            />
          </label>
        </div>
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
  const [actionError, setActionError] = useState<string | null>(null);
  const [globalRisk, setGlobalRisk] = useState<RiskSettings | null>(null);
  const [botRisk, setBotRisk] = useState<RiskSettings | null>(null);
  const [riskSource, setRiskSource] = useState<string | null>(null);
  const [selectedBotId, setSelectedBotId] = useState('');
  const [riskSaving, setRiskSaving] = useState(false);
  const [riskError, setRiskError] = useState<string | null>(null);
  const [riskTab, setRiskTab] = useState<'global' | 'perBot'>('global');

  const riskTabClass = (tab: 'global' | 'perBot') =>
    `rounded-t border px-3 py-2 text-[10px] font-bold uppercase tracking-widest transition-colors ${
      riskTab === tab
        ? 'border-primary/50 bg-primary/15 text-primary'
        : 'border-border/50 bg-surface/30 text-gray-500 hover:text-gray-300'
    }`;

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
    <>
      <main className="flex min-h-0 flex-1 flex-col overflow-hidden">
        <div className="mx-auto min-h-0 w-full max-w-4xl flex-1 overflow-y-auto px-4 py-6 sm:px-6 lg:py-8">
          <header className="mb-8 border-b border-border/60 pb-6">
            <h1 className="font-headline text-2xl font-black uppercase italic tracking-tight text-white">
              Configuration
            </h1>
            <p className="mt-2 max-w-2xl text-sm text-gray-500">
              Binance credentials from your server <code className="font-mono text-xs text-gray-600">.env</code>, global
              halt, and risk defaults. Live vs testnet routing is set <span className="text-gray-400">per bot</span>, not
              here.
            </p>
          </header>

          {loadError && (
            <div className="mb-6 rounded border border-red-500/40 bg-red-500/10 p-4 text-sm text-red-300">
              {loadError}
            </div>
          )}
          {actionError && (
            <div className="mb-6 rounded border border-amber-500/40 bg-amber-500/10 p-4 text-sm text-amber-200">
              {actionError}
            </div>
          )}

          <div className="flex flex-col gap-6">
            <section className="border border-border bg-panel p-5 sm:p-6">
              <h2 className="mb-1 text-lg font-bold text-white">Binance API keys</h2>
              <p className="mb-6 text-sm text-gray-400">
                Credentials come from your server <code className="rounded bg-black/40 px-1.5 py-0.5 font-mono text-xs">.env</code>{' '}
                only — nothing here is stored in the browser.
              </p>

              <div className="space-y-8">
                <div>
                  <h3 className="mb-1 text-sm font-bold text-white">Spot testnet</h3>
                  <p className="mb-4 font-mono text-[10px] uppercase tracking-wide text-gray-500">
                    BINANCE_TESTNET_API_KEY · BINANCE_TESTNET_API_SECRET
                  </p>
                  <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                    <label className="block">
                      <span className="mb-1 block text-xs text-gray-500">Testnet API key</span>
                      <input
                        type="password"
                        value="••••••••••••••"
                        readOnly
                        autoComplete="off"
                        aria-label="BINANCE_TESTNET_API_KEY (masked)"
                        className="w-full rounded border border-border bg-surface px-3 py-2 font-mono text-sm text-white focus:outline-none"
                      />
                    </label>
                    <label className="block">
                      <span className="mb-1 block text-xs text-gray-500">Testnet secret</span>
                      <input
                        type="password"
                        value="••••••••••••••"
                        readOnly
                        autoComplete="off"
                        aria-label="BINANCE_TESTNET_API_SECRET (masked)"
                        className="w-full rounded border border-border bg-surface px-3 py-2 font-mono text-sm text-white focus:outline-none"
                      />
                    </label>
                  </div>
                </div>

                <div className="border-t border-border/60 pt-8">
                  <h3 className="mb-1 text-sm font-bold text-white">Spot mainnet (live)</h3>
                  <p className="mb-4 font-mono text-[10px] uppercase tracking-wide text-gray-500">
                    BINANCE_API_KEY · BINANCE_API_SECRET
                  </p>
                  <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                    <label className="block">
                      <span className="mb-1 block text-xs text-gray-500">Mainnet API key</span>
                      <input
                        type="password"
                        value="••••••••••••••"
                        readOnly
                        autoComplete="off"
                        aria-label="BINANCE_API_KEY (masked)"
                        className="w-full rounded border border-border bg-surface px-3 py-2 font-mono text-sm text-white focus:outline-none"
                      />
                    </label>
                    <label className="block">
                      <span className="mb-1 block text-xs text-gray-500">Mainnet secret</span>
                      <input
                        type="password"
                        value="••••••••••••••"
                        readOnly
                        autoComplete="off"
                        aria-label="BINANCE_API_SECRET (masked)"
                        className="w-full rounded border border-border bg-surface px-3 py-2 font-mono text-sm text-white focus:outline-none"
                      />
                    </label>
                  </div>
                </div>
              </div>
            </section>

            <section className="border border-border bg-panel p-5 sm:p-6">
              <h2 className="mb-1 text-lg font-bold text-white">Global killswitch</h2>
              <p className="mb-5 text-sm text-gray-400">
                When halted, no bot may enter the running state. This is independent of which bots are on testnet or
                promoted to live.
              </p>
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
                <p className="min-w-0 flex-1 text-xs text-gray-500">
                  Status:{' '}
                  <span className={settings?.global_trading_halted ? 'font-semibold text-amber-400' : 'text-emerald-400'}>
                    {settings?.global_trading_halted ? 'HALTED — fleet stopped from starting' : 'Normal — bots may run'}
                  </span>
                </p>
                <div className="flex shrink-0 items-center gap-3">
                  {settings?.global_trading_halted ? (
                    <button
                      type="button"
                      disabled={saving || !settings}
                      onClick={() => setHalt(false)}
                      className="rounded border border-emerald-900/50 bg-emerald-600/20 px-4 py-1.5 text-sm font-bold text-emerald-400 transition-colors hover:bg-emerald-600 hover:text-white disabled:opacity-50"
                    >
                      RESUME
                    </button>
                  ) : (
                    <button
                      type="button"
                      disabled={saving || !settings}
                      onClick={() => setHalt(true)}
                      className="rounded border border-red-900/50 bg-red-600/20 px-4 py-1.5 text-sm font-bold text-red-500 transition-colors hover:bg-red-600 hover:text-white disabled:opacity-50"
                    >
                      HALT ALL
                    </button>
                  )}
                </div>
              </div>
            </section>

            <section className="border border-border bg-panel p-5 sm:p-6">
              <h2 className="text-lg font-bold text-white">Risk & position sizing</h2>
              <p className="mt-1 text-sm text-gray-500">
                Global defaults seed new bots; per-bot profiles control live sizing and protections.
              </p>

              {riskError && (
                <div className="mt-4 rounded border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-200">
                  {riskError}
                </div>
              )}

              <div className="mt-6 flex flex-wrap items-end gap-1 border-b border-border/60 pb-px">
                <button type="button" className={riskTabClass('global')} onClick={() => setRiskTab('global')}>
                  Global defaults
                </button>
                <button type="button" className={riskTabClass('perBot')} onClick={() => setRiskTab('perBot')}>
                  Per-bot profile
                </button>
                {riskTab === 'perBot' && riskSource && (
                  <span className="mb-1 ml-auto text-[10px] uppercase tracking-widest text-primary/80 sm:mb-2">
                    Source: <span className="text-primary">{riskSource}</span>
                  </span>
                )}
              </div>

              <div className="mt-5">
                {riskTab === 'global' && (
                  <div>
                    <div className="mb-4 flex flex-wrap items-center justify-end gap-2">
                      <button
                        type="button"
                        disabled={!globalRisk || riskSaving}
                        onClick={() => void saveGlobalRisk()}
                        className="rounded bg-primary px-3 py-2 text-xs font-bold uppercase tracking-widest text-black disabled:opacity-50"
                      >
                        Save defaults
                      </button>
                    </div>
                    {globalRisk ? (
                      <RiskFields value={globalRisk} onChange={(next) => setGlobalRisk(cloneRiskSettings(next))} />
                    ) : (
                      <p className="text-sm text-gray-500">Loading defaults…</p>
                    )}
                  </div>
                )}

                {riskTab === 'perBot' && (
                  <div className="space-y-5">
                    <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
                      <label className="min-w-0 flex-1 lg:max-w-md">
                        <span className="mb-1 block text-xs text-gray-500">Bot</span>
                        <select
                          value={selectedBotId}
                          onChange={(e) => setSelectedBotId(e.target.value)}
                          className={`${RISK_INPUT_CLASS} font-mono text-xs sm:text-sm`}
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
                      </label>
                      <div className="flex flex-wrap gap-2">
                        <button
                          type="button"
                          disabled={!botRisk || !selectedBotId || riskSaving}
                          onClick={() => void resetBotRisk('global')}
                          className="rounded border border-border px-3 py-2 text-xs text-gray-300 hover:text-white disabled:opacity-50"
                        >
                          Reset to global
                        </button>
                        <button
                          type="button"
                          disabled={!botRisk || !selectedBotId || riskSaving}
                          onClick={() => void resetBotRisk('template')}
                          className="rounded border border-primary/40 px-3 py-2 text-xs text-primary hover:bg-primary/10 disabled:opacity-50"
                        >
                          Reset to template
                        </button>
                        <button
                          type="button"
                          disabled={!botRisk || !selectedBotId || riskSaving}
                          onClick={() => void saveBotRisk()}
                          className="rounded bg-primary px-3 py-2 text-xs font-bold uppercase tracking-widest text-black disabled:opacity-50"
                        >
                          Save bot risk
                        </button>
                      </div>
                    </div>
                    {bots.length === 0 ? (
                      <p className="text-sm text-gray-500">Create a bot from the Bots page to edit per-bot risk.</p>
                    ) : botRisk ? (
                      <RiskFields value={botRisk} onChange={(next) => setBotRisk(cloneRiskSettings(next))} />
                    ) : (
                      <p className="text-sm text-gray-500">Select a bot to load its risk profile.</p>
                    )}
                  </div>
                )}
              </div>
            </section>
          </div>
        </div>
      </main>

    </>
  );
}
