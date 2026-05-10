import { create } from 'zustand';
import { API_BASE } from '../config';
import { TRACKED_STREAM_IDS_FALLBACK, TRACKED_TICKER_SYMBOLS_FALLBACK } from '../trackedMarketsFallback';
import type { MagiWebSocketMessage, WebSocketStatus } from '../hooks/useMagiWebSocket';
import type { RiskSettings } from '../riskSettings';

export type NetworkView = 'testnet' | 'live';

export interface Ticker {
  symbol: string;
  price: string;
  change: string;
  changePercent: string;
}

export interface WalletItem {
  asset: string;
  free: number;
  used: number;
  total: number;
  value?: number;
}

export interface BotRow {
  bot_id: string;
  name: string;
  symbol: string;
  strategy: string;
  strategy_params_json?: string | null;
  status: string;
  execution_mode: string;
  started_at?: number | null;
  initial_budget_quote: number | null;
  realized_pnl_quote: number | null;
  win_rate_pct: number | null;
  closed_trades: number | null;
  risk_settings?: RiskSettings;
}

export interface TradingSettings {
  execution_mode: string;
  global_trading_halted?: boolean;
  live_confirmation_phrase?: string;
}

export interface BotRecord {
  bot_id: string;
  name: string;
  symbol: string;
  strategy: string;
  status: string;
  execution_mode: string;
  strategy_params_json: string | null;
  risk_settings?: RiskSettings;
}

export interface BotLogRow {
  log_id: number;
  bot_id: string;
  created_at: number;
  level: string;
  execution_mode: string;
  message: string;
}

export interface BotOrderStats {
  total_orders: number;
  buy_count: number;
  sell_count: number;
  last_order_at_ms: number | null;
}

export interface StrategyHealth {
  realized_pnl_quote: number;
  unrealized_pnl_quote: number | null;
  open_base_position: number;
  open_cost_basis_quote: number;
  closed_trades: number;
  winning_trades: number;
  losing_trades: number;
  breakeven_trades: number;
  win_rate_pct: number | null;
  max_drawdown_quote: number;
  max_drawdown_pct: number | null;
  quote_currency: string;
  mark_price: number | null;
  total_pnl_quote: number;
  initial_budget_quote: number | null;
  current_capital_quote: number | null;
  pnl_return_on_budget_pct: number | null;
  max_drawdown_vs_budget_pct: number | null;
  base_value_quote: number | null;
  quote_remaining: number | null;
  base_alloc_pct: number | null;
  quote_alloc_pct: number | null;
}

export interface BotOrderRow {
  order_row_id: number;
  bot_id: string;
  execution_mode: string;
  exchange_order_id: string | null;
  symbol: string;
  side: string;
  order_type: string;
  amount: number | null;
  cost: number | null;
  average: number | null;
  filled: number | null;
  status: string | null;
  created_at: number;
  display_price?: number | null;
  display_status?: string;
}

export interface ClosedTrade {
  timestamp: number | null;
  quantity: number;
  entry_price: number | null;
  exit_price: number | null;
  cost_basis_quote: number;
  proceeds_quote: number;
  realized_pnl: number;
  outcome: 'win' | 'loss' | 'flat';
  quote_currency: string;
}

export interface LiveVoterSignal {
  voter_name: string;
  voter_signal: 'buy' | 'sell' | 'hold';
  confidence: number | null;
  consensus_score: number | null;
  timestamp: number;
}

export interface BotDetailState {
  bot: BotRecord | null;
  logs: BotLogRow[];
  orderStats: BotOrderStats | null;
  orders: BotOrderRow[];
  strategyHealth: StrategyHealth | null;
  executionMode: string | null;
  tradeSummary: ClosedTrade[] | null;
  tradeSummaryLoading: boolean;
  liveVoterSignals: LiveVoterSignal[];
  voterSignalsUpdatedAt: number | null;
  loaded: boolean;
  loading: boolean;
  error: string | null;
}

interface TrackedMarketsResponse {
  ticker_symbols?: string[];
  stream_ids?: string[];
}

interface RealtimeState {
  bots: BotRow[];
  botsLoaded: boolean;
  botsLoading: boolean;
  botsError: string | null;
  tradingSettings: TradingSettings | null;
  settingsLoaded: boolean;
  settingsLoading: boolean;
  settingsError: string | null;
  trackedTickers: string[];
  trackedStreamIds: string[];
  marketTickers: Record<string, Ticker>;
  marketUpdatedAt: number | null;
  channelStatuses: Record<string, WebSocketStatus>;
  lastApiLatencyMs: number | null;
  apiOk: boolean | null;
  botDetailsById: Record<string, BotDetailState>;

  setChannelStatus: (path: string, status: WebSocketStatus) => void;
  loadBots: (signal?: AbortSignal) => Promise<void>;
  loadTradingSettings: (signal?: AbortSignal) => Promise<void>;
  loadTrackedMarkets: (signal?: AbortSignal) => Promise<void>;
  bootstrap: (signal?: AbortSignal) => Promise<void>;
  handleBotsMessage: (message: MagiWebSocketMessage<Record<string, unknown>>) => void;
  handleMarketMessage: (message: MagiWebSocketMessage<Record<string, unknown>>) => void;
  handleBotDetailMessage: (botId: string, message: MagiWebSocketMessage<Record<string, unknown>>) => void;
  loadBotDetail: (botId: string, signal?: AbortSignal) => Promise<void>;
  loadTradeSummary: (botId: string, signal?: AbortSignal) => Promise<void>;
  loadVoterSignals: (botId: string, signal?: AbortSignal) => Promise<void>;
  patchBotStatus: (botId: string, status: string) => void;
  removeBot: (botId: string) => void;
  scheduleBotsRefresh: () => void;
  scheduleBotDetailRefresh: (botId: string, includeTradeSummary?: boolean) => void;
}

const emptyBotDetail = (): BotDetailState => ({
  bot: null,
  logs: [],
  orderStats: null,
  orders: [],
  strategyHealth: null,
  executionMode: null,
  tradeSummary: null,
  tradeSummaryLoading: false,
  liveVoterSignals: [],
  voterSignalsUpdatedAt: null,
  loaded: false,
  loading: false,
  error: null,
});

let botsRefreshTimer = 0;
const detailRefreshTimers = new Map<string, number>();

function normalizeTickerSymbol(value: unknown): string {
  if (typeof value !== 'string') return '';
  return value.replace('/', '').toUpperCase();
}

function mergeBotStatus(bots: BotRow[], botId: string, status: string): BotRow[] {
  return bots.map((bot) => (bot.bot_id === botId ? { ...bot, status } : bot));
}

export const useRealtimeStore = create<RealtimeState>((set, get) => ({
  bots: [],
  botsLoaded: false,
  botsLoading: false,
  botsError: null,
  tradingSettings: null,
  settingsLoaded: false,
  settingsLoading: false,
  settingsError: null,
  trackedTickers: TRACKED_TICKER_SYMBOLS_FALLBACK,
  trackedStreamIds: TRACKED_STREAM_IDS_FALLBACK,
  marketTickers: {},
  marketUpdatedAt: null,
  channelStatuses: {},
  lastApiLatencyMs: null,
  apiOk: null,
  botDetailsById: {},

  setChannelStatus: (path, status) =>
    set((state) => ({
      channelStatuses: { ...state.channelStatuses, [path]: status },
      apiOk: status === 'open' ? true : state.apiOk,
    })),

  loadBots: async (signal) => {
    set({ botsLoading: true, botsError: null });
    try {
      const res = await fetch(`${API_BASE}/api/bots`, { signal });
      if (!res.ok) throw new Error('Failed to load bots');
      const data = (await res.json()) as { bots?: BotRow[] };
      set({ bots: data.bots ?? [], botsLoaded: true, botsLoading: false, apiOk: true });
    } catch (e) {
      if (e instanceof Error && e.name === 'AbortError') return;
      set({
        botsError: e instanceof Error ? e.message : 'Failed to load bots',
        botsLoading: false,
        apiOk: false,
      });
    }
  },

  loadTradingSettings: async (signal) => {
    set({ settingsLoading: true, settingsError: null });
    const t0 = performance.now();
    try {
      const res = await fetch(`${API_BASE}/api/settings/trading`, { signal });
      const elapsed = Math.round(performance.now() - t0);
      if (!res.ok) throw new Error('Failed to load settings');
      const data = (await res.json()) as TradingSettings;
      set({
        tradingSettings: data,
        settingsLoaded: true,
        settingsLoading: false,
        lastApiLatencyMs: elapsed,
        apiOk: true,
      });
    } catch (e) {
      if (e instanceof Error && e.name === 'AbortError') return;
      set({
        settingsError: e instanceof Error ? e.message : 'Failed to load settings',
        settingsLoaded: true,
        settingsLoading: false,
        lastApiLatencyMs: null,
        apiOk: false,
      });
    }
  },

  loadTrackedMarkets: async (signal) => {
    try {
      const res = await fetch(`${API_BASE}/api/market/tracked`, { signal });
      if (!res.ok) throw new Error('Failed to load tracked markets');
      const data = (await res.json()) as TrackedMarketsResponse;
      const tickerSymbols =
        Array.isArray(data.ticker_symbols) && data.ticker_symbols.length
          ? data.ticker_symbols
          : TRACKED_TICKER_SYMBOLS_FALLBACK;
      const streamIds =
        Array.isArray(data.stream_ids) && data.stream_ids.length
          ? data.stream_ids
          : tickerSymbols.map((symbol) => symbol.toLowerCase());
      set({ trackedTickers: tickerSymbols, trackedStreamIds: streamIds, apiOk: true });
    } catch (e) {
      if (e instanceof Error && e.name === 'AbortError') return;
      set({
        trackedTickers: TRACKED_TICKER_SYMBOLS_FALLBACK,
        trackedStreamIds: TRACKED_STREAM_IDS_FALLBACK,
        apiOk: false,
      });
    }
  },

  bootstrap: async (signal) => {
    await Promise.all([
      get().loadBots(signal),
      get().loadTradingSettings(signal),
      get().loadTrackedMarkets(signal),
    ]);
  },

  handleBotsMessage: (message) => {
    const data = message.data;
    if (message.type === 'bot_status' && typeof data.bot_id === 'string') {
      get().patchBotStatus(data.bot_id, String(data.status ?? ''));
      return;
    }

    if (message.type === 'trading_settings' && typeof data.execution_mode === 'string') {
      const executionMode = data.execution_mode;
      set((state) => ({
        tradingSettings: {
          ...(state.tradingSettings ?? { execution_mode: executionMode }),
          ...(data as Partial<TradingSettings>),
          execution_mode: executionMode,
        },
        settingsLoaded: true,
        apiOk: true,
      }));
      return;
    }

    if (message.type === 'bots_changed' && data.action === 'deleted' && typeof data.bot_id === 'string') {
      get().removeBot(data.bot_id);
      return;
    }

    if (['bots_changed', 'bot_updated', 'trade_executed'].includes(message.type)) {
      get().scheduleBotsRefresh();
    }
  },

  handleMarketMessage: (message) => {
    if (message.type !== 'market_tick') return;
    const symbol =
      typeof message.data.stream_id === 'string'
        ? message.data.stream_id.toUpperCase()
        : normalizeTickerSymbol(message.data.symbol);
    if (!symbol) return;

    const price = Number(message.data.last_price);
    if (!Number.isFinite(price)) return;
    const change = Number(message.data.price_change);
    const changePercent = Number(message.data.price_change_percent);

    set((state) => ({
      marketTickers: {
        ...state.marketTickers,
        [symbol]: {
          symbol,
          price: price.toFixed(2),
          change: Number.isFinite(change) ? change.toFixed(2) : '0.00',
          changePercent: Number.isFinite(changePercent) ? changePercent.toFixed(2) : '0.00',
        },
      },
      marketUpdatedAt: Date.now(),
    }));
  },

  handleBotDetailMessage: (botId, message) => {
    const data = message.data;
    if (message.type === 'bot_log' && data.log && typeof data.log === 'object') {
      const log = data.log as BotLogRow;
      set((state) => {
        const detail = state.botDetailsById[botId] ?? emptyBotDetail();
        return {
          botDetailsById: {
            ...state.botDetailsById,
            [botId]: {
              ...detail,
              logs: [log, ...detail.logs.filter((item) => item.log_id !== log.log_id)].slice(0, 150),
            },
          },
        };
      });
      return;
    }

    if (message.type === 'bot_log_batch' && Array.isArray(data.logs)) {
      const batchLogs = (data.logs as BotLogRow[]).slice().reverse();
      set((state) => {
        const detail = state.botDetailsById[botId] ?? emptyBotDetail();
        const existingIds = new Set(detail.logs.map((log) => log.log_id));
        const newLogs = batchLogs.filter((log) => !existingIds.has(log.log_id));
        return {
          botDetailsById: {
            ...state.botDetailsById,
            [botId]: { ...detail, logs: [...newLogs, ...detail.logs].slice(0, 150) },
          },
        };
      });
      return;
    }

    if (message.type === 'voter_signals' && Array.isArray(data.voter_signals)) {
      set((state) => {
        const detail = state.botDetailsById[botId] ?? emptyBotDetail();
        return {
          botDetailsById: {
            ...state.botDetailsById,
            [botId]: {
              ...detail,
              liveVoterSignals: data.voter_signals as LiveVoterSignal[],
              voterSignalsUpdatedAt: Date.now(),
            },
          },
        };
      });
      return;
    }

    if (message.type === 'bot_status' && typeof data.status === 'string') {
      get().patchBotStatus(botId, data.status);
      return;
    }

    if (message.type === 'bot_updated' && data.bot && typeof data.bot === 'object') {
      set((state) => {
        const detail = state.botDetailsById[botId] ?? emptyBotDetail();
        return {
          botDetailsById: {
            ...state.botDetailsById,
            [botId]: { ...detail, bot: data.bot as BotRecord, loaded: true },
          },
        };
      });
      get().scheduleBotsRefresh();
      return;
    }

    if (['trade_executed', 'trade_rejected', 'wallet_update'].includes(message.type)) {
      get().scheduleBotDetailRefresh(botId, true);
      if (message.type === 'trade_executed') get().scheduleBotsRefresh();
    }
  },

  loadBotDetail: async (botId, signal) => {
    set((state) => {
      const detail = state.botDetailsById[botId] ?? emptyBotDetail();
      return {
        botDetailsById: {
          ...state.botDetailsById,
          [botId]: { ...detail, loading: true, error: null },
        },
      };
    });

    try {
      const res = await fetch(`${API_BASE}/api/bots/${botId}`, { signal });
      if (res.status === 404) {
        set((state) => ({
          botDetailsById: {
            ...state.botDetailsById,
            [botId]: { ...emptyBotDetail(), loaded: true, error: 'Bot not found' },
          },
        }));
        return;
      }
      if (!res.ok) throw new Error('Failed to load bot');
      const data = await res.json();
      set((state) => ({
        botDetailsById: {
          ...state.botDetailsById,
          [botId]: {
            ...(state.botDetailsById[botId] ?? emptyBotDetail()),
            bot: data.bot,
            logs: data.logs || [],
            orderStats: data.order_stats ?? null,
            orders: data.orders || [],
            strategyHealth: data.strategy_health ?? null,
            executionMode: data.execution_mode ?? null,
            loaded: true,
            loading: false,
            error: null,
          },
        },
        apiOk: true,
      }));
    } catch (e) {
      if (e instanceof Error && e.name === 'AbortError') return;
      set((state) => {
        const detail = state.botDetailsById[botId] ?? emptyBotDetail();
        return {
          botDetailsById: {
            ...state.botDetailsById,
            [botId]: {
              ...detail,
              loading: false,
              error: e instanceof Error ? e.message : 'Load failed',
            },
          },
          apiOk: false,
        };
      });
    }
  },

  loadTradeSummary: async (botId, signal) => {
    set((state) => {
      const detail = state.botDetailsById[botId] ?? emptyBotDetail();
      return {
        botDetailsById: {
          ...state.botDetailsById,
          [botId]: { ...detail, tradeSummaryLoading: true },
        },
      };
    });
    try {
      const res = await fetch(`${API_BASE}/api/bots/${botId}/trade-summary`, { signal });
      if (!res.ok) return;
      const data = (await res.json()) as { trades: ClosedTrade[] };
      set((state) => {
        const detail = state.botDetailsById[botId] ?? emptyBotDetail();
        return {
          botDetailsById: {
            ...state.botDetailsById,
            [botId]: { ...detail, tradeSummary: data.trades ?? [], tradeSummaryLoading: false },
          },
        };
      });
    } catch (e) {
      if (e instanceof Error && e.name === 'AbortError') return;
    } finally {
      set((state) => {
        const detail = state.botDetailsById[botId] ?? emptyBotDetail();
        return {
          botDetailsById: {
            ...state.botDetailsById,
            [botId]: { ...detail, tradeSummaryLoading: false },
          },
        };
      });
    }
  },

  loadVoterSignals: async (botId, signal) => {
    try {
      const res = await fetch(`${API_BASE}/api/bots/${botId}/voter-signals`, { signal });
      if (!res.ok) return;
      const data = (await res.json()) as { voter_signals: LiveVoterSignal[] };
      set((state) => {
        const detail = state.botDetailsById[botId] ?? emptyBotDetail();
        return {
          botDetailsById: {
            ...state.botDetailsById,
            [botId]: {
              ...detail,
              liveVoterSignals: data.voter_signals ?? [],
              voterSignalsUpdatedAt: Date.now(),
            },
          },
        };
      });
    } catch (e) {
      if (e instanceof Error && e.name === 'AbortError') return;
    }
  },

  patchBotStatus: (botId, status) =>
    set((state) => {
      const detail = state.botDetailsById[botId];
      return {
        bots: status ? mergeBotStatus(state.bots, botId, status) : state.bots,
        botDetailsById: detail
          ? {
              ...state.botDetailsById,
              [botId]: {
                ...detail,
                bot: detail.bot && status ? { ...detail.bot, status } : detail.bot,
              },
            }
          : state.botDetailsById,
      };
    }),

  removeBot: (botId) =>
    set((state) => ({
      bots: state.bots.filter((bot) => bot.bot_id !== botId),
      botDetailsById: Object.fromEntries(
        Object.entries(state.botDetailsById).filter(([id]) => id !== botId),
      ),
    })),

  scheduleBotsRefresh: () => {
    if (botsRefreshTimer) return;
    botsRefreshTimer = window.setTimeout(() => {
      botsRefreshTimer = 0;
      void get().loadBots();
    }, 750);
  },

  scheduleBotDetailRefresh: (botId, includeTradeSummary = false) => {
    if (detailRefreshTimers.has(botId)) return;
    const timer = window.setTimeout(() => {
      detailRefreshTimers.delete(botId);
      void get().loadBotDetail(botId);
      if (includeTradeSummary) void get().loadTradeSummary(botId);
    }, 1_000);
    detailRefreshTimers.set(botId, timer);
  },
}));

export function selectBotDetail(botId: string | undefined): BotDetailState {
  if (!botId) return emptyBotDetail();
  return useRealtimeStore.getState().botDetailsById[botId] ?? emptyBotDetail();
}
