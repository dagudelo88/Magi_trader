"""Application WebSocket broadcast manager.

FastAPI owns the event loop, while the bot runner does blocking exchange work
in thread-pool workers.  This module keeps one injectable manager that can be
used from both async code and those worker threads.

Throttling design
-----------------
Non-priority events are rate-limited to WS_MAX_EVENTS_PER_SECOND per
(channel, event_type) pair.  Exceeding calls are silently dropped — this
prevents bot-log floods from overwhelming slow clients while still delivering
every trade and wallet event immediately.

Priority events (trade_executed, trade_rejected, wallet_update) always bypass
the rate limit so financially important signals are never discarded.

A background heartbeat pings every active channel every WS_HEARTBEAT_SEC
seconds so idle TCP connections stay alive and clients can detect stale state.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from typing import Any

from fastapi import WebSocket
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Configuration — override via environment variables before server start.
# ---------------------------------------------------------------------------

# Max broadcasts per second for a given (channel, event_type) pair.
# Increase for more real-time feel; decrease to protect slow/mobile clients.
WS_MAX_EVENTS_PER_SECOND: float = max(
    0.1, float(os.environ.get("WS_MAX_EVENTS_PER_SECOND", "2"))
)

# Seconds between server-initiated heartbeat pings to every active channel.
WS_HEARTBEAT_SEC: float = float(os.environ.get("WS_HEARTBEAT_SEC", "22"))

# Derived minimum gap between consecutive broadcasts per (channel, event_type).
_WS_MIN_INTERVAL: float = 1.0 / WS_MAX_EVENTS_PER_SECOND

# Event types that bypass per-channel rate limiting and are always delivered
# immediately regardless of how recently the channel was last written to.
# Keep this list small — only events that carry financially relevant state.
_PRIORITY_EVENTS: frozenset[str] = frozenset({
    "trade_executed",
    "trade_rejected",
    "wallet_update",
    "connected",
    "pong",
})


class WebSocketMessage(BaseModel):
    """Stable envelope for every backend-to-frontend push message."""

    type: str
    timestamp: int = Field(default_factory=_now_ms)
    data: dict[str, Any] = Field(default_factory=dict)


class ConnectionManager:
    """Channel-based broadcast manager for browser WebSocket clients."""

    def __init__(self) -> None:
        self._channels: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        # Rate-limit state: (channel, event_type) → last-sent monotonic time.
        # Written/read exclusively from the event loop; no extra lock needed.
        self._last_sent: dict[tuple[str, str], float] = {}
        self._heartbeat_task: asyncio.Task | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Bind the FastAPI loop so worker threads can schedule broadcasts."""
        self._loop = loop or asyncio.get_running_loop()
        # Start the background heartbeat so idle connections stay alive and
        # clients can detect stale state without their own ping timer.
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = self._loop.create_task(
                self._heartbeat_loop(), name="ws-heartbeat"
            )

    async def _heartbeat_loop(self) -> None:
        """Broadcast a lightweight ping to every active channel periodically.

        Uses priority=True so heartbeats bypass rate limiting.
        Clients should silently ignore these pings.
        """
        while True:
            await asyncio.sleep(WS_HEARTBEAT_SEC)
            if not self._channels:
                continue
            ts = _now_ms()
            for channel in list(self._channels):
                await self.broadcast(
                    channel, "ping", {"time": ts}, priority=True
                )

    async def connect(self, websocket: WebSocket, channel: str) -> None:
        await websocket.accept()
        async with self._lock:
            self._channels[channel].add(websocket)
        await websocket.send_text(
            WebSocketMessage(
                type="connected",
                data={
                    "channel": channel,
                    "clients": self.connected_count(channel),
                },
            ).model_dump_json()
        )

    async def disconnect(self, websocket: WebSocket, channel: str) -> None:
        async with self._lock:
            self._channels[channel].discard(websocket)
            if not self._channels[channel]:
                self._channels.pop(channel, None)

    def connected_count(self, channel: str | None = None) -> int:
        if channel is not None:
            return len(self._channels.get(channel, set()))
        return sum(len(connections) for connections in self._channels.values())

    def health(self) -> dict[str, Any]:
        return {
            "connected_clients": self.connected_count(),
            "channels": {
                channel: len(connections)
                for channel, connections in sorted(self._channels.items())
            },
        }

    async def broadcast(
        self,
        channel: str,
        event_type: str,
        data: dict[str, Any] | None = None,
        *,
        priority: bool = False,
    ) -> None:
        """Broadcast an event to all subscribers on ``channel``.

        Rate limiting: non-priority events are silently dropped when the same
        (channel, event_type) pair was broadcast within the last
        ``_WS_MIN_INTERVAL`` seconds.  Trade/wallet events and server heartbeat
        pings are exempt and always delivered immediately.

        Stale WebSocket connections (send raises) are pruned on detection.
        """
        if not priority and event_type not in _PRIORITY_EVENTS:
            key = (channel, event_type)
            now = time.monotonic()
            if now - self._last_sent.get(key, 0.0) < _WS_MIN_INTERVAL:
                return  # rate-limited — drop to prevent client flooding
            self._last_sent[key] = now

        message = WebSocketMessage(type=event_type, data=data or {})
        payload = message.model_dump_json()
        async with self._lock:
            targets = list(self._channels.get(channel, set()))

        if not targets:
            return

        stale: list[WebSocket] = []
        for websocket in targets:
            try:
                await websocket.send_text(payload)
            except (RuntimeError, ConnectionResetError, OSError) as exc:
                # Log at DEBUG — dropped connections are expected during client
                # refreshes and should not pollute production logs.
                logger.debug("WS send failed on channel %r: %s", channel, exc)
                stale.append(websocket)

        if stale:
            async with self._lock:
                for websocket in stale:
                    self._channels[channel].discard(websocket)
                if not self._channels[channel]:
                    self._channels.pop(channel, None)

    def publish(
        self,
        channel: str,
        event_type: str,
        data: dict[str, Any] | None = None,
        *,
        priority: bool = False,
    ) -> None:
        """Thread-safe fire-and-forget broadcast.

        Synchronous bot-runner code calls this from worker threads.  Async code
        running on the FastAPI loop gets a cheap create_task path.

        The ``priority`` flag bypasses per-channel rate limiting — use for
        trade confirmations, wallet updates, and other critical events.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        coro = self.broadcast(channel, event_type, data, priority=priority)
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is loop:
            loop.create_task(coro)
        else:
            asyncio.run_coroutine_threadsafe(coro, loop)


ws_manager = ConnectionManager()


def publish_bots_event(
    event_type: str,
    data: dict[str, Any] | None = None,
    *,
    priority: bool = False,
) -> None:
    ws_manager.publish("bots", event_type, data, priority=priority)


def publish_bot_event(
    bot_id: str,
    event_type: str,
    data: dict[str, Any] | None = None,
    *,
    include_overview: bool = True,
    priority: bool = False,
) -> None:
    payload = {"bot_id": bot_id, **(data or {})}
    ws_manager.publish(f"bot:{bot_id}", event_type, payload, priority=priority)
    if include_overview:
        ws_manager.publish("bots", event_type, payload, priority=priority)


def publish_market_event(
    event_type: str,
    data: dict[str, Any] | None = None,
) -> None:
    ws_manager.publish("market", event_type, data)
