import { Outlet } from 'react-router-dom';
import { TopNav } from './TopNav';

export function Layout() {
  return (
    <>
      <TopNav />
      <Outlet />
    </>
  );
}
