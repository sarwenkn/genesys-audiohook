import asyncio
import json
import time
from collections import deque
from typing import Any, Deque, Dict, Optional, Set


class DebugHub:
    """
    In-memory pub/sub for debug UI.

    Keeps a small ring buffer of recent events and broadcasts to connected
    WebSocket clients (browser dashboard).
    """

    def __init__(self, max_events: int = 500):
        self._clients: Set[Any] = set()
        self._events: Deque[Dict[str, Any]] = deque(maxlen=max_events)
        self._lock = asyncio.Lock()

    async def register(self, websocket: Any) -> None:
        async with self._lock:
            self._clients.add(websocket)
            backlog = list(self._events)
            client_count = len(self._clients)
            backlog_count = len(backlog)
            now = time.time()
            hello_evt: Dict[str, Any] = {
                "ts": now,
                "type": "debug_connected",
                "payload": {
                    "client_count": client_count,
                    "backlog_events": backlog_count,
                    "server_ts": now,
                },
            }
            self._events.append(hello_evt)
        for evt in backlog:
            try:
                await websocket.send(json.dumps(evt))
            except Exception:
                break
        try:
            await websocket.send(json.dumps(hello_evt))
        except Exception:
            return

    async def unregister(self, websocket: Any) -> None:
        async with self._lock:
            self._clients.discard(websocket)

    async def publish(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        evt: Dict[str, Any] = {
            "ts": time.time(),
            "type": event_type,
            "payload": payload or {},
        }
        async with self._lock:
            self._events.append(evt)
            clients = list(self._clients)

        if not clients:
            return

        msg = json.dumps(evt)
        dead = []
        for ws in clients:
            try:
                await ws.send(msg)
            except Exception:
                dead.append(ws)

        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)
