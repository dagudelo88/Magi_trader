import { WS_BASE } from '../config';
import type { MagiWebSocketMessage, WebSocketStatus } from '../hooks/useMagiWebSocket';

type MessageHandler = (message: MagiWebSocketMessage<Record<string, unknown>>) => void;
type StatusHandler = (status: WebSocketStatus) => void;

interface SubscribeOptions {
  path: string;
  enabled?: boolean;
  maxRetries?: number;
  heartbeatMs?: number;
  onMessage?: MessageHandler;
  onStatus?: StatusHandler;
}

interface Subscriber {
  onMessage?: MessageHandler;
  onStatus?: StatusHandler;
}

const IGNORED_MESSAGE_TYPES = new Set(['connected', 'pong', 'ping']);
const CLOSE_GRACE_MS = 2_000;

function buildWebSocketUrl(path: string): string {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  return `${WS_BASE}${normalizedPath}`;
}

function reconnectDelayMs(attempt: number): number {
  const base = Math.min(30_000, 1_000 * 2 ** Math.max(0, attempt - 1));
  return base + Math.floor(Math.random() * 500);
}

class ChannelConnection {
  private socket: WebSocket | null = null;
  private status: WebSocketStatus = 'closed';
  private attempts = 0;
  private heartbeatTimer = 0;
  private reconnectTimer = 0;
  private closeTimer = 0;
  private nextSubscriberId = 1;
  private subscribers = new Map<number, Subscriber>();
  private readonly path: string;
  private readonly maxRetries: number;
  private readonly heartbeatMs: number;

  constructor(path: string, maxRetries: number, heartbeatMs: number) {
    this.path = path;
    this.maxRetries = maxRetries;
    this.heartbeatMs = heartbeatMs;
  }

  subscribe(subscriber: Subscriber): () => void {
    const id = this.nextSubscriberId++;
    this.subscribers.set(id, subscriber);
    this.cancelClose();
    subscriber.onStatus?.(this.status);
    this.connect();

    return () => {
      this.subscribers.delete(id);
      if (this.subscribers.size === 0) {
        this.scheduleClose();
      }
    };
  }

  getStatus(): WebSocketStatus {
    return this.status;
  }

  private connect(): void {
    if (
      this.socket?.readyState === WebSocket.OPEN ||
      this.socket?.readyState === WebSocket.CONNECTING
    ) {
      return;
    }

    this.clearReconnectTimer();
    this.setStatus(this.attempts === 0 ? 'connecting' : 'reconnecting');
    this.socket = new WebSocket(buildWebSocketUrl(this.path));

    this.socket.onopen = () => {
      this.attempts = 0;
      this.setStatus('open');
      this.clearHeartbeatTimer();
      this.heartbeatTimer = window.setInterval(() => {
        if (this.socket?.readyState === WebSocket.OPEN) {
          this.socket.send(JSON.stringify({ type: 'ping', timestamp: Date.now() }));
        }
      }, this.heartbeatMs);
    };

    this.socket.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data) as MagiWebSocketMessage<Record<string, unknown>>;
        if (IGNORED_MESSAGE_TYPES.has(message.type)) return;
        this.subscribers.forEach((subscriber) => subscriber.onMessage?.(message));
      } catch {
        // A malformed frame should not tear down the shared channel.
      }
    };

    this.socket.onclose = () => {
      this.clearHeartbeatTimer();
      this.socket = null;
      if (this.subscribers.size === 0) {
        this.setStatus('closed');
        return;
      }

      this.attempts += 1;
      if (this.attempts > this.maxRetries) {
        this.setStatus('fallback');
        return;
      }

      this.setStatus('reconnecting');
      this.reconnectTimer = window.setTimeout(() => this.connect(), reconnectDelayMs(this.attempts));
    };

    this.socket.onerror = () => {
      this.socket?.close();
    };
  }

  private scheduleClose(): void {
    this.cancelClose();
    this.closeTimer = window.setTimeout(() => {
      if (this.subscribers.size > 0) return;
      this.close();
      channels.delete(this.path);
    }, CLOSE_GRACE_MS);
  }

  private cancelClose(): void {
    if (!this.closeTimer) return;
    window.clearTimeout(this.closeTimer);
    this.closeTimer = 0;
  }

  private close(): void {
    this.clearHeartbeatTimer();
    this.clearReconnectTimer();
    this.socket?.close();
    this.socket = null;
    this.attempts = 0;
    this.setStatus('closed');
  }

  private clearHeartbeatTimer(): void {
    if (!this.heartbeatTimer) return;
    window.clearInterval(this.heartbeatTimer);
    this.heartbeatTimer = 0;
  }

  private clearReconnectTimer(): void {
    if (!this.reconnectTimer) return;
    window.clearTimeout(this.reconnectTimer);
    this.reconnectTimer = 0;
  }

  private setStatus(status: WebSocketStatus): void {
    if (this.status === status) return;
    this.status = status;
    this.subscribers.forEach((subscriber) => subscriber.onStatus?.(status));
  }
}

const channels = new Map<string, ChannelConnection>();

export function subscribeMagiChannel({
  path,
  enabled = true,
  maxRetries = 8,
  heartbeatMs = 25_000,
  onMessage,
  onStatus,
}: SubscribeOptions): () => void {
  if (!enabled) {
    onStatus?.('closed');
    return () => undefined;
  }

  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  let channel = channels.get(normalizedPath);
  if (!channel) {
    channel = new ChannelConnection(normalizedPath, maxRetries, heartbeatMs);
    channels.set(normalizedPath, channel);
  }

  return channel.subscribe({ onMessage, onStatus });
}

export function getMagiChannelStatus(path: string): WebSocketStatus {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  return channels.get(normalizedPath)?.getStatus() ?? 'closed';
}
