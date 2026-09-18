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

    def __init__(self):
        self.user_id = ME
        self.deleted = []
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
scope: {}
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


def build(tmp: Path, overrides: str = ""):
    path = tmp / "config.yaml"
    path.write_text(CONFIG + overrides, encoding="utf-8")
    cfg = load_config(str(path))
    bus, stats = EventBus(), Stats()
    ctl = Controller(bus)
    ctl.reset()
    client = FakeClient()
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

    shutil.rmtree(tmp, ignore_errors=True)

    ok, total = sum(results), len(results)
    color = "\033[92m" if ok == total else "\033[91m"
    print(f"\n{color}{ok}/{total} tests pasados\033[0m\n")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
