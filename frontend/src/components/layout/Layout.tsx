import { Outlet } from 'react-router-dom';
import { TopNav } from './TopNav';

export function Layout() {
  return (
    <div className="flex h-[100dvh] min-h-0 w-full max-w-[100vw] flex-col overflow-x-hidden bg-magi-bg">
      <TopNav />
      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        <Outlet />
      </div>
    </div>
  );
}
