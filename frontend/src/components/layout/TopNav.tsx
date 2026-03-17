import { Link, useLocation } from 'react-router-dom';

export function TopNav() {
  const location = useLocation();

  const isActive = (path: string) => {
    return location.pathname === path || location.pathname.startsWith(`${path}/`);
  };

  return (
    <header className="h-16 border-b border-border flex items-center justify-between px-6 bg-panel shrink-0" data-purpose="main-header">
      <div className="flex items-center space-gap-4">
        <div className="flex items-center gap-2 mr-8">
          <div className="w-8 h-8 bg-primary rounded-custom flex items-center justify-center">
            <svg className="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M13 10V3L4 14h7v7l9-11h-7z" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2"></path></svg>
          </div>
          <span className="font-bold text-xl tracking-tight text-white">MagiTrader</span>
        </div>
        <nav className="flex items-center space-x-6 text-sm font-medium text-gray-400">
          <Link to="/" className={`transition-colors ${location.pathname === '/' ? 'text-white border-b-2 border-primary py-5' : 'hover:text-white'}`}>Dashboard</Link>
          <Link to="/bots" className={`transition-colors ${isActive('/bots') ? 'text-white border-b-2 border-primary py-5' : 'hover:text-white'}`}>Bots</Link>
          <Link to="/strategies" className={`transition-colors ${isActive('/strategies') ? 'text-white border-b-2 border-primary py-5' : 'hover:text-white'}`}>Strategies</Link>
          <Link to="/performance" className={`transition-colors ${isActive('/performance') ? 'text-white border-b-2 border-primary py-5' : 'hover:text-white'}`}>Performance</Link>
          <Link to="/database" className={`transition-colors ${isActive('/database') ? 'text-white border-b-2 border-primary py-5' : 'hover:text-white'}`}>Database</Link>
          <Link to="/settings" className={`transition-colors ${isActive('/settings') ? 'text-white border-b-2 border-primary py-5' : 'hover:text-white'}`}>Settings</Link>
        </nav>
      </div>
      <div className="flex items-center gap-4">
        <span className="flex items-center gap-2 text-xs font-mono bg-green-500/10 text-green-500 px-2 py-1 rounded border border-green-500/20">
          <span className="w-2 h-2 bg-green-500 rounded-full animate-pulse"></span>
          SYSTEM OPERATIONAL
        </span>
        <div className="w-8 h-8 rounded-full bg-gray-800 border border-border"></div>
      </div>
    </header>
  );
}
