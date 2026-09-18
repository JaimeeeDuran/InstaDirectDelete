#!/usr/bin/env python3
"""Test del motor de decisión con mensajes simulados (sin tocar Instagram).

Comprueba que la whitelist gana siempre, que las reglas se aplican en orden,
y que la clasificación por tipo funciona.

  python test_rules.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))

from src.config import load_config  # noqa: E402
from src.filters import DELETE, KEEP, PROTECTED, FilterEngine, MessageCtx, classify  # noqa: E402

PASS, FAIL = "\033[92m✓\033[0m", "\033[91m✗\033[0m"
results = []


def check(name: str, got, want) -> None:
    ok = got == want
    results.append(ok)
    print(f"  {PASS if ok else FAIL} {name}" + ("" if ok else f"\n      esperado={want!r} obtenido={got!r}"))


# --- dobles de prueba -------------------------------------------------------
def msg(item_type="text", text="", media_type=None, ts=None, mid="m1"):
    media = SimpleNamespace(media_type=media_type, caption_text=None) if media_type else None
    return SimpleNamespace(
        id=mid, item_type=item_type, text=text, media=media, link=None,
        timestamp=ts or datetime(2025, 6, 1), user_id="999",
    )


def thread(tid="t1", title="chat de prueba", users=("amigo",), is_group=False):
    return SimpleNamespace(
        id=tid, thread_title=title, is_group=is_group,
        users=[SimpleNamespace(username=u) for u in users],
    )


def ctx(m, t=None):
    return MessageCtx.build(m, t or thread())


# --- config de prueba -------------------------------------------------------
CONFIG = """
account: {username: test, password: test}
rules:
  default_action: keep
  items:
    - name: "Proteger voz"
      action: keep
      when: {types: [voice]}
    - name: "Datos sensibles"
      action: delete
      when: {keywords_any: ["iban", "contraseña"]}
    - name: "Fotos antiguas"
      action: delete
      when: {types: [photo], before: "2025-01-01"}
    - name: "Texto"
      action: delete
      when: {types: [text]}
whitelist:
  protect_groups: true
  message_ids: ["protegido-123"]
  match:
    usernames: ["mi_pareja"]
    types: [gif]
    keywords_any: ["te quiero"]
"""


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(CONFIG, encoding="utf-8")
    cfg = load_config(str(cfg_path))
    fe = FilterEngine(cfg)

    print("\n\033[1mCLASIFICACIÓN DE TIPOS\033[0m")
    check("texto",            classify(msg("text")), "text")
    check("foto",             classify(msg("media", media_type=1)), "photo")
    check("vídeo",            classify(msg("media", media_type=2)), "video")
    check("nota de voz",      classify(msg("voice_media")), "voice")
    check("gif",              classify(msg("animated_media")), "gif")
    check("reel compartido",  classify(msg("clip")), "reel")
    check("story",            classify(msg("story_share")), "story")
    check("desaparece",       classify(msg("raven_media")), "disappearing")
    check("desconocido",      classify(msg("cosa_rara_nueva")), "other")

    print("\n\033[1mWHITELIST (debe ganar siempre)\033[0m")
    d = fe.decide(ctx(msg("text", "hola"), thread(users=("mi_pareja",))))
    check("usuario protegido",   d.action, PROTECTED)
    d = fe.decide(ctx(msg("text", "hola")))
    check("...pero otro usuario no", d.action, DELETE)
    d = fe.decide(ctx(msg("text", "oye te quiero mucho")))
    check("palabra protegida",   d.action, PROTECTED)
    d = fe.decide(ctx(msg("animated_media")))
    check("tipo protegido (gif)", d.action, PROTECTED)
    d = fe.decide(ctx(msg("text", "hola"), thread(is_group=True)))
    check("grupo protegido",     d.action, PROTECTED)
    d = fe.decide(ctx(msg("text", "hola", mid="protegido-123")))
    check("ID protegido",        d.action, PROTECTED)

    print("\n\033[1mWHITELIST vs REGLA DE BORRADO\033[0m")
    # "iban" casa con la regla de borrado, pero el usuario está en whitelist.
    d = fe.decide(ctx(msg("text", "mi iban es X"), thread(users=("mi_pareja",))))
    check("whitelist gana a delete", d.action, PROTECTED)
    d = fe.decide(ctx(msg("text", "mi iban es X")))
    check("sin whitelist, se borra", d.action, DELETE)
    check("motivo indica la regla",  d.rule, "Datos sensibles")

    print("\n\033[1mORDEN DE LAS REGLAS\033[0m")
    d = fe.decide(ctx(msg("voice_media")))
    check("voz -> keep (regla 1)", d.action, KEEP)
    d = fe.decide(ctx(msg("media", media_type=1, ts=datetime(2024, 3, 1))))
    check("foto antigua -> delete", d.action, DELETE)
    d = fe.decide(ctx(msg("media", media_type=1, ts=datetime(2025, 6, 1))))
    check("foto reciente -> defecto keep", d.action, KEEP)
    d = fe.decide(ctx(msg("media", media_type=2)))
    check("vídeo -> ninguna regla -> keep", d.action, KEEP)

    print("\n\033[1mVALIDACIÓN DE CONFIG\033[0m")
    bad = tmp / "bad.yaml"
    bad.write_text("""
account: {username: t, password: t}
rules:
  items:
    - name: "regex rota"
      action: delete
      when: {regex_any: ["[sin cerrar"]}
    - name: "tipo inventado"
      action: delete
      when: {types: [fotoo]}
    - name: "pilla todo"
      action: delete
      when: {catch_all: true}
    - name: "inalcanzable"
      action: delete
      when: {types: [text]}
""", encoding="utf-8")
    warns = FilterEngine(load_config(str(bad))).validate()
    check("detecta regex inválida",  any("regex inválida" in w for w in warns), True)
    check("detecta tipo desconocido", any("tipo desconocido" in w for w in warns), True)
    check("detecta regla inalcanzable", any("inalcanzable" in w for w in warns), True)

    shutil.rmtree(tmp, ignore_errors=True)

    ok, total = sum(results), len(results)
    color = "\033[92m" if ok == total else "\033[91m"
    print(f"\n{color}{ok}/{total} tests pasados\033[0m\n")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
