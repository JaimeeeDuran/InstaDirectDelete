"""Ciclo de vida de un escaneo y bus de eventos para el panel.

Más simple que el motor de ig-unsender a propósito: aquí no se borra nada, así
que no hace falta pausar/reanudar ni una máquina de estados completa. Basta
con "está escaneando o no", poder pararlo, y un hilo de progreso legible.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from queue import Empty, Queue
from typing import Any, Deque, Dict, List, Optional

from .analysis import analyze, serie_historica
from .client import build_client
from .config import Config, load_config
from .fetch import escanear
from .snapshots import SnapshotStore

log = logging.getLogger("ig-espejo.runner")

MAX_QUEUE = 1000


class EventBus:
    """Bus pub/sub en memoria, con historial acotado para quien llega tarde."""

    def __init__(self, history: int = 200):
        self._subs: List[Queue] = []
        self._history: Deque[Dict[str, Any]] = deque(maxlen=history)
        self._lock = threading.Lock()
        self._seq = 0

    def publish(self, kind: str, **payload: Any) -> Dict[str, Any]:
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "kind": kind, "ts": time.time(), **payload}
            self._history.append(event)
            muertos = []
            for q in self._subs:
                try:
                    if q.qsize() < MAX_QUEUE:
                        q.put_nowait(event)
                    else:
                        muertos.append(q)  # cliente colgado: lo soltamos
                except Exception:  # noqa: BLE001
                    muertos.append(q)
            for q in muertos:
                if q in self._subs:
                    self._subs.remove(q)
        return event

    def log(self, level: str, message: str, **extra: Any) -> None:
        getattr(log, level.lower(), log.info)(message)
        self.publish("log", level=level.upper(), message=message, **extra)

    def subscribe(self) -> Queue:
        q: Queue = Queue()
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def history(self, limit: int = 120) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._history)[-limit:]

    def drain(self, q: Queue, max_items: int = 100) -> List[Dict[str, Any]]:
        salida = []
        for _ in range(max_items):
            try:
                salida.append(q.get_nowait())
            except Empty:
                break
        return salida


class ScanRunner:
    """Lanza escaneos en un hilo y guarda el cliente entre ejecuciones."""

    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = config_path
        self.bus = EventBus()
        self.cfg: Config = load_config(config_path)
        self._thread: Optional[threading.Thread] = None
        self._client: Any = None       # se reutiliza: no re-loguearse
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.state = "idle"            # idle | conectando | escaneando | listo | error | detenido
        self.error = ""

    # ------------------------------------------------------------- config
    def reload_config(self) -> Config:
        self.cfg = load_config(self.config_path)
        return self.cfg

    def store(self) -> SnapshotStore:
        return SnapshotStore(
            self.cfg.path(self.cfg.snapshots.dir), keep=self.cfg.snapshots.keep
        )

    # ------------------------------------------------------------ control
    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _set_state(self, state: str, error: str = "") -> None:
        self.state = state
        if error:
            self.error = error
        self.bus.publish("status", state=state, error=self.error)

    def start(self, deep: Optional[bool] = None) -> Dict[str, Any]:
        with self._lock:
            if self.busy:
                return {"ok": False, "error": "Ya hay un escaneo en marcha."}

            self.reload_config()
            if deep is not None:
                self.cfg.scan.deep_scan = deep
            self._stop.clear()
            self.error = ""

            def _target() -> None:
                try:
                    self._set_state("conectando")
                    self.bus.log("INFO", "Conectando con Instagram…")
                    if self._client is None:
                        self._client = build_client(self.cfg)

                    self._set_state("escaneando")
                    snapshot = escanear(
                        self._client,
                        self.cfg,
                        on_progress=self.bus.log,
                        should_continue=lambda: not self._stop.is_set(),
                    )

                    store = self.store()
                    anterior = store.latest()          # el último guardado, aún sin el nuevo
                    historico = store.all()[:-1] if store.count() >= 2 else []
                    store.save(snapshot)

                    rep = analyze(snapshot, anterior, historico, self.cfg)
                    self.bus.publish("report", **rep.to_dict())
                    self._resumen(rep)
                    self._set_state("listo")
                except InterruptedError as exc:
                    self.bus.log("WARNING", str(exc))
                    self._set_state("detenido")
                except Exception as exc:  # noqa: BLE001
                    log.exception("Fallo no controlado en el escaneo")
                    self.bus.log("ERROR", f"Fallo en el escaneo: {exc}")
                    self._set_state("error", str(exc))

            self._thread = threading.Thread(target=_target, name="ig-espejo-scan", daemon=True)
            self._thread.start()
            return {"ok": True, "deep": self.cfg.scan.deep_scan}

    def stop(self) -> Dict[str, Any]:
        if not self.busy:
            return {"ok": False, "error": "No hay ningún escaneo en marcha."}
        self._stop.set()
        self.bus.log("WARNING", "Parada solicitada. Terminando de forma ordenada…")
        return {"ok": True}

    def _resumen(self, rep) -> None:
        d = rep.to_dict()
        t, m = d["totales"], d["metricas"]
        self.bus.log(
            "INFO",
            f"✔ {t['seguidores']} seguidores · {t['siguiendo']} siguiendo · "
            f"{t['mutuos']} mutuos ({m['reciprocidad']}% de reciprocidad) · "
            f"{t['no_te_siguen']} no te siguen · {t['sospechosos']} sospechosos.",
        )

    # ------------------------------------------------------------ lectura
    def last_report(self) -> Optional[Dict[str, Any]]:
        store = self.store()
        actual = store.latest()
        if not actual:
            return None
        historico = store.all()
        anterior = historico[-2] if len(historico) >= 2 else None
        previos = historico[:-2] if len(historico) >= 3 else []
        return analyze(actual, anterior, previos, self.cfg).to_dict()

    def history(self) -> List[Dict[str, Any]]:
        return serie_historica(self.store().all())

    def status(self) -> Dict[str, Any]:
        store = self.store()
        ultimo = store.latest()
        return {
            "state": self.state,
            "busy": self.busy,
            "error": self.error,
            "cuenta": self.cfg.account.username,
            "objetivo": self.cfg.scan.target or self.cfg.account.username,
            "deep_scan": self.cfg.scan.deep_scan,
            "snapshots": store.count(),
            "ultimo_escaneo": (ultimo or {}).get("ts", ""),
        }
