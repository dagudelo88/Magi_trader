import { useEffect } from 'react';
import { useMagiWebSocket } from '../hooks/useMagiWebSocket';
import { useRealtimeStore } from '../stores/realtimeStore';

export function RealtimeBootstrap() {
  const bootstrap = useRealtimeStore((state) => state.bootstrap);
  const handleBotsMessage = useRealtimeStore((state) => state.handleBotsMessage);
  const handleMarketMessage = useRealtimeStore((state) => state.handleMarketMessage);
  const setChannelStatus = useRealtimeStore((state) => state.setChannelStatus);

  const botsWs = useMagiWebSocket({
    path: '/ws/bots',
    onMessage: handleBotsMessage,
  });

  const marketWs = useMagiWebSocket({
    path: '/ws/market',
    onMessage: handleMarketMessage,
  });

  useEffect(() => {
    setChannelStatus('/ws/bots', botsWs.status);
  }, [botsWs.status, setChannelStatus]);

  useEffect(() => {
    setChannelStatus('/ws/market', marketWs.status);
  }, [marketWs.status, setChannelStatus]);

  useEffect(() => {
    const ctrl = new AbortController();
    void bootstrap(ctrl.signal);
    return () => ctrl.abort();
  }, [bootstrap]);

  return null;
}
