import { useEffect, useRef, useState } from 'react';
import { useQueryClient, type QueryKey } from '@tanstack/react-query';
import { WS_BASE } from '../config';

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

function buildWebSocketUrl(path: string): string {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  return `${WS_BASE}${normalizedPath}`;
}

function reconnectDelayMs(attempt: number): number {
  const base = Math.min(30_000, 1_000 * 2 ** Math.max(0, attempt - 1));
  return base + Math.floor(Math.random() * 500);
}

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

  onMessageRef.current = onMessage;

  useEffect(() => {
    if (!enabled) {
      setStatus('closed');
      return;
    }

    let socket: WebSocket | null = null;
    let closedByEffect = false;
    let reconnectTimer = 0;
    let heartbeatTimer = 0;
    let attempts = 0;

    const clearTimers = () => {
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      if (heartbeatTimer) window.clearInterval(heartbeatTimer);
      reconnectTimer = 0;
      heartbeatTimer = 0;
    };

    const connect = () => {
      if (closedByEffect) return;
      setStatus(attempts === 0 ? 'connecting' : 'reconnecting');
      socket = new WebSocket(buildWebSocketUrl(path));

      socket.onopen = () => {
        attempts = 0;
        setStatus('open');
        heartbeatTimer = window.setInterval(() => {
          if (socket?.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({ type: 'ping', timestamp: Date.now() }));
          }
        }, heartbeatMs);
      };

      socket.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data) as MagiWebSocketMessage<TData>;
          if (message.type === 'pong' || message.type === 'connected') return;
          queryClient.setQueryData(['magi-ws', path, message.type], message);
          if (queryKey) queryClient.setQueryData(queryKey, message);
          onMessageRef.current?.(message);
        } catch {
          // Ignore malformed frames; the next valid backend event will update state.
        }
      };

      socket.onclose = () => {
        if (heartbeatTimer) window.clearInterval(heartbeatTimer);
        heartbeatTimer = 0;
        if (closedByEffect) return;

        attempts += 1;
        if (attempts > maxRetries) {
          setStatus('fallback');
          return;
        }

        setStatus('reconnecting');
        reconnectTimer = window.setTimeout(connect, reconnectDelayMs(attempts));
      };

      socket.onerror = () => {
        socket?.close();
      };
    };

    connect();

    return () => {
      closedByEffect = true;
      clearTimers();
      socket?.close();
    };
  }, [enabled, heartbeatMs, maxRetries, path, queryClient, queryKey]);

  return {
    status,
    isConnected: status === 'open',
    isFallbackPolling: status === 'fallback',
  };
}
