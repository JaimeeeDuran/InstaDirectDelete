"""Bus de eventos en memoria para alimentar el dashboard en tiempo real.

El motor publica; el endpoint SSE del servidor web consume. Thread-safe,
con historial acotado para que un cliente que se conecta tarde vea contexto.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from queue import Empty, Queue
from typing import Any, Deque, Dict, List

MAX_QUEUE = 2000


class EventBus:
    def __init__(self, history: int = 400):
        self._subscribers: List[Queue] = []
        self._history: Deque[Dict[str, Any]] = deque(maxlen=history)
        self._lock = threading.Lock()
        self._seq = 0

    # -- publicación -------------------------------------------------------
    def publish(self, kind: str, **payload: Any) -> Dict[str, Any]:
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "kind": kind, "ts": time.time(), **payload}
            self._history.append(event)
            dead: List[Queue] = []
            for q in self._subscribers:
                try:
                    if q.qsize() < MAX_QUEUE:
                        q.put_nowait(event)
                    else:
                        dead.append(q)  # cliente colgado: lo soltamos
                except Exception:  # noqa: BLE001
                    dead.append(q)
            for q in dead:
                if q in self._subscribers:
                    self._subscribers.remove(q)
        return event

    def log(self, level: str, message: str, **extra: Any) -> None:
        self.publish("log", level=level.upper(), message=message, **extra)

    # -- suscripción -------------------------------------------------------
    def subscribe(self) -> Queue:
        q: Queue = Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def history(self, kinds: List[str] | None = None, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._history)
        if kinds:
            items = [e for e in items if e["kind"] in kinds]
        return items[-limit:]

    def drain(self, q: Queue, max_items: int = 100) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for _ in range(max_items):
            try:
                out.append(q.get_nowait())
            except Empty:
                break
        return out

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)
