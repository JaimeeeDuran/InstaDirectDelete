"""Orquestador: arranca el motor en un hilo y expone control sobre él.

Lo usan tanto la CLI como el dashboard, así que el comportamiento es idéntico
se lance desde donde se lance.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from .audit import AuditLog
from .config import Config, load_config
from .engine import Controller, Engine, EngineState
from .events import EventBus
from .filters import FilterEngine
from .state import State
from .stats import Stats

log = logging.getLogger("ig-unsender.runner")


class EngineRunner:
    """Ciclo de vida de una ejecución. Solo una activa a la vez."""

    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = config_path
        self.bus = EventBus()
        self.stats = Stats()
        self.ctl = Controller(self.bus)
        self.cfg: Config = load_config(config_path)
        self._thread: Optional[threading.Thread] = None
        self._engine: Optional[Engine] = None
        self._client: Any = None          # se reutiliza entre ejecuciones
        self._lock = threading.Lock()

    # ------------------------------------------------------------- config
    def reload_config(self) -> Config:
        self.cfg = load_config(self.config_path)
        return self.cfg

    def config_warnings(self) -> list[str]:
        try:
            return FilterEngine(self.cfg).validate()
        except Exception as exc:  # noqa: BLE001
            return [f"No se pudo validar la configuración: {exc}"]

    # ------------------------------------------------------------- control
    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, dry_run: Optional[bool] = None) -> Dict[str, Any]:
        with self._lock:
            if self.busy:
                return {"ok": False, "error": "Ya hay una ejecución en marcha."}

            self.reload_config()
            if dry_run is not None:
                self.cfg.behavior.dry_run = dry_run

            self.ctl.reset()
            self._engine = Engine(self.cfg, self.bus, self.stats, self.ctl,
                                  client=self._client)

            def _target() -> None:
                try:
                    self._engine.run()  # type: ignore[union-attr]
                except Exception as exc:  # noqa: BLE001
                    log.exception("Fallo no controlado en el motor")
                    self.bus.log("ERROR", f"Fallo no controlado: {exc}")
                    self.ctl.set_state(EngineState.ERROR, str(exc))
                finally:
                    # Guardamos el cliente para no re-loguearnos en la siguiente.
                    if self._engine is not None:
                        self._client = self._engine.cl

            self._thread = threading.Thread(target=_target, name="ig-engine", daemon=True)
            self._thread.start()
            return {"ok": True, "run_id": self.ctl.run_id,
                    "dry_run": self.cfg.behavior.dry_run}

    def pause(self) -> Dict[str, Any]:
        if not self.busy:
            return {"ok": False, "error": "No hay ninguna ejecución en marcha."}
        self.ctl.pause()
        return {"ok": True}

    def resume(self) -> Dict[str, Any]:
        if not self.busy:
            return {"ok": False, "error": "No hay ninguna ejecución en marcha."}
        self.ctl.resume()
        return {"ok": True}

    def stop(self) -> Dict[str, Any]:
        if not self.busy:
            return {"ok": False, "error": "No hay ninguna ejecución en marcha."}
        self.ctl.stop()
        return {"ok": True}

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # ------------------------------------------------------------- lectura
    def status(self) -> Dict[str, Any]:
        state = State(self.cfg.path(self.cfg.state.file))
        audit = AuditLog(self.cfg.path(self.cfg.audit.file))
        return {
            "state": self.ctl.state.value,
            "busy": self.busy,
            "run_id": self.ctl.run_id,
            "error": self.ctl.error,
            "dry_run": self.cfg.behavior.dry_run,
            "account": self.cfg.account.username,
            "stats": self.stats.snapshot(),
            "quota": state.snapshot(self.cfg.behavior.daily_limit),
            "audit_size": audit.size_bytes,
            "warnings": self.config_warnings(),
            "rules_active": sum(1 for r in self.cfg.rules.items if r.enabled),
            "rules_total": len(self.cfg.rules.items),
        }
