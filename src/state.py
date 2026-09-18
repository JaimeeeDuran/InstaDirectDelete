"""Estado persistente: mensajes ya anulados y cupo diario consumido.

Permite reanudar tras un corte sin repetir trabajo y respetar el límite
diario aunque lances el proceso varias veces en el mismo día.
"""
from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path
from typing import Dict, Set


class State:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.deleted: Set[str] = set()
        self.daily_counts: Dict[str, int] = {}
        self.thread_cursors: Dict[str, str] = {}  # thread_id -> cursor
        self.thread_complete: Set[str] = set()    # hilos recorridos hasta el fondo
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.deleted = set(data.get("deleted", []))
            self.daily_counts = dict(data.get("daily_counts", {}))
            self.thread_cursors = dict(data.get("thread_cursors", {}))
            self.thread_complete = set(data.get("thread_complete", []))
        except (json.JSONDecodeError, OSError):
            self.deleted = set()
            self.daily_counts = {}
            self.thread_cursors = {}
            self.thread_complete = set()

    def save(self, force: bool = False) -> None:
        with self._lock:
            if not self._dirty and not force:
                return
            payload = {
                "deleted": sorted(self.deleted),
                "daily_counts": self.daily_counts,
                "thread_cursors": self.thread_cursors,
                "thread_complete": sorted(self.thread_complete),
                "updated_at": date.today().isoformat(),
            }
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.path)  # atómico: no deja el archivo a medias
            self._dirty = False

    @staticmethod
    def key(thread_id: str, item_id: str) -> str:
        return f"{thread_id}:{item_id}"

    def is_done(self, thread_id: str, item_id: str) -> bool:
        return self.key(thread_id, item_id) in self.deleted

    def mark_done(self, thread_id: str, item_id: str) -> None:
        with self._lock:
            self.deleted.add(self.key(thread_id, item_id))
            today = date.today().isoformat()
            self.daily_counts[today] = self.daily_counts.get(today, 0) + 1
            self._dirty = True

    def count_today(self) -> int:
        return self.daily_counts.get(date.today().isoformat(), 0)

    def remaining_today(self, daily_limit: int) -> int:
        if daily_limit <= 0:
            return 10**9  # sin límite
        return max(0, daily_limit - self.count_today())

    def snapshot(self, daily_limit: int) -> Dict[str, object]:
        return {
            "total_deleted_ever": len(self.deleted),
            "deleted_today": self.count_today(),
            "daily_limit": daily_limit,
            "remaining_today": self.remaining_today(daily_limit),
            "history": dict(sorted(self.daily_counts.items(), reverse=True)[:14]),
        }

    # ----- Gestión de cursores (paginación) -----

    def get_cursor(self, thread_id: str) -> str:
        """Devuelve el último cursor guardado para este hilo, o cadena vacía."""
        return self.thread_cursors.get(thread_id, "")

    def set_cursor(self, thread_id: str, cursor: str) -> None:
        """Guarda el cursor para reanudar desde aquí la próxima vez."""
        with self._lock:
            if cursor:
                self.thread_cursors[thread_id] = cursor
            elif thread_id in self.thread_cursors:
                del self.thread_cursors[thread_id]
            self._dirty = True

    def is_thread_complete(self, thread_id: str) -> bool:
        """Devuelve True si este hilo ya ha sido recorrido hasta el fondo."""
        return thread_id in self.thread_complete

    def mark_thread_complete(self, thread_id: str) -> None:
        """Marca este hilo como completamente recorrido."""
        with self._lock:
            self.thread_complete.add(thread_id)
            # Limpiar el cursor guardado: ya no lo necesitamos.
            if thread_id in self.thread_cursors:
                del self.thread_cursors[thread_id]
            self._dirty = True

    def reset_cursors(self) -> None:
        """Limpia todos los cursores para forzar un repaso completo de todos los hilos.

        Útil desde el dashboard cuando el usuario quiere volver a paginar
        desde cero (por ejemplo, si sospecha que algo se saltó).
        """
        with self._lock:
            self.thread_cursors.clear()
            self.thread_complete.clear()
            self._dirty = True
