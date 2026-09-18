"""Métricas en vivo de la ejecución."""
from __future__ import annotations

import threading
import time
from collections import Counter
from typing import Any, Dict


class Stats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.started_at: float | None = None
            self.finished_at: float | None = None
            self.threads_total = 0
            self.threads_done = 0
            self.scanned = 0        # mensajes míos examinados
            self.deleted = 0        # anulados de verdad
            self.would_delete = 0   # marcados para borrar en dry-run
            self.protected = 0      # salvados por la whitelist
            self.kept = 0           # descartados por reglas
            self.already_done = 0   # ya anulados en ejecuciones previas
            self.errors = 0
            self.consecutive_errors = 0
            self.by_type: Counter = Counter()      # tipo -> nº marcados
            self.by_thread: Counter = Counter()    # hilo -> nº marcados
            self.current_thread = ""
            self.last_action = ""

    # -- mutadores ---------------------------------------------------------
    def start(self, threads_total: int = 0) -> None:
        with self._lock:
            self.started_at = time.time()
            self.finished_at = None
            self.threads_total = threads_total

    def finish(self) -> None:
        with self._lock:
            self.finished_at = time.time()

    def bump(self, field: str, amount: int = 1) -> None:
        with self._lock:
            setattr(self, field, getattr(self, field) + amount)

    def record_target(self, msg_type: str, thread_title: str) -> None:
        with self._lock:
            self.by_type[msg_type] += 1
            self.by_thread[thread_title] += 1

    def record_error(self) -> int:
        with self._lock:
            self.errors += 1
            self.consecutive_errors += 1
            return self.consecutive_errors

    def clear_error_streak(self) -> None:
        with self._lock:
            self.consecutive_errors = 0

    def set_current_thread(self, title: str) -> None:
        with self._lock:
            self.current_thread = title

    def set_last_action(self, text: str) -> None:
        with self._lock:
            self.last_action = text

    # -- lectura -----------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            elapsed = 0.0
            if self.started_at:
                end = self.finished_at or time.time()
                elapsed = end - self.started_at
            rate = (self.deleted / elapsed * 60) if elapsed > 0 and self.deleted else 0.0
            return {
                "elapsed_seconds": round(elapsed, 1),
                "rate_per_minute": round(rate, 1),
                "threads_total": self.threads_total,
                "threads_done": self.threads_done,
                "scanned": self.scanned,
                "deleted": self.deleted,
                "would_delete": self.would_delete,
                "protected": self.protected,
                "kept": self.kept,
                "already_done": self.already_done,
                "errors": self.errors,
                "by_type": dict(self.by_type.most_common()),
                "by_thread": dict(self.by_thread.most_common(12)),
                "current_thread": self.current_thread,
                "last_action": self.last_action,
            }
