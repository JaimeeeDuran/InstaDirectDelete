"""Motor de ejecución: recorre hilos, decide con el FilterEngine y anula.

Diseñado para ser controlable desde fuera (dashboard o CLI): se puede pausar,
reanudar y parar en cualquier punto, y todo lo que hace queda auditado.
"""
from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from .audit import AuditLog
from .client import build_client
from .config import Config
from .events import EventBus
from .filters import DELETE, PROTECTED, Decision, FilterEngine, MessageCtx, _thread_title
from .state import State
from .stats import Stats

log = logging.getLogger("ig-unsender.engine")


def _lazy_import_pagination():
    """Importa iter_with_fallback bajo demanda, no al cargar el módulo.

    pagination.py ya no exige instagrapi instalado (tiene su propio
    extractor de respaldo), pero seguimos el mismo patrón perezoso que el
    resto del proyecto por si el módulo falta o cambia en el futuro.
    """
    try:
        from .pagination import iter_with_fallback
        return iter_with_fallback
    except ImportError:
        return None

# Excepciones de instagrapi que significan "vas muy rápido, frena".
RATE_LIMIT_HINTS = ("please wait", "ratelimit", "rate limit", "429", "throttle", "spam")


class EngineState(str, Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    FINISHED = "finished"
    STOPPED = "stopped"
    ERROR = "error"


class Controller:
    """Semáforo compartido entre el motor y quien lo gobierna."""

    def __init__(self, bus: EventBus):
        self.bus = bus
        self._resume = threading.Event()
        self._resume.set()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.state = EngineState.IDLE
        self.error: str = ""
        self.run_id: str = ""

    def set_state(self, state: EngineState, error: str = "") -> None:
        with self._lock:
            self.state = state
            if error:
                self.error = error
        self.bus.publish("status", state=state.value, error=self.error, run_id=self.run_id)

    def pause(self) -> None:
        self._resume.clear()
        self.set_state(EngineState.PAUSED)
        self.bus.log("WARNING", "⏸  Pausado por el usuario.")

    def resume(self) -> None:
        self._resume.set()
        self.set_state(EngineState.RUNNING)
        self.bus.log("INFO", "▶  Reanudado.")

    def stop(self) -> None:
        self._stop.set()
        self._resume.set()  # desbloquea si estaba en pausa
        self.set_state(EngineState.STOPPING)
        self.bus.log("WARNING", "⏹  Parada solicitada. Terminando de forma ordenada…")

    def reset(self) -> None:
        self._stop.clear()
        self._resume.set()
        self.error = ""
        self.run_id = uuid.uuid4().hex[:12]

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def checkpoint(self) -> bool:
        """Bloquea mientras esté en pausa. Devuelve False si toca parar."""
        while not self._resume.wait(timeout=0.5):
            if self._stop.is_set():
                return False
        return not self._stop.is_set()

    def sleep(self, seconds: float) -> bool:
        """Espera interrumpible. Devuelve False si toca parar."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._stop.is_set():
                return False
            time.sleep(min(0.25, max(0.0, deadline - time.time())))
        return not self._stop.is_set()


class Engine:
    def __init__(
        self,
        cfg: Config,
        bus: EventBus,
        stats: Stats,
        controller: Controller,
        client: Any = None,
    ):
        self.cfg = cfg
        self.bus = bus
        self.stats = stats
        self.ctl = controller
        self.cl = client
        self.filters = FilterEngine(cfg)
        self.state = State(cfg.path(cfg.state.file))
        self.audit = AuditLog(cfg.path(cfg.audit.file))
        self.me_id = ""
        self._since_pause = 0

    # ------------------------------------------------------------------ util
    def _emit(self, level: str, message: str) -> None:
        getattr(log, level.lower(), log.info)(message)
        self.bus.log(level, message)

    def _audit(self, action: str, ctx: Optional[MessageCtx], decision: Optional[Decision],
               error: str = "") -> None:
        if action in ("kept", "protected") and not self.cfg.audit.record_keeps:
            return
        self.audit.record(
            run_id=self.ctl.run_id,
            action=action,
            reason=decision.reason if decision else "",
            rule=decision.rule if decision else "",
            dry_run=self.cfg.behavior.dry_run,
            thread_id=ctx.thread_id if ctx else "",
            thread_title=ctx.thread_title if ctx else "",
            message_id=ctx.message_id if ctx else "",
            message_type=ctx.msg_type if ctx else "",
            message_ts=ctx.timestamp.isoformat() if ctx and ctx.timestamp else "",
            preview=ctx.preview if ctx else "",
            error=error or None,
        )

    def _decision_event(self, ctx: MessageCtx, action: str, decision: Decision) -> None:
        self.bus.publish(
            "decision",
            action=action,
            reason=decision.reason,
            rule=decision.rule,
            thread_id=ctx.thread_id,
            thread=ctx.thread_title,
            message_id=ctx.message_id,
            type=ctx.msg_type,
            preview=ctx.preview,
            ts_message=ctx.timestamp.isoformat() if ctx.timestamp else "",
        )

    # ------------------------------------------------------------- descarga
    def _fetch_threads(self) -> List[Any]:
        scope = self.cfg.scope
        amount = scope.threads_amount or 0
        threads = list(self.cl.direct_threads(amount=amount) or [])

        if scope.include_requests:
            try:
                threads += list(self.cl.direct_pending_inbox(amount=amount) or [])
            except Exception as exc:  # noqa: BLE001
                self._emit("WARNING", f"No se pudo leer la bandeja de solicitudes: {exc}")

        only = {str(t).strip() for t in scope.only_thread_ids if str(t).strip()}
        skip = {str(t).strip() for t in scope.skip_thread_ids if str(t).strip()}
        if only:
            threads = [t for t in threads if str(t.id) in only]
        if skip:
            threads = [t for t in threads if str(t.id) not in skip]
        return threads

    def _iter_pages(self, thread_id: str) -> Any:
        """Generador que consume páginas del hilo, de forma incremental.

        Yields:
            Tuplas (mensajes_página, cursor_siguiente, hay_más, paginando).
            `paginando` es True solo cuando se usó paginación de verdad
            (no cuando cayó al fallback direct_messages()), para que
            _process() sepa si tiene sentido marcar el hilo como completo.

        Si use_pagination está desactivado o falla, cae a direct_messages().
        """
        scope = self.cfg.scope

        # Si paginación está desactivada, usa el método antiguo.
        if not scope.use_pagination:
            amount = scope.messages_per_thread or 0
            messages = list(self.cl.direct_messages(thread_id, amount=amount) or [])
            yield messages, "", False, False
            return

        # Intentar paginación. Si no está disponible, cae a directo.
        iter_with_fallback = _lazy_import_pagination()
        if iter_with_fallback is None:
            amount = scope.messages_per_thread or 0
            messages = list(self.cl.direct_messages(thread_id, amount=amount) or [])
            yield messages, "", False, False
            return

        # Paginación explícita con cursor.
        start_cursor = ""
        if scope.resume_cursors:
            # Si ya habíamos empezado este hilo, reanudar desde donde nos quedamos.
            start_cursor = self.state.get_cursor(thread_id)
            if self.state.is_thread_complete(thread_id):
                # Ya completamos este hilo en una ejecución anterior.
                log.debug(f"Hilo {thread_id} ya completado en ejecución previa. Saltando.")
                return

        # Sin try/except aquí a propósito: si algo falla, la excepción se
        # propaga tal cual hasta el `except` de _process(), que ya la
        # registra y cuenta como error. Capturarla aquí también duplicaría
        # el conteo y el mensaje de log.
        for messages, next_cursor, hay_mas, es_real in iter_with_fallback(
            self.cl,
            thread_id,
            page_size=scope.page_size,
            max_pages=scope.max_pages_per_thread,
            start_cursor=start_cursor,
            should_continue=lambda: not self.ctl.stopping,
        ):
            yield messages, next_cursor, hay_mas, es_real

    # ------------------------------------------------------------- borrado
    def _delete_with_retry(self, ctx: MessageCtx) -> bool:
        b = self.cfg.behavior
        for attempt in range(1, max(1, b.max_retries) + 1):
            if self.ctl.stopping:
                return False
            try:
                self.cl.direct_message_delete(ctx.thread_id, ctx.message_id)
                self.stats.clear_error_streak()
                return True
            except Exception as exc:  # noqa: BLE001
                text = str(exc).lower()
                is_rate = any(hint in text for hint in RATE_LIMIT_HINTS)
                if attempt >= b.max_retries:
                    self._emit("ERROR", f"Fallo definitivo en {ctx.message_id}: {exc}")
                    self._audit("error", ctx, None, error=str(exc))
                    streak = self.stats.record_error()
                    if b.stop_on_consecutive_errors and streak >= b.stop_on_consecutive_errors:
                        self._emit(
                            "ERROR",
                            f"{streak} errores seguidos. Paro por seguridad: "
                            "puede ser un bloqueo temporal de Instagram.",
                        )
                        self.ctl.stop()
                    return False

                if is_rate:
                    wait = b.rate_limit_cooldown
                    self._emit("WARNING", f"Rate-limit detectado. Enfriando {wait:.0f}s…")
                else:
                    wait = b.backoff_base * (2 ** (attempt - 1)) * random.uniform(0.8, 1.2)
                    self._emit(
                        "WARNING",
                        f"Error en {ctx.message_id} (intento {attempt}/{b.max_retries}): "
                        f"{exc}. Reintento en {wait:.0f}s.",
                    )
                if not self.ctl.sleep(wait):
                    return False
        return False

    def _cooldown(self) -> bool:
        b = self.cfg.behavior
        self._since_pause += 1
        if b.pause_every and self._since_pause % b.pause_every == 0:
            delay = random.uniform(b.long_pause_min, b.long_pause_max)
            self._emit("INFO", f"Pausa larga de {delay:.0f}s (cada {b.pause_every} anulados).")
        else:
            delay = random.uniform(b.min_delay, b.max_delay)
        return self.ctl.sleep(delay)

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        b = self.cfg.behavior
        self.stats.reset()

        warnings = self.filters.validate()
        for w in warnings:
            self._emit("WARNING", f"Config: {w}")

        try:
            if self.cl is None:
                self.ctl.set_state(EngineState.CONNECTING)
                self._emit("INFO", "Conectando con Instagram…")
                self.cl = build_client(self.cfg)
            self.me_id = str(self.cl.user_id)
        except Exception as exc:  # noqa: BLE001
            self._emit("ERROR", f"No se pudo conectar: {exc}")
            self.ctl.set_state(EngineState.ERROR, str(exc))
            return

        mode = "SIMULACIÓN (no se borra nada)" if b.dry_run else "BORRADO REAL"
        self._emit("INFO", f"Sesión iniciada como user_id={self.me_id}. Modo: {mode}.")
        self.ctl.set_state(EngineState.RUNNING)

        try:
            threads = self._fetch_threads()
        except Exception as exc:  # noqa: BLE001
            self._emit("ERROR", f"No se pudieron listar los chats: {exc}")
            self.ctl.set_state(EngineState.ERROR, str(exc))
            return

        self.stats.start(threads_total=len(threads))
        self._emit("INFO", f"{len(threads)} chats en el alcance.")

        try:
            self._process(threads)
        finally:
            self.state.save(force=True)
            self.stats.finish()
            self.bus.publish("stats", **self.stats.snapshot())

        if self.ctl.stopping:
            self.ctl.set_state(EngineState.STOPPED)
            self._emit("WARNING", "Ejecución detenida. El progreso queda guardado.")
        else:
            self.ctl.set_state(EngineState.FINISHED)
            snap = self.stats.snapshot()
            verb = "se habrían anulado" if b.dry_run else "anulados"
            self._emit(
                "INFO",
                f"✔ Terminado. {snap['would_delete'] if b.dry_run else snap['deleted']} "
                f"mensajes {verb}, {snap['protected']} protegidos, "
                f"{snap['kept']} descartados, {snap['errors']} errores.",
            )

    def _process(self, threads: List[Any]) -> None:
        b = self.cfg.behavior

        for thread in threads:
            if not self.ctl.checkpoint():
                return

            tid = str(thread.id)
            title = _thread_title(thread)
            page_num = 0

            self.stats.set_current_thread(title or tid)
            self.bus.publish("thread", thread_id=tid, title=title or tid)

            try:
                # Consumir páginas del hilo, una a una.
                for page_messages, next_cursor, hay_mas, es_real in self._iter_pages(tid):
                    if not self.ctl.checkpoint():
                        return

                    page_num += 1

                    # Procesar cada página.
                    candidates: List[MessageCtx] = []
                    for msg in page_messages:
                        if str(getattr(msg, "user_id", "")) != self.me_id:
                            continue  # solo mis mensajes; los ajenos ni se tocan
                        ctx = MessageCtx.build(msg, thread)
                        title = ctx.thread_title
                        candidates.append(ctx)

                    # Publicar evento de progreso dentro del hilo.
                    if hay_mas or page_num > 1:
                        self.bus.publish(
                            "page",
                            thread_id=tid,
                            page_num=page_num,
                            fetched=len(candidates),
                            has_more=hay_mas,
                        )

                    self.stats.set_current_thread(title or tid)

                    # Procesar los mensajes de esta página.
                    for ctx in candidates:
                        if not self.ctl.checkpoint():
                            return

                        self.stats.bump("scanned")

                        if self.state.is_done(ctx.thread_id, ctx.message_id):
                            self.stats.bump("already_done")
                            continue

                        decision = self.filters.decide(ctx)

                        if decision.action == PROTECTED:
                            self.stats.bump("protected")
                            self._decision_event(ctx, "protected", decision)
                            self._audit("protected", ctx, decision)
                            continue

                        if decision.action != DELETE:
                            self.stats.bump("kept")
                            self._decision_event(ctx, "kept", decision)
                            self._audit("kept", ctx, decision)
                            continue

                        # --- a partir de aquí, el mensaje está marcado para anular ---
                        self.stats.record_target(ctx.msg_type, ctx.thread_title)

                        if b.dry_run:
                            self.stats.bump("would_delete")
                            self.stats.set_last_action(f"[SIM] {ctx.preview}")
                            self._decision_event(ctx, "would_delete", decision)
                            self._audit("would_delete", ctx, decision)
                            continue

                        if self.state.remaining_today(b.daily_limit) <= 0:
                            self._emit(
                                "WARNING",
                                f"Cupo diario agotado ({b.daily_limit}). "
                                "Se reanudará mañana desde este punto.",
                            )
                            # Guardar el cursor donde nos quedamos (solo si es paginación real).
                            if es_real and next_cursor:
                                self.state.set_cursor(tid, next_cursor)
                            self.state.save(force=True)
                            return

                        if self._delete_with_retry(ctx):
                            self.state.mark_done(ctx.thread_id, ctx.message_id)
                            self.state.save()
                            self.stats.bump("deleted")
                            self.stats.set_last_action(ctx.preview)
                            self._decision_event(ctx, "deleted", decision)
                            self._audit("deleted", ctx, decision)
                            self.bus.publish("stats", **self.stats.snapshot())
                            if not self._cooldown():
                                return

                    # Guardar el cursor después de procesar la página completa.
                    # Solo confiamos en "fin de hilo" cuando fue paginación real:
                    # el fallback direct_messages() no garantiza haber llegado
                    # al fondo (es justo el problema que la Mejora 1 resuelve).
                    if es_real:
                        if next_cursor:
                            self.state.set_cursor(tid, next_cursor)
                        else:
                            # No hay más cursor: hemos llegado al fondo.
                            self.state.mark_thread_complete(tid)
                        self.state.save()

            except Exception as exc:  # noqa: BLE001
                self._emit("ERROR", f"No se pudieron leer los mensajes del hilo {tid}: {exc}")
                self.stats.bump("errors")
                continue

            self.stats.bump("threads_done")
            self.bus.publish("stats", **self.stats.snapshot())

    # ---------------------------------------------------------- utilidades
    def list_threads(self) -> List[Dict[str, Any]]:
        """Inventario de chats, para construir whitelists desde el dashboard."""
        if self.cl is None:
            self.cl = build_client(self.cfg)
            self.me_id = str(self.cl.user_id)
        out = []
        for t in self._fetch_threads():
            users = getattr(t, "users", None) or []
            out.append({
                "id": str(t.id),
                "title": getattr(t, "thread_title", "") or ", ".join(
                    str(getattr(u, "username", "?")) for u in users[:3]
                ),
                "usernames": [str(getattr(u, "username", "")) for u in users],
                "is_group": bool(getattr(t, "is_group", False)),
            })
        return out
