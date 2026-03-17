import { Routes, Route } from 'react-router-dom';
import { Layout } from './components/layout/Layout';
import Dashboard from './pages/Dashboard';
import BotsList from './pages/BotsList';
import BotDetail from './pages/BotDetail';
import Performance from './pages/Performance';
import Settings from './pages/Settings';
import Database from './pages/Database';

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="bots" element={<BotsList />} />
        <Route path="bots/:id" element={<BotDetail />} />
        <Route path="performance" element={<Performance />} />
        <Route path="database" element={<Database />} />
        <Route path="settings" element={<Settings />} />
      </Route>
    </Routes>
  );
}
