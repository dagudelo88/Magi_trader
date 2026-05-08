"""Application WebSocket broadcast manager.

FastAPI owns the event loop, while the bot runner does blocking exchange work
in thread-pool workers.  This module keeps one injectable manager that can be
used from both async code and those worker threads.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any

from fastapi import WebSocket
from pydantic import BaseModel, Field


def _now_ms() -> int:
    return int(time.time() * 1000)


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

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Bind the FastAPI loop so worker threads can schedule broadcasts."""
        self._loop = loop or asyncio.get_running_loop()

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
    ) -> None:
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
            except Exception:
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
    ) -> None:
        """Thread-safe fire-and-forget broadcast.

        Synchronous bot-runner code calls this from worker threads.  Async code
        running on the FastAPI loop gets a cheap create_task path.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        coro = self.broadcast(channel, event_type, data)
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
) -> None:
    ws_manager.publish("bots", event_type, data)


def publish_bot_event(
    bot_id: str,
    event_type: str,
    data: dict[str, Any] | None = None,
    *,
    include_overview: bool = True,
) -> None:
    payload = {"bot_id": bot_id, **(data or {})}
    ws_manager.publish(f"bot:{bot_id}", event_type, payload)
    if include_overview:
        ws_manager.publish("bots", event_type, payload)


def publish_market_event(
    event_type: str,
    data: dict[str, Any] | None = None,
) -> None:
    ws_manager.publish("market", event_type, data)
