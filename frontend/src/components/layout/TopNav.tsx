import { Link, useLocation } from 'react-router-dom';
import { ShieldAlert, Terminal } from 'lucide-react';
import { useRealtimeStore } from '../../stores/realtimeStore';

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
  const executionMode = useRealtimeStore((state) => state.tradingSettings?.execution_mode ?? null);
  const botsStatus = useRealtimeStore((state) => state.channelStatuses['/ws/bots']);

  const isActive = (path: string) => {
    if (path === '/bots') {
      return location.pathname === '/bots' || location.pathname.startsWith('/bots/');
    }
    return location.pathname === path;
  };

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
          MagiTrader
        </Link>
        <div className="ml-4 hidden gap-4 md:flex">
          <span
            className={`font-label px-2 py-1 text-[10px] font-bold uppercase tracking-tighter ${
              latencyMs == null ? 'text-orange-900/50' : 'text-orange-400/80'
            }`}
          >
            LATENCY: {latencyMs != null ? `${latencyMs}MS` : '—'}
          </span>
          <span
            className={`font-label border-b-2 px-2 py-1 pb-1 text-[10px] font-bold uppercase tracking-tighter ${
              apiOk === null
                ? 'border-orange-900/30 text-orange-900/50'
                : apiOk
                  ? 'border-orange-500 text-orange-400'
                  : 'border-red-600/50 text-red-400'
            }`}
          >
            API: {apiOk === null ? '…' : apiOk ? 'OK' : 'ERR'}
          </span>
          <span
            className={`font-label px-2 py-1 text-[10px] font-bold uppercase tracking-tighter ${
              botsStatus === 'open' ? 'text-orange-400/70' : 'text-orange-900/50'
            }`}
          >
            WS: {botsStatus?.toUpperCase() ?? '—'}
          </span>
          <span
            className={`font-label px-2 py-1 text-[10px] font-bold uppercase tracking-tighter ${
              executionMode ? 'text-orange-400/70' : 'text-orange-900/50'
            }`}
          >
            MODE: {executionMode?.toUpperCase() ?? '—'}
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
