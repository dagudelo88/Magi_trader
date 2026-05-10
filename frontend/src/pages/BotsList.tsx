import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { API_BASE } from '../config';
import { BOT_TEMPLATES, SUPPORTED_SYMBOLS, type BotTemplate } from '../botTemplates';
import { useRealtimeStore, type BotRow } from '../stores/realtimeStore';
import {
  effectiveRiskPct,
  templateRiskDefaults,
  validateRiskSettings,
  type RiskSettings,
} from '../riskSettings';

const STATUS_BADGE: Record<string, string> = {
  running: 'bg-green-500/20 text-green-400 border border-green-500/30',
  paused: 'bg-amber-500/20 text-amber-400 border border-amber-500/30',
  stopped: 'bg-gray-500/20 text-gray-400 border border-gray-500/30',
};

function statusBadge(s: string) {
  return STATUS_BADGE[s] ?? STATUS_BADGE.stopped;
}

// ── Create modal state ──────────────────────────────────────────────────────

type CreateStep = 'pick-template' | 'configure';

/** Strategy default params fetched from GET /api/strategies. */
type StrategyDefaults = Record<string, unknown>;

interface CreateConfig {
  template: BotTemplate;
  /** Default params fetched from the backend for this template's strategy. */
  params: StrategyDefaults;
  name: string;
  symbol: string;
  budget: string;
  /** Active voter list for ensemble templates (ignored for non-ensemble). */
  voters: string[];
  riskSettings: RiskSettings;
}

/** All strategies that can act as voters (leaf strategies only — no ensembles). */
const ALL_VOTERS: { id: string; label: string }[] = [
  { id: 'macd_rsi',     label: 'MACD + RSI' },
  { id: 'stochastic',   label: 'Stochastic' },
  { id: 'cci',          label: 'CCI' },
  { id: 'bb_breakout',  label: 'BB Breakout' },
  { id: 'rsi_cross',    label: 'RSI Cross' },
  { id: 'supertrend',   label: 'Supertrend' },
  { id: 'dual_ema',     label: 'Dual EMA' },
  { id: 'bb_rsi',       label: 'BB + RSI' },
  { id: 'ema_ribbon',   label: 'EMA Ribbon' },
  { id: 'parabolic_sar',label: 'Parabolic SAR' },
  { id: 'donchian',     label: 'Donchian' },
  { id: 'tema',         label: 'TEMA' },
  { id: 'sma_cross',    label: 'SMA Cross' },
  { id: 'obv_price',    label: 'OBV + Price' },
  { id: 'price_breakout', label: 'Price Breakout' },
];

// ── Edit modal state ────────────────────────────────────────────────────────

interface EditForm {
  name: string;
  symbol: string;
}

// ── Component ───────────────────────────────────────────────────────────────

export default function BotsList() {
  const bots = useRealtimeStore((state) => state.bots);
  const executionMode = useRealtimeStore((state) => state.tradingSettings?.execution_mode ?? null);
  const loadBots = useRealtimeStore((state) => state.loadBots);
  const removeBot = useRealtimeStore((state) => state.removeBot);
  const [error, setError] = useState<string | null>(null);
  const [busyBotId, setBusyBotId] = useState<string | null>(null);
  const [bulkBusy, setBulkBusy] = useState<'resume' | 'pause' | null>(null);

  // Strategy defaults fetched from backend — keyed by strategy name.
  // This is the single source of truth for all strategy params.
  const [strategyDefaults, setStrategyDefaults] = useState<Record<string, StrategyDefaults>>({});

  // create modal
  const [createStep, setCreateStep] = useState<CreateStep | null>(null);
  const [createConfig, setCreateConfig] = useState<CreateConfig | null>(null);
  const [createBusy, setCreateBusy] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const budgetRef = useRef<HTMLInputElement>(null);

  // edit modal
  const [editBotId, setEditBotId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState<EditForm>({ name: '', symbol: '' });
  const [editBusy, setEditBusy] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);

  const loadStrategies = async (signal?: AbortSignal) => {
    try {
      const strategiesRes = await fetch(`${API_BASE}/api/strategies`, { signal });
      if (strategiesRes.ok) {
        const s = await strategiesRes.json();
        const defaults: Record<string, StrategyDefaults> = {};
        for (const entry of (s.strategies ?? [])) {
          defaults[entry.name] = entry.default_params ?? {};
        }
        setStrategyDefaults(defaults);
      }
    } catch (e) {
      if (e instanceof Error && e.name === 'AbortError') return;
      setError(e instanceof Error ? e.message : 'Failed to load');
    }
  };

  useEffect(() => {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 10_000);
    void loadStrategies(ctrl.signal).then(() => clearTimeout(t));
    return () => {
      ctrl.abort();
      clearTimeout(t);
    };
  }, []);

  // Focus budget field when configure step opens
  useEffect(() => {
    if (createStep === 'configure') setTimeout(() => budgetRef.current?.focus(), 60);
  }, [createStep]);

  const openCreate = () => {
    setCreateStep('pick-template');
    setCreateConfig(null);
    setCreateError(null);
  };

  const closeCreate = () => {
    setCreateStep(null);
    setCreateConfig(null);
    setCreateError(null);
  };

  const pickTemplate = (tpl: BotTemplate) => {
    const params = strategyDefaults[tpl.strategy] ?? {};
    const defaultVoters = tpl.strategy.includes('ensemble')
      ? (params.voters as string[] | undefined ?? [])
      : [];
    setCreateConfig({
      template: tpl,
      params,
      name: '',
      symbol: tpl.defaultSymbol,
      budget: '',
      voters: defaultVoters,
      riskSettings: templateRiskDefaults(tpl.strategy),
    });
    setCreateError(null);
    setCreateStep('configure');
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!createConfig) return;
    setCreateError(null);

    const name = createConfig.name.trim() ||
      `${createConfig.template.name} · ${createConfig.symbol.replace('/', '_')}`;
    const budget = Number.parseFloat(createConfig.budget.trim());
    if (!Number.isFinite(budget) || budget <= 0) {
      setCreateError('Budget is required and must be a positive number.');
      budgetRef.current?.focus();
      return;
    }

    const isEnsemble = createConfig.template.strategy.includes('ensemble');
    if (isEnsemble && createConfig.voters.length < 2) {
      setCreateError('Select at least 2 voters for an ensemble bot.');
      return;
    }
    const riskError = validateRiskSettings(createConfig.riskSettings);
    if (riskError) {
      setCreateError(riskError);
      return;
    }

    setCreateBusy(true);
    try {
      // Send backend-fetched params so they stay in sync with strategy_templates.py.
      // Only override voters when the user has customised them.
      const strategyParams = isEnsemble
        ? { ...createConfig.params, voters: createConfig.voters }
        : createConfig.params;

      const res = await fetch(`${API_BASE}/api/bots`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name,
          symbol: createConfig.symbol,
          strategy: createConfig.template.strategy,
          initial_budget_quote: budget,
          strategy_params: strategyParams,
          risk_settings: createConfig.riskSettings,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Create failed');
      closeCreate();
      await loadBots();
    } catch (e) {
      setCreateError(e instanceof Error ? e.message : 'Create failed');
    } finally {
      setCreateBusy(false);
    }
  };

  const setStatus = async (botId: string, status: 'running' | 'stopped' | 'paused') => {
    setBusyBotId(botId);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/bots/${botId}/status`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Update failed');
      await loadBots();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Update failed');
    } finally {
      setBusyBotId(null);
    }
  };

  const resumeAll = async () => {
    const targets = bots.filter((b) => b.status === 'paused');
    if (targets.length === 0) return;
    setBulkBusy('resume');
    setError(null);
    try {
      await Promise.all(
        targets.map((b) =>
          fetch(`${API_BASE}/api/bots/${b.bot_id}/status`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: 'running' }),
          }),
        ),
      );
      await loadBots();
    } catch {
      setError('Failed to resume all bots');
    } finally {
      setBulkBusy(null);
    }
  };

  const pauseAll = async () => {
    const targets = bots.filter((b) => b.status === 'running');
    if (targets.length === 0) return;
    setBulkBusy('pause');
    setError(null);
    try {
      await Promise.all(
        targets.map((b) =>
          fetch(`${API_BASE}/api/bots/${b.bot_id}/status`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: 'paused' }),
          }),
        ),
      );
      await loadBots();
    } catch {
      setError('Failed to pause all bots');
    } finally {
      setBulkBusy(null);
    }
  };

  const handleDelete = async (bot: BotRow) => {
    if (!window.confirm(`Delete "${bot.name}"? This permanently removes all its orders and logs.`))
      return;
    setBusyBotId(bot.bot_id);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/bots/${bot.bot_id}`, { method: 'DELETE' });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(typeof data.detail === 'string' ? data.detail : 'Delete failed');
      }
      removeBot(bot.bot_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Delete failed');
    } finally {
      setBusyBotId(null);
    }
  };

  const openEdit = (bot: BotRow) => {
    setEditBotId(bot.bot_id);
    setEditForm({ name: bot.name, symbol: bot.symbol });
    setEditError(null);
  };

  const handleEdit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!editBotId) return;
    setEditError(null);
    setEditBusy(true);
    try {
      const res = await fetch(`${API_BASE}/api/bots/${editBotId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: editForm.name.trim() || undefined,
          symbol: editForm.symbol.trim() || undefined,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Update failed');
      setEditBotId(null);
      await loadBots();
    } catch (e) {
      setEditError(e instanceof Error ? e.message : 'Update failed');
    } finally {
      setEditBusy(false);
    }
  };

  // ── Render ───────────────────────────────────────────────────────────────

  return (
    <main className="flex-1 flex flex-col overflow-hidden p-6">
      {/* Page header */}
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
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => void resumeAll()}
            disabled={bulkBusy !== null || !bots.some((b) => b.status === 'paused')}
            className="px-4 py-2 bg-green-700 text-white rounded text-sm font-bold hover:bg-green-600 transition-all disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {bulkBusy === 'resume' ? 'Resuming…' : '▶ Resume All'}
          </button>
          <button
            type="button"
            onClick={() => void pauseAll()}
            disabled={bulkBusy !== null || !bots.some((b) => b.status === 'running')}
            className="px-4 py-2 bg-amber-600 text-white rounded text-sm font-bold hover:bg-amber-500 transition-all disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {bulkBusy === 'pause' ? 'Pausing…' : '⏸ Pause All'}
          </button>
          <button
            type="button"
            onClick={openCreate}
            className="px-4 py-2 bg-primary text-black rounded text-sm font-bold hover:brightness-110 transition-all"
          >
            + Create New Bot
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-4 p-3 rounded border border-red-500/40 bg-red-950/20 text-red-300 text-sm">
          {error}
        </div>
      )}

      {/* Bot list */}
      <div className="grid grid-cols-1 gap-3">
        {bots.length === 0 && !error && (
          <p className="text-gray-500 text-sm">No bots yet — create one with the button above.</p>
        )}
        {bots.map((bot) => {
          const isBusy = busyBotId === bot.bot_id;
          const isRunning = bot.status === 'running';
          const isPaused = bot.status === 'paused';
          return (
            <div
              key={bot.bot_id}
              className="bg-panel border border-border rounded p-4 flex flex-col sm:flex-row sm:items-center gap-3"
            >
              <Link to={`/bots/${bot.bot_id}`} className="flex-1 min-w-0 group">
                <div className="flex items-center gap-2 mb-1 flex-wrap">
                  <span className={`text-[9px] font-bold tracking-widest uppercase px-2 py-0.5 rounded ${statusBadge(bot.status)}`}>
                    {bot.status}
                  </span>
                  {bot.execution_mode === 'live' ? (
                    <span className="text-[9px] font-black tracking-widest uppercase px-2 py-0.5 rounded bg-red-500/20 text-red-400 border border-red-500/40">
                      ● LIVE
                    </span>
                  ) : (
                    <span className="text-[9px] font-bold tracking-widest uppercase px-2 py-0.5 rounded bg-blue-500/15 text-blue-300 border border-blue-400/25">
                      TESTNET
                    </span>
                  )}
                  <h3 className="text-base font-bold text-white group-hover:text-primary truncate transition-colors">
                    {bot.name}
                  </h3>
                </div>
                <div className="flex items-center gap-3 flex-wrap">
                  <p className="text-xs text-gray-400">
                    {bot.strategy.toUpperCase()} · {bot.symbol}
                    {bot.initial_budget_quote != null && (
                      <span className="ml-2 text-gray-500">
                        Budget: {bot.initial_budget_quote.toLocaleString()} USDT
                      </span>
                    )}
                  </p>
                  {/* P&L summary */}
                  {bot.realized_pnl_quote != null && (
                    <span className={`text-[11px] font-mono font-bold ${bot.realized_pnl_quote >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {bot.realized_pnl_quote >= 0 ? '+' : ''}{bot.realized_pnl_quote.toFixed(4)} USDT
                    </span>
                  )}
                  {bot.win_rate_pct != null && bot.closed_trades != null && bot.closed_trades > 0 && (
                    <span className="text-[10px] text-gray-500">
                      {bot.win_rate_pct.toFixed(1)}% WR · {bot.closed_trades} trades
                    </span>
                  )}
                  {(bot.closed_trades === 0 || bot.closed_trades == null) && (
                    <span className="text-[10px] text-gray-600">No closed trades yet</span>
                  )}
                </div>
              </Link>

              <div className="flex items-center gap-1.5 shrink-0 flex-wrap">
                {!isRunning && (
                  <button type="button" disabled={isBusy}
                    onClick={() => void setStatus(bot.bot_id, 'running')}
                    className="px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider bg-green-600/80 hover:bg-green-500 text-white rounded disabled:opacity-40 transition-colors">
                    START
                  </button>
                )}
                {isRunning && (
                  <button type="button" disabled={isBusy}
                    onClick={() => void setStatus(bot.bot_id, 'paused')}
                    className="px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider bg-amber-500/80 hover:bg-amber-400 text-black rounded disabled:opacity-40 transition-colors">
                    PAUSE
                  </button>
                )}
                {isPaused && (
                  <button type="button" disabled={isBusy}
                    onClick={() => void setStatus(bot.bot_id, 'running')}
                    className="px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider bg-green-600/80 hover:bg-green-500 text-white rounded disabled:opacity-40 transition-colors">
                    RESUME
                  </button>
                )}
                {(isRunning || isPaused) && (
                  <button type="button" disabled={isBusy}
                    onClick={() => void setStatus(bot.bot_id, 'stopped')}
                    className="px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider bg-red-700/70 hover:bg-red-600 text-white rounded disabled:opacity-40 transition-colors">
                    STOP
                  </button>
                )}
                <button type="button" disabled={isBusy}
                  onClick={() => openEdit(bot)}
                  className="px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider bg-gray-700/60 hover:bg-gray-600 text-gray-200 rounded disabled:opacity-40 transition-colors">
                  EDIT
                </button>
                <button type="button" disabled={isBusy || isRunning}
                  onClick={() => void handleDelete(bot)}
                  title={isRunning ? 'Stop bot first' : 'Delete bot'}
                  className="px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider bg-red-950/60 hover:bg-red-900/80 text-red-400 rounded disabled:opacity-40 transition-colors">
                  DELETE
                </button>
                <Link to={`/bots/${bot.bot_id}`}
                  className="px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider bg-primary/20 hover:bg-primary/30 text-primary rounded transition-colors">
                  MANAGE →
                </Link>
              </div>
            </div>
          );
        })}
      </div>

      {/* ── CREATE BOT MODAL ─────────────────────────────────────────────── */}
      {createStep !== null && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm p-4">
          <div className="bg-[#161616] border border-[#2a2a2a] rounded-xl w-full shadow-2xl"
            style={{
              maxWidth: createStep === 'pick-template'
                ? 680
                : createConfig?.template.strategy.startsWith('magi_ensemble') ? 560 : 460,
            }}>

            {/* Modal header */}
            <div className="flex items-center justify-between px-6 py-4 border-b border-[#2a2a2a]">
              {createStep === 'configure' && (
                <button type="button" onClick={() => setCreateStep('pick-template')}
                  className="text-gray-500 hover:text-white text-lg leading-none mr-3 transition-colors"
                  title="Back to templates">
                  ←
                </button>
              )}
              <h2 className="text-sm font-black uppercase tracking-widest text-white flex-1">
                {createStep === 'pick-template' ? 'Choose a Bot Template' : (
                  <span>
                    Configure{' '}
                    <span className="text-primary">{createConfig?.template.name}</span>
                  </span>
                )}
              </h2>
              <button type="button" onClick={closeCreate}
                className="text-gray-500 hover:text-white text-xl leading-none ml-2 transition-colors">
                ×
              </button>
            </div>

            {/* Step 1: Template picker */}
            {createStep === 'pick-template' && (
              <div className="p-5 grid grid-cols-1 sm:grid-cols-2 gap-3">
                {BOT_TEMPLATES.map((tpl) => {
                  const p = strategyDefaults[tpl.strategy] ?? {};
                  const isEnsemble = tpl.strategy.includes('ensemble');
                  return (
                  <button
                    key={tpl.id}
                    type="button"
                    onClick={() => pickTemplate(tpl)}
                    className="text-left rounded-lg border border-[#2a2a2a] bg-[#1c1c1c] p-4 hover:border-primary/60 hover:bg-primary/5 transition-all group"
                  >
                    <div className="flex items-start justify-between gap-2 mb-2">
                      <p className="text-sm font-black text-white group-hover:text-primary transition-colors">
                        {tpl.name}
                      </p>
                      <span className="shrink-0 text-[9px] font-bold uppercase tracking-wider text-primary/70 bg-primary/10 border border-primary/20 px-2 py-0.5 rounded whitespace-nowrap">
                        {(p.ohlcv_timeframe as string) ?? '…'}
                      </span>
                    </div>
                    <p className="text-[10px] font-semibold text-amber-400/80 uppercase tracking-wider mb-1.5">
                      {tpl.tagline}
                    </p>
                    <p className="text-[11px] text-gray-400 leading-snug">{tpl.description}</p>
                    <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-gray-600 font-mono">
                      {isEnsemble ? (
                        <>
                          <span>voters={(p.voters as string[] | undefined)?.length ?? '…'}</span>
                          <span>mode={(p.consensus_mode as string) ?? '…'}</span>
                          <span>threshold={p.consensus_threshold !== undefined ? `${(p.consensus_threshold as number) * 100}%` : '…'}</span>
                        </>
                      ) : (
                        <>
                          <span>fast={(p.fast_period as number | undefined) ?? '…'}</span>
                          <span>slow={(p.slow_period as number | undefined) ?? '…'}</span>
                        </>
                      )}
                      <span>buy={p.quote_fraction !== undefined ? `${(p.quote_fraction as number) * 100}%` : '…'}</span>
                      <span>sell={p.base_fraction !== undefined ? `${(p.base_fraction as number) * 100}%` : '…'}</span>
                    </div>
                  </button>
                  );
                })}
              </div>
            )}

            {/* Step 2: Configure */}
            {createStep === 'configure' && createConfig && (
              <form onSubmit={(e) => void handleCreate(e)} className="px-6 py-5 flex flex-col gap-4">
                {createError && (
                  <p className="text-red-400 text-xs border border-red-500/40 bg-red-950/20 rounded p-2">
                    {createError}
                  </p>
                )}

                {/* Template summary */}
                <div className="rounded-lg border border-primary/20 bg-primary/5 px-4 py-3 flex flex-wrap items-center gap-x-4 gap-y-1">
                  <span className="text-xs font-black text-primary uppercase tracking-widest">
                    {createConfig.template.name}
                  </span>
                  <span className="text-[10px] text-gray-500 font-mono">
                    {(createConfig.params.ohlcv_timeframe as string) ?? '…'}
                    {createConfig.template.strategy.includes('ensemble') ? (
                      <> · {createConfig.voters.length} voters
                        · {(createConfig.params.consensus_mode as string) ?? '…'}</>
                    ) : (
                      <> · fast={(createConfig.params.fast_period as number | undefined) ?? '…'}
                        / slow={(createConfig.params.slow_period as number | undefined) ?? '…'}</>
                    )}
                    {' '}· buy {createConfig.params.quote_fraction !== undefined ? `${(createConfig.params.quote_fraction as number) * 100}%` : '…'}
                    · sell {createConfig.params.base_fraction !== undefined ? `${(createConfig.params.base_fraction as number) * 100}%` : '…'}
                    · risk {createConfig.riskSettings.base_risk_pct}% base
                  </span>
                </div>

                {/* Voter picker — ensemble strategies only */}
                {createConfig.template.strategy.startsWith('magi_ensemble') && (
                  <div className="flex flex-col gap-2">
                    <div className="flex items-center justify-between">
                      <span className="text-[11px] font-bold uppercase tracking-wider text-gray-400">
                        Voters
                      </span>
                      <span className={`text-[10px] font-mono ${createConfig.voters.length < 2 ? 'text-red-400' : 'text-primary/70'}`}>
                        {createConfig.voters.length} selected
                      </span>
                    </div>
                    <div className="grid grid-cols-3 gap-1.5">
                      {ALL_VOTERS.map(({ id, label }) => {
                        const active = createConfig.voters.includes(id);
                        return (
                          <button
                            key={id}
                            type="button"
                            onClick={() =>
                              setCreateConfig((c) => {
                                if (!c) return c;
                                const next = active
                                  ? c.voters.filter((v) => v !== id)
                                  : [...c.voters, id];
                                return { ...c, voters: next };
                              })
                            }
                            className={`px-2 py-1.5 rounded text-[10px] font-bold border transition-all text-left truncate ${
                              active
                                ? 'border-primary/60 bg-primary/10 text-primary'
                                : 'border-[#2a2a2a] bg-[#1c1c1c] text-gray-500 hover:border-gray-600 hover:text-gray-400'
                            }`}
                          >
                            {label}
                          </button>
                        );
                      })}
                    </div>
                    <p className="text-[10px] text-gray-600">
                      Select 2 or more. MetaMagi will auto-adjust weights over time based on each voter's accuracy.
                    </p>
                  </div>
                )}

                <label className="flex flex-col gap-1.5 text-[11px] uppercase tracking-wider text-gray-400">
                  Bot Name
                  <input
                    type="text"
                    placeholder={`${createConfig.template.name} · ${createConfig.symbol.replace('/', '_')}`}
                    value={createConfig.name}
                    onChange={(e) => setCreateConfig((c) => c && ({ ...c, name: e.target.value }))}
                    className="rounded border border-[#2a2a2a] bg-black/40 px-3 py-2 text-sm text-white focus:border-primary/60 focus:outline-none"
                  />
                  <span className="text-[10px] text-gray-600 normal-case -mt-0.5">
                    Leave blank to auto-generate from template + pair.
                  </span>
                </label>

                <label className="flex flex-col gap-1.5 text-[11px] uppercase tracking-wider text-gray-400">
                  Trading Pair
                  <select
                    value={createConfig.symbol}
                    onChange={(e) => setCreateConfig((c) => c && ({ ...c, symbol: e.target.value }))}
                    className="rounded border border-[#2a2a2a] bg-black/40 px-3 py-2 text-sm text-white focus:border-primary/60 focus:outline-none"
                  >
                    {SUPPORTED_SYMBOLS.map((s) => <option key={s} value={s}>{s}</option>)}
                  </select>
                </label>

                <label className="flex flex-col gap-1.5 text-[11px] uppercase tracking-wider text-gray-400">
                  <span>
                    Initial Budget (USDT)
                    <span className="ml-1 text-red-400">*</span>
                  </span>
                  <input
                    ref={budgetRef}
                    type="text"
                    inputMode="decimal"
                    placeholder="e.g. 1000"
                    value={createConfig.budget}
                    onChange={(e) => setCreateConfig((c) => c && ({ ...c, budget: e.target.value }))}
                    className="rounded border border-[#2a2a2a] bg-black/40 px-3 py-2 text-sm text-white focus:border-primary/60 focus:outline-none"
                  />
                  <span className="text-[10px] text-gray-600 normal-case -mt-0.5">
                    Capital allocated to this bot. Used to track ROI and max drawdown.
                  </span>
                </label>

                <div className="rounded-lg border border-[#2a2a2a] bg-black/25 p-4">
                  <div className="mb-3 flex items-start justify-between gap-3">
                    <div>
                      <p className="text-[11px] font-black uppercase tracking-widest text-primary">
                        Risk Profile
                      </p>
                      <p className="mt-1 text-[10px] text-gray-500">
                        Pre-filled from the selected template. You can tune it now or later in CONFIG.
                      </p>
                    </div>
                    <div className="text-right font-mono text-[10px] text-gray-400">
                      <div>base {createConfig.riskSettings.base_risk_pct}%</div>
                      <div>strong {effectiveRiskPct(createConfig.riskSettings, 0.9).toFixed(2)}%</div>
                    </div>
                  </div>
                  <div className="grid grid-cols-2 gap-3">
                    <label className="flex flex-col gap-1 text-[10px] uppercase tracking-wider text-gray-500">
                      Base risk %
                      <input
                        type="number"
                        step="0.1"
                        min="0.1"
                        value={createConfig.riskSettings.base_risk_pct}
                        onChange={(e) =>
                          setCreateConfig((c) =>
                            c && ({
                              ...c,
                              riskSettings: { ...c.riskSettings, base_risk_pct: Number(e.target.value) },
                            }),
                          )
                        }
                        className="rounded border border-[#2a2a2a] bg-black/40 px-3 py-2 text-sm text-white focus:border-primary/60 focus:outline-none"
                      />
                    </label>
                    <label className="flex flex-col gap-1 text-[10px] uppercase tracking-wider text-gray-500">
                      Daily loss %
                      <input
                        type="number"
                        step="0.1"
                        min="0.1"
                        value={createConfig.riskSettings.daily_loss_limit_pct}
                        onChange={(e) =>
                          setCreateConfig((c) =>
                            c && ({
                              ...c,
                              riskSettings: { ...c.riskSettings, daily_loss_limit_pct: Number(e.target.value) },
                            }),
                          )
                        }
                        className="rounded border border-[#2a2a2a] bg-black/40 px-3 py-2 text-sm text-white focus:border-primary/60 focus:outline-none"
                      />
                    </label>
                    <label className="flex flex-col gap-1 text-[10px] uppercase tracking-wider text-gray-500">
                      Max drawdown %
                      <input
                        type="number"
                        step="0.1"
                        min="0.1"
                        value={createConfig.riskSettings.max_drawdown_pct}
                        onChange={(e) =>
                          setCreateConfig((c) =>
                            c && ({
                              ...c,
                              riskSettings: { ...c.riskSettings, max_drawdown_pct: Number(e.target.value) },
                            }),
                          )
                        }
                        className="rounded border border-[#2a2a2a] bg-black/40 px-3 py-2 text-sm text-white focus:border-primary/60 focus:outline-none"
                      />
                    </label>
                    <label className="flex flex-col gap-1 text-[10px] uppercase tracking-wider text-gray-500">
                      Consecutive losses
                      <input
                        type="number"
                        step="1"
                        min="1"
                        value={createConfig.riskSettings.consecutive_loss_limit}
                        onChange={(e) =>
                          setCreateConfig((c) =>
                            c && ({
                              ...c,
                              riskSettings: {
                                ...c.riskSettings,
                                consecutive_loss_limit: Number.parseInt(e.target.value, 10),
                              },
                            }),
                          )
                        }
                        className="rounded border border-[#2a2a2a] bg-black/40 px-3 py-2 text-sm text-white focus:border-primary/60 focus:outline-none"
                      />
                    </label>
                  </div>
                  <div className="mt-3 grid grid-cols-2 gap-2 text-[10px] uppercase tracking-wider text-gray-500">
                    <label className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={createConfig.riskSettings.enable_daily_loss_limit}
                        onChange={(e) =>
                          setCreateConfig((c) =>
                            c && ({
                              ...c,
                              riskSettings: { ...c.riskSettings, enable_daily_loss_limit: e.target.checked },
                            }),
                          )
                        }
                      />
                      Daily loss
                    </label>
                    <label className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={createConfig.riskSettings.enable_drawdown_protection}
                        onChange={(e) =>
                          setCreateConfig((c) =>
                            c && ({
                              ...c,
                              riskSettings: { ...c.riskSettings, enable_drawdown_protection: e.target.checked },
                            }),
                          )
                        }
                      />
                      Drawdown
                    </label>
                    <label className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={createConfig.riskSettings.enable_consecutive_loss}
                        onChange={(e) =>
                          setCreateConfig((c) =>
                            c && ({
                              ...c,
                              riskSettings: { ...c.riskSettings, enable_consecutive_loss: e.target.checked },
                            }),
                          )
                        }
                      />
                      Loss streak
                    </label>
                    <label className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={createConfig.riskSettings.enable_dynamic_sizing}
                        onChange={(e) =>
                          setCreateConfig((c) =>
                            c && ({
                              ...c,
                              riskSettings: { ...c.riskSettings, enable_dynamic_sizing: e.target.checked },
                            }),
                          )
                        }
                      />
                      Dynamic sizing
                    </label>
                  </div>
                </div>

                <div className="flex gap-2 pt-1">
                  <button type="submit" disabled={createBusy}
                    className="flex-1 py-2.5 bg-primary text-black text-[11px] font-black uppercase tracking-widest rounded hover:brightness-110 disabled:opacity-40 transition-all">
                    {createBusy ? 'Creating…' : 'Create Bot'}
                  </button>
                  <button type="button" onClick={closeCreate}
                    className="px-4 py-2.5 border border-[#2a2a2a] text-gray-400 text-[11px] font-bold uppercase tracking-widest rounded hover:border-gray-500 transition-all">
                    Cancel
                  </button>
                </div>
              </form>
            )}
          </div>
        </div>
      )}

      {/* ── EDIT BOT MODAL ───────────────────────────────────────────────── */}
      {editBotId && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm p-4">
          <div className="bg-[#161616] border border-[#2a2a2a] rounded-xl w-full max-w-md shadow-2xl">
            <div className="flex items-center justify-between px-6 py-4 border-b border-[#2a2a2a]">
              <h2 className="text-sm font-black uppercase tracking-widest text-white">Edit Bot</h2>
              <button type="button" onClick={() => setEditBotId(null)}
                className="text-gray-500 hover:text-white text-xl leading-none transition-colors">
                ×
              </button>
            </div>
            <form onSubmit={(e) => void handleEdit(e)} className="px-6 py-5 flex flex-col gap-4">
              {editError && (
                <p className="text-red-400 text-xs border border-red-500/40 bg-red-950/20 rounded p-2">
                  {editError}
                </p>
              )}
              <label className="flex flex-col gap-1.5 text-[11px] uppercase tracking-wider text-gray-400">
                Bot Name
                <input type="text" value={editForm.name}
                  onChange={(e) => setEditForm((f) => ({ ...f, name: e.target.value }))}
                  className="rounded border border-[#2a2a2a] bg-black/40 px-3 py-2 text-sm text-white focus:border-primary/60 focus:outline-none" />
              </label>
              <label className="flex flex-col gap-1.5 text-[11px] uppercase tracking-wider text-gray-400">
                Trading Pair
                <select value={editForm.symbol}
                  onChange={(e) => setEditForm((f) => ({ ...f, symbol: e.target.value }))}
                  className="rounded border border-[#2a2a2a] bg-black/40 px-3 py-2 text-sm text-white focus:border-primary/60 focus:outline-none">
                  {SUPPORTED_SYMBOLS.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </label>
              <p className="text-[10px] text-gray-600">
                To change the budget, open the bot detail → Capital &amp; New Instance.
              </p>
              <div className="flex gap-2 pt-1">
                <button type="submit" disabled={editBusy}
                  className="flex-1 py-2.5 bg-primary text-black text-[11px] font-black uppercase tracking-widest rounded hover:brightness-110 disabled:opacity-40 transition-all">
                  {editBusy ? 'Saving…' : 'Save Changes'}
                </button>
                <button type="button" onClick={() => setEditBotId(null)}
                  className="px-4 py-2.5 border border-[#2a2a2a] text-gray-400 text-[11px] font-bold uppercase tracking-widest rounded hover:border-gray-500 transition-all">
                  Cancel
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </main>
  );
}
