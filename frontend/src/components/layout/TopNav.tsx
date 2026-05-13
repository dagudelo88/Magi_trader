import { Link, useLocation } from 'react-router-dom';
import { ShieldAlert, Terminal } from 'lucide-react';
import type { WebSocketStatus } from '../../hooks/useMagiWebSocket';
import { useRealtimeStore } from '../../stores/realtimeStore';

/** API round-trip: ms (good / elevated / poor). */
const LATENCY_GOOD_MS = 300;
const LATENCY_WARN_MS = 900;

type StatusTone = 'good' | 'warn' | 'bad';

const TONE_TEXT: Record<StatusTone, string> = {
  good: 'text-emerald-400',
  warn: 'text-amber-400',
  bad: 'text-red-400',
};

/** Bottom border under API label matches health. */
const TONE_API: Record<StatusTone, string> = {
  good: 'border-emerald-500 text-emerald-400',
  warn: 'border-amber-500 text-amber-400',
  bad: 'border-red-500 text-red-400',
};

function latencyTone(latencyMs: number | null): StatusTone {
  if (latencyMs == null) return 'warn';
  if (latencyMs <= LATENCY_GOOD_MS) return 'good';
  if (latencyMs <= LATENCY_WARN_MS) return 'warn';
  return 'bad';
}

function apiTone(apiOk: boolean | null): StatusTone {
  if (apiOk === null) return 'warn';
  return apiOk ? 'good' : 'bad';
}

function wsTone(status: WebSocketStatus | undefined): StatusTone {
  if (status === 'open') return 'good';
  if (status === 'closed') return 'bad';
  return 'warn';
}

const NAV = [
  { to: '/', label: 'DASHBOARD' },
  { to: '/bots', label: 'BOTS' },
  { to: '/performance', label: 'ANALYTICS' },
  { to: '/database', label: 'DATA' },
  { to: '/settings', label: 'CONFIG' },
] as const;

export function TopNav() {
  const location = useLocation();
  const latencyMs = useRealtimeStore((state) => state.lastApiLatencyMs);
  const apiOk = useRealtimeStore((state) => state.apiOk);
  const botsStatus = useRealtimeStore((state) => state.channelStatuses['/ws/bots']);

  const isActive = (path: string) => {
    if (path === '/bots') {
      return location.pathname === '/bots' || location.pathname.startsWith('/bots/');
    }
    return location.pathname === path;
  };

  const lat = latencyTone(latencyMs);
  const api = apiTone(apiOk);
  const ws = wsTone(botsStatus);

  return (
    <header
      className="z-50 flex min-h-14 shrink-0 flex-wrap items-center justify-between gap-2 border-b-2 border-orange-900/30 bg-[#131313] px-3 py-2 shadow-[0_0_15px_rgba(255,145,0,0.08)] sm:h-14 sm:flex-nowrap sm:px-6 sm:py-0"
      data-purpose="main-header"
    >
      <div className="flex min-w-0 items-center gap-2 sm:gap-4">
        <Link
          to="/"
          className="font-label shrink-0 text-lg font-black italic tracking-widest text-orange-600 sm:text-xl"
        >
          Magi_Trader
        </Link>
        <div className="ml-4 hidden gap-4 md:flex">
          <span
            className={`font-label px-2 py-1 text-[10px] font-bold uppercase tracking-tighter ${TONE_TEXT[lat]}`}
            title={
              latencyMs == null
                ? 'Waiting for latency sample'
                : `API latency ${latencyMs} ms (green ≤${LATENCY_GOOD_MS}, amber ≤${LATENCY_WARN_MS})`
            }
          >
            LATENCY: {latencyMs != null ? `${latencyMs}MS` : '—'}
          </span>
          <span
            className={`font-label border-b-2 px-2 py-1 pb-1 text-[10px] font-bold uppercase tracking-tighter ${TONE_API[api]}`}
            title={apiOk === false ? 'Backend unreachable or error' : undefined}
          >
            API: {apiOk === null ? '…' : apiOk ? 'OK' : 'ERR'}
          </span>
          <span
            className={`font-label px-2 py-1 text-[10px] font-bold uppercase tracking-tighter ${TONE_TEXT[ws]}`}
            title={
              botsStatus === 'closed'
                ? 'Bots WebSocket closed'
                : botsStatus === 'open'
                  ? 'Bots stream connected'
                  : 'Connecting or degraded WebSocket'
            }
          >
            WebSocket: {botsStatus?.toUpperCase() ?? '—'}
          </span>
        </div>
      </div>
      <div className="flex min-w-0 flex-1 items-center justify-end gap-2 sm:flex-none sm:gap-6">
        <nav className="flex max-w-full items-center gap-0.5 overflow-x-auto [-ms-overflow-style:none] [scrollbar-width:none] sm:gap-1 [&::-webkit-scrollbar]:hidden">
          {NAV.map(({ to, label }) => (
            <Link
              key={to}
              to={to}
              className={`font-label shrink-0 px-2 py-1 text-[9px] font-semibold uppercase tracking-wider transition-colors sm:px-4 sm:text-[10px] sm:tracking-widest ${
                isActive(to)
                  ? 'bg-orange-500 font-black text-black'
                  : 'text-orange-700/70 hover:text-orange-200'
              }`}
            >
              {label}
            </Link>
          ))}
        </nav>
        <div className="hidden h-6 w-px bg-orange-900/20 sm:block" />
        <div className="hidden items-center gap-1 sm:flex">
          <button
            type="button"
            className="material-symbols-outlined rounded p-1.5 text-orange-500 hover:bg-orange-950/20"
            aria-label="Components"
          >
            settings_input_component
          </button>
          <button
            type="button"
            className="rounded p-1.5 text-orange-500 hover:bg-orange-950/20"
            aria-label="Terminal"
          >
            <Terminal className="h-5 w-5" strokeWidth={1.5} />
          </button>
          <button
            type="button"
            className="rounded p-1.5 text-magi-secondary hover:bg-orange-950/20"
            aria-label="Shield"
          >
            <ShieldAlert className="h-5 w-5" strokeWidth={1.5} />
          </button>
        </div>
      </div>
    </header>
  );
}
