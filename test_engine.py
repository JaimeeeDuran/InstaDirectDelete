#!/usr/bin/env python3
"""Prueba de integración del motor con un Instagram falso.

Verifica el pipeline completo sin tocar la red: recorrido de chats, decisiones,
simulación vs borrado real, whitelist, cupo diario, estado, auditoría y eventos.

  python test_engine.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))

from src.audit import AuditLog  # noqa: E402
from src.config import load_config  # noqa: E402
from src.engine import Controller, Engine  # noqa: E402
from src.events import EventBus  # noqa: E402
from src.stats import Stats  # noqa: E402

PASS, FAIL = "\033[92m✓\033[0m", "\033[91m✗\033[0m"
results = []


def check(name, got, want):
    ok = got == want
    results.append(ok)
    print(f"  {PASS if ok else FAIL} {name}" + ("" if ok else f"\n      esperado={want!r} obtenido={got!r}"))


ME = "42"


class FakeClient:
    """Imita la superficie de instagrapi que usa el motor."""

    def __init__(self, enable_pagination=False, include_long_thread=False):
        self.user_id = ME
        self.deleted = []
        self.enable_pagination = enable_pagination
        self._threads = [
            self._thread("t1", "Amigo", ["colega"], [
                ("m1", "text", "hola qué tal", ME),
                ("m2", "text", "mi iban es ES12 3456", ME),
                ("m3", "voice_media", "", ME),
                ("m4", "text", "respuesta del otro", "999"),   # no es mío
            ]),
            self._thread("t2", "Pareja", ["mi_pareja"], [
                ("m5", "text", "te espero abajo", ME),
                ("m6", "text", "mi iban es ES99", ME),          # protegido por usuario
            ]),
            self._thread("t3", "Grupo curro", ["a", "b"], [
                ("m7", "text", "buenos días", ME),
            ], is_group=True),
        ]
        if include_long_thread:
            # Hilo largo con ~55 mensajes para probar paginación con page_size=20.
            self._threads.append(self._thread("t4", "Hilo largo", ["alice"], [
                (f"long_{i:03d}", "text", f"mensaje {i}", ME)
                for i in range(55)
            ]))

    @staticmethod
    def _thread(tid, title, users, msgs, is_group=False):
        return SimpleNamespace(
            id=tid, thread_title=title, is_group=is_group,
            users=[SimpleNamespace(username=u) for u in users],
            messages=[SimpleNamespace(
                id=m, item_type=t, text=x, media=None, link=None,
                timestamp=datetime(2025, 6, 1), user_id=uid,
            ) for m, t, x, uid in msgs],
        )

    def direct_threads(self, amount=0):
        return self._threads

    def direct_messages(self, thread_id, amount=0):
        for t in self._threads:
            if t.id == thread_id:
                return t.messages
        return []

    def private_request(self, path: str, params=None):
        """Simula la API privada direct_v2/threads/{thread_id}/ con paginación."""
        if not path.startswith("direct_v2/threads/"):
            raise ValueError(f"Endpoint no soportado: {path}")

        thread_id = path.split("/")[2]
        params = params or {}

        # Buscar el hilo.
        messages = []
        for t in self._threads:
            if str(t.id) == thread_id:
                messages = t.messages
                break

        if not messages:
            return {"thread": {"items": [], "oldest_cursor": "", "has_older": False}}

        # Simular paginación: partir en páginas de `limit` elementos.
        limit = int(params.get("limit", 20))
        cursor = params.get("cursor", "")

        # El cursor es "<start_idx>:<end_idx>" (formato simulado).
        if cursor:
            try:
                start_idx = int(cursor.split(":")[0])
            except (ValueError, IndexError):
                start_idx = 0
        else:
            start_idx = 0

        end_idx = min(start_idx + limit, len(messages))
        page_items = messages[start_idx:end_idx]

        # Calcular el cursor siguiente.
        if end_idx < len(messages):
            next_cursor = f"{end_idx}:{end_idx + limit}"
            has_older = True
        else:
            next_cursor = ""
            has_older = False

        # Convertir a formato API crudo (simplificado).
        items = [
            {
                "id": m.id,
                "item_type": m.item_type,
                "text": m.text,
                "user_id": m.user_id,
                "timestamp": m.timestamp.isoformat() if m.timestamp else "",
                "media": m.media,
                "link": m.link,
            }
            for m in page_items
        ]

        return {
            "thread": {
                "items": items,
                "oldest_cursor": next_cursor,
                "has_older": has_older,
            }
        }

    def direct_message_delete(self, thread_id, message_id):
        self.deleted.append((thread_id, message_id))
        return True


CONFIG = """
account: {username: test, password: test}
behavior:
  dry_run: true
  min_delay: 0
  max_delay: 0
  pause_every: 0
  daily_limit: 100
scope:
  use_pagination: false  # deshabilitado por defecto en tests (instagrapi no disponible)
whitelist:
  protect_groups: true
  match:
    usernames: [mi_pareja]
    types: [voice]
rules:
  default_action: keep
  items:
    - name: "Datos sensibles"
      action: delete
      when: {keywords_any: [iban]}
    - name: "Todo el texto"
      action: delete
      when: {types: [text]}
"""


def build(tmp: Path, overrides: str = "", include_long_thread: bool = False):
    path = tmp / "config.yaml"
    path.write_text(CONFIG + overrides, encoding="utf-8")
    cfg = load_config(str(path))
    bus, stats = EventBus(), Stats()
    ctl = Controller(bus)
    ctl.reset()
    client = FakeClient(include_long_thread=include_long_thread)
    return cfg, bus, stats, ctl, client, Engine(cfg, bus, stats, ctl, client=client)


def main() -> int:
    tmp = Path(tempfile.mkdtemp())

    # ---------------------------------------------------------------- SIMULACIÓN
    print("\n\033[1mSIMULACIÓN (dry-run)\033[0m")
    cfg, bus, stats, ctl, client, engine = build(tmp)
    engine.run()
    snap = stats.snapshot()

    check("no borra nada de verdad",   client.deleted, [])
    check("marca 2 mensajes",          snap["would_delete"], 2)   # m1, m2
    # m3 (voz), m5 y m6 (chat entero con mi_pareja), m7 (grupo)
    check("protege 4",                 snap["protected"], 4)
    check("el usuario protege el chat entero, no solo un mensaje",
          snap["would_delete"] + snap["protected"], 6)
    check("examina solo los míos",     snap["scanned"], 6)
    check("recorre los 3 chats",       snap["threads_done"], 3)
    check("sin errores",               snap["errors"], 0)
    check("desglose por tipo",         snap["by_type"], {"text": 2})

    events = bus.history(kinds=["decision"], limit=99)
    protected = [e for e in events if e["action"] == "protected"]
    check("evento de voz protegida",
          any("voice" in e["reason"] or e["type"] == "voice" for e in protected), True)
    check("evento de pareja protegida",
          any("mi_pareja" in e["reason"] for e in protected), True)

    alog = AuditLog(cfg.path(cfg.audit.file))
    summary = alog.summary()
    check("auditoría registra simulados", summary["by_action"].get("would_delete"), 2)
    check("auditoría registra protegidos", summary["by_action"].get("protected"), 4)
    csv = alog.to_csv()
    check("exporta CSV con cabecera", csv.splitlines()[0].startswith("ts,run_id,action"), True)

    # ------------------------------------------------------------- BORRADO REAL
    print("\n\033[1mBORRADO REAL\033[0m")
    shutil.rmtree(tmp); tmp = Path(tempfile.mkdtemp())
    cfg, bus, stats, ctl, client, engine = build(tmp, "\n")
    cfg.behavior.dry_run = False
    engine.run()
    snap = stats.snapshot()

    check("borra exactamente 2",       len(client.deleted), 2)
    check("nunca toca la pareja",      any(m == "m6" for _, m in client.deleted), False)
    check("nunca toca la nota de voz", any(m == "m3" for _, m in client.deleted), False)
    check("nunca toca el grupo",       any(m == "m7" for _, m in client.deleted), False)
    check("nunca toca lo ajeno",       any(m == "m4" for _, m in client.deleted), False)
    check("contador de borrados",      snap["deleted"], 2)

    # ------------------------------------------------- REANUDACIÓN E IDEMPOTENCIA
    print("\n\033[1mREANUDACIÓN (segunda pasada)\033[0m")
    cfg2, bus2, stats2, ctl2, client2, engine2 = build(tmp, "\n")
    cfg2.behavior.dry_run = False
    engine2.run()
    snap2 = stats2.snapshot()
    check("no repite trabajo",         len(client2.deleted), 0)
    check("los reconoce como hechos",  snap2["already_done"], 2)

    # ------------------------------------------------------------- CUPO DIARIO
    print("\n\033[1mCUPO DIARIO\033[0m")
    shutil.rmtree(tmp); tmp = Path(tempfile.mkdtemp())
    cfg3, bus3, stats3, ctl3, client3, engine3 = build(tmp, "\n")
    cfg3.behavior.dry_run = False
    cfg3.behavior.daily_limit = 1
    engine3.run()
    check("respeta el tope de 1",      len(client3.deleted), 1)

    # ------------------------------------------------------------------ PARADA
    print("\n\033[1mPARADA ORDENADA\033[0m")
    shutil.rmtree(tmp); tmp = Path(tempfile.mkdtemp())
    cfg4, bus4, stats4, ctl4, client4, engine4 = build(tmp, "\n")
    cfg4.behavior.dry_run = False
    ctl4.stop()                       # parada pedida antes de empezar
    engine4.run()
    check("para sin borrar nada",      len(client4.deleted), 0)
    check("estado = stopped",          ctl4.state.value, "stopped")

    # ---------------------------------------------------------- PAGINACIÓN
    # Nota: iter_thread_messages() no exige instagrapi instalado (usa un
    # extractor de respaldo si la librería no está disponible), así que
    # estos tests corren igual que el resto: sin red ni credenciales.
    print("\n\033[1mPAGINACIÓN POR CURSOR (fallback desactivado)\033[0m")
    shutil.rmtree(tmp); tmp = Path(tempfile.mkdtemp())
    cfg5, bus5, stats5, ctl5, client5, engine5 = build(tmp, "\n", include_long_thread=True)
    cfg5.scope.use_pagination = False
    cfg5.behavior.dry_run = True
    engine5.run()
    snap5 = stats5.snapshot()

    check("sin paginación: escanea todos de una vez",
          snap5["scanned"], 6 + 55)  # 6 de los primeros 3 hilos + 55 del hilo largo
    # Sin paginación real, los hilos no se marcan como completos.
    check("sin paginación: hilos sin marca de completo",
          engine5.state.is_thread_complete("t4"), False)

    # ------------------------------------------------ PAGINACIÓN REAL (3 páginas)
    print("\n\033[1mPAGINACIÓN REAL POR CURSOR (55 mensajes, page_size=20)\033[0m")
    shutil.rmtree(tmp); tmp = Path(tempfile.mkdtemp())
    cfg6, bus6, stats6, ctl6, client6, engine6 = build(tmp, "\n", include_long_thread=True)
    cfg6.scope.use_pagination = True
    cfg6.scope.page_size = 20
    cfg6.scope.max_pages_per_thread = 0  # sin límite: hasta el fondo
    cfg6.behavior.dry_run = True
    engine6.run()
    snap6 = stats6.snapshot()

    page_events = bus6.history(kinds=["page"], limit=99)
    t4_pages = [e for e in page_events if e["thread_id"] == "t4"]

    check("paginación real: recorre las 3 páginas del hilo largo",
          len(t4_pages), 3)
    check("paginación real: escanea todos los mensajes (6 + 55)",
          snap6["scanned"], 6 + 55)
    check("paginación real: marca el hilo largo como completo",
          engine6.state.is_thread_complete("t4"), True)
    check("paginación real: limpia el cursor tras completar",
          engine6.state.get_cursor("t4"), "")
    check("paginación real: última página no indica 'hay más'",
          t4_pages[-1]["has_more"], False)

    # -------------------------------------------------- PAGINACIÓN CON LÍMITE
    print("\n\033[1mPAGINACIÓN CON max_pages_per_thread\033[0m")
    shutil.rmtree(tmp); tmp = Path(tempfile.mkdtemp())
    cfg7, bus7, stats7, ctl7, client7, engine7 = build(tmp, "\n", include_long_thread=True)
    cfg7.scope.use_pagination = True
    cfg7.scope.page_size = 20
    cfg7.scope.max_pages_per_thread = 2  # solo 2 de las 3 páginas
    cfg7.behavior.dry_run = True
    engine7.run()
    snap7 = stats7.snapshot()

    check("límite de páginas: corta antes de agotar el hilo",
          snap7["scanned"], 6 + 40)  # 2 páginas de 20 = 40 del hilo largo
    check("límite de páginas: NO marca el hilo como completo",
          engine7.state.is_thread_complete("t4"), False)
    check("límite de páginas: guarda el cursor para poder reanudar",
          bool(engine7.state.get_cursor("t4")), True)

    # ------------------------------------------------- REANUDACIÓN DESDE CURSOR
    print("\n\033[1mREANUDACIÓN DESDE EL CURSOR GUARDADO\033[0m")
    # Mismo directorio de estado (state.json ya tiene el cursor de arriba).
    cfg8, bus8, stats8, ctl8, client8, engine8 = build(tmp, "\n", include_long_thread=True)
    cfg8.scope.use_pagination = True
    cfg8.scope.page_size = 20
    cfg8.scope.max_pages_per_thread = 0  # ahora sin límite: que llegue al fondo
    cfg8.behavior.dry_run = True
    engine8.run()
    snap8 = stats8.snapshot()

    check("reanudación: no repite los mensajes de las páginas ya vistas",
          snap8["scanned"], 15)  # solo la 3ª página del hilo largo (55-40=15)
    check("reanudación: termina de completar el hilo largo",
          engine8.state.is_thread_complete("t4"), True)

    # ------------------------------------------------------- PARADA A MITAD
    # Interrumpimos justo al empezar la 2ª página del hilo largo: la 1ª ya
    # completó su ciclo (mensajes procesados + cursor guardado) antes de que
    # el evento de la 2ª dispare la parada. El cursor guardado debe ser
    # exactamente el de "tras la página 1", ni más ni menos — así la
    # siguiente ejecución retoma ahí sin perder ni repetir nada.
    print("\n\033[1mPARADA A MITAD DE PAGINACIÓN\033[0m")
    shutil.rmtree(tmp); tmp = Path(tempfile.mkdtemp())
    cfg9, bus9, stats9, ctl9, client9, engine9 = build(tmp, "\n", include_long_thread=True)
    cfg9.scope.use_pagination = True
    cfg9.scope.page_size = 20
    cfg9.behavior.dry_run = True

    original_publish = bus9.publish
    def _stop_at_second_long_page(kind, **payload):
        event = original_publish(kind, **payload)
        if kind == "page" and payload.get("thread_id") == "t4" and payload.get("page_num") == 2:
            ctl9.stop()
        return event
    bus9.publish = _stop_at_second_long_page

    engine9.run()

    check("parada a mitad: guarda el cursor de la página ya completada",
          bool(engine9.state.get_cursor("t4")), True)
    check("parada a mitad: NO marca el hilo como completo (aún quedaba trabajo)",
          engine9.state.is_thread_complete("t4"), False)
    check("parada a mitad: no procesó nada de la página interrumpida",
          engine9.state.get_cursor("t4"), "20:40")
    check("parada a mitad: ejecución marcada como detenida",
          ctl9.state.value, "stopped")

    # ------------------------------------------------------- RESET DE CURSORES
    print("\n\033[1mRESET DE CURSORES\033[0m")
    check("reset: antes - hilo largo completo (de la reanudación)",
          engine8.state.is_thread_complete("t4"), True)
    engine8.state.reset_cursors()
    check("reset: después - hilo largo ya no está completo",
          engine8.state.is_thread_complete("t4"), False)
    check("reset: todos los cursores quedan limpios",
          bool(engine8.state.thread_cursors), False)

    shutil.rmtree(tmp, ignore_errors=True)

    ok, total = sum(results), len(results)
    color = "\033[92m" if ok == total else "\033[91m"
    print(f"\n{color}{ok}/{total} tests pasados\033[0m\n")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
