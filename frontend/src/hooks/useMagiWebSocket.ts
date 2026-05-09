import { useEffect, useRef, useState } from 'react';
import { useQueryClient, type QueryKey } from '@tanstack/react-query';
import { subscribeMagiChannel } from '../services/realtimeChannels';

export type WebSocketStatus = 'connecting' | 'open' | 'reconnecting' | 'fallback' | 'closed';

export interface MagiWebSocketMessage<TData = Record<string, unknown>> {
  type: string;
  timestamp: number;
  data: TData;
}

interface UseMagiWebSocketOptions<TData = Record<string, unknown>> {
  path: string;
  enabled?: boolean;
  queryKey?: QueryKey;
  maxRetries?: number;
  heartbeatMs?: number;
  onMessage?: (message: MagiWebSocketMessage<TData>) => void;
}

interface UseMagiWebSocketResult {
  status: WebSocketStatus;
  isConnected: boolean;
  isFallbackPolling: boolean;
}

// Cache entries for ['magi-ws', path, eventType] that have not been refreshed
// within CACHE_STALE_THRESHOLD_MS are pruned every CACHE_CLEANUP_INTERVAL_MS.
// In normal operation each (path, eventType) pair is overwritten on every event
// and never accumulates, but orphaned entries from old sessions or rarely-fired
// event types can linger indefinitely without this cleanup.
const CACHE_CLEANUP_INTERVAL_MS = 5 * 60 * 1000; // 5 minutes
const CACHE_STALE_THRESHOLD_MS = 5 * 60 * 1000;  // 5 minutes without update

export function useMagiWebSocket<TData = Record<string, unknown>>({
  path,
  enabled = true,
  queryKey,
  maxRetries = 8,
  heartbeatMs = 25_000,
  onMessage,
}: UseMagiWebSocketOptions<TData>): UseMagiWebSocketResult {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<WebSocketStatus>(enabled ? 'connecting' : 'closed');
  const onMessageRef = useRef(onMessage);

  useEffect(() => {
    onMessageRef.current = onMessage;
  }, [onMessage]);

  useEffect(() => {
    // Periodically prune stale ['magi-ws', path, eventType] cache entries.
    // Each live subscription overwrites its key on every incoming event, so
    // stale entries only appear when a channel produces a rarely-seen eventType
    // or when the hook unmounts without explicit cache invalidation.
    const cleanupTimer = window.setInterval(() => {
      const cutoff = Date.now() - CACHE_STALE_THRESHOLD_MS;
      queryClient
        .getQueryCache()
        .getAll()
        .filter((q) => {
          const key = q.queryKey;
          return (
            Array.isArray(key) &&
            key[0] === 'magi-ws' &&
            key[1] === path
          );
        })
        .filter((q) => (q.state.dataUpdatedAt ?? 0) < cutoff)
        .forEach((q) =>
          queryClient.removeQueries({ queryKey: q.queryKey, exact: true })
        );
    }, CACHE_CLEANUP_INTERVAL_MS);

    const unsubscribe = subscribeMagiChannel({
      path,
      enabled,
      maxRetries,
      heartbeatMs,
      onStatus: setStatus,
      onMessage: (message) => {
        const typedMessage = message as MagiWebSocketMessage<TData>;
        queryClient.setQueryData(['magi-ws', path, typedMessage.type], typedMessage);
        if (queryKey) queryClient.setQueryData(queryKey, typedMessage);
        onMessageRef.current?.(typedMessage);
      },
    });

    return () => {
      window.clearInterval(cleanupTimer);
      unsubscribe();
    };
  }, [enabled, heartbeatMs, maxRetries, path, queryClient, queryKey]);

  return {
    status,
    isConnected: status === 'open',
    isFallbackPolling: status === 'fallback',
  };
}
