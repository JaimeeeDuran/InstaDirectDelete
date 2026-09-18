#!/usr/bin/env python3
"""Pruebas del motor de análisis de ig-espejo.

No necesita credenciales, ni red, ni instagrapi: el motor trabaja sobre
diccionarios planos a propósito, justo para poder probarlo así.

  python test_analysis.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.analysis import analyze, score_sospecha, serie_historica, to_csv  # noqa: E402
from src.config import load_config  # noqa: E402
from src.snapshots import SnapshotStore  # noqa: E402

PASS, FAIL = "\033[92m✓\033[0m", "\033[91m✗\033[0m"
results = []


def check(name, got, want):
    ok = got == want
    results.append(ok)
    print(f"  {PASS if ok else FAIL} {name}"
          + ("" if ok else f"\n      esperado={want!r} obtenido={got!r}"))


def u(pk, username, **extra):
    """Construye un registro de usuario normalizado."""
    base = {
        "pk": str(pk), "username": username, "full_name": username.title(),
        "is_private": False, "is_verified": False, "sin_foto": False,
    }
    base.update(extra)
    return base


def snap(ts, seguidores, siguiendo, deep=None):
    return {
        "ts": ts,
        "me": {"pk": "1", "username": "jimmy"},
        "followers": {r["pk"]: r for r in seguidores},
        "following": {r["pk"]: r for r in siguiendo},
        "deep": deep or {},
    }


CONFIG = """
account: {username: jimmy, password: x}
scan: {}
suspects:
  enabled: true
  umbral_medio: 35
  umbral_alto: 60
whitelist:
  usernames: [mi_pareja]
snapshots: {dir: snapshots}
"""


def build_cfg(tmp: Path):
    path = tmp / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return load_config(str(path))


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    cfg = build_cfg(tmp)

    # ------------------------------------------------------------ CATEGORÍAS
    print("\n\033[1mCATEGORÍAS DE RELACIÓN\033[0m")
    actual = snap(
        "2026-01-02T10:00:00",
        seguidores=[u(1, "colega"), u(2, "mi_pareja"), u(3, "fan_silencioso")],
        siguiendo=[u(1, "colega"), u(2, "mi_pareja"), u(4, "famoso")],
    )
    rep = analyze(actual, None, None, cfg).to_dict()

    check("cuenta los seguidores", rep["totales"]["seguidores"], 3)
    check("cuenta a quién sigues", rep["totales"]["siguiendo"], 3)
    check("mutuos = intersección", rep["totales"]["mutuos"], 2)
    check("no te siguen = sigues − te siguen", rep["totales"]["no_te_siguen"], 1)
    check("el que no te sigue es el famoso",
          rep["listas"]["no_te_siguen"][0]["username"], "famoso")
    check("fans = te siguen − sigues", rep["totales"]["fans"], 1)
    check("el fan es el silencioso", rep["listas"]["fans"][0]["username"], "fan_silencioso")
    check("reciprocidad 2 de 3", rep["metricas"]["reciprocidad"], 66.7)
    check("sin escaneo previo no hay movimientos", rep["movimientos"]["comparado_con"], "")

    # ------------------------------------------------------------ INTOCABLES
    print("\n\033[1mINTOCABLES\033[0m")
    check("la pareja cuenta como protegida", rep["totales"]["protegidos"], 1)
    protegida = [r for r in rep["listas"]["mutuos"] if r["username"] == "mi_pareja"][0]
    check("y va marcada en su ficha", protegida.get("protegido"), True)

    # ----------------------------------------------------------- MOVIMIENTOS
    print("\n\033[1mMOVIMIENTOS ENTRE ESCANEOS\033[0m")
    anterior = snap(
        "2026-01-01T10:00:00",
        seguidores=[u(1, "colega"), u(2, "mi_pareja"), u(9, "se_va")],
        siguiendo=[u(1, "colega"), u(2, "mi_pareja")],
    )
    rep2 = analyze(actual, anterior, None, cfg).to_dict()
    mov = rep2["movimientos"]

    check("detecta el nuevo seguidor", [r["username"] for r in mov["nuevos"]], ["fan_silencioso"])
    check("detecta quién te dejó de seguir",
          [r["username"] for r in mov["perdidos"]], ["se_va"])
    check("detecta a quién empezaste a seguir",
          [r["username"] for r in mov["nuevos_seguidos"]], ["famoso"])
    check("dice con qué escaneo compara", mov["comparado_con"], "2026-01-01T10:00:00")

    # ----------------------------------------------------------- RECUPERADOS
    print("\n\033[1mRECUPERADOS (se fueron y volvieron)\033[0m")
    viejo = snap(
        "2025-12-01T10:00:00",
        seguidores=[u(1, "colega"), u(7, "vuelve")],
        siguiendo=[u(1, "colega")],
    )
    medio = snap(
        "2025-12-15T10:00:00",
        seguidores=[u(1, "colega")],          # "vuelve" se había ido
        siguiendo=[u(1, "colega")],
    )
    ahora = snap(
        "2026-01-02T10:00:00",
        seguidores=[u(1, "colega"), u(7, "vuelve"), u(8, "nunca_visto")],
        siguiendo=[u(1, "colega")],
    )
    rep3 = analyze(ahora, medio, [viejo], cfg).to_dict()
    check("marca como recuperado a quien ya estuvo",
          [r["username"] for r in rep3["movimientos"]["recuperados"]], ["vuelve"])
    check("el que nunca estuvo es nuevo, no recuperado",
          sorted(r["username"] for r in rep3["movimientos"]["nuevos"]),
          ["nunca_visto", "vuelve"])

    # ------------------------------------------------------------ SOSPECHOSOS
    print("\n\033[1mHEURÍSTICA DE SOSPECHOSOS\033[0m")
    limpio = u(10, "ana_martinez", full_name="Ana Martínez")
    score, motivos = score_sospecha(limpio, cfg.suspects)
    check("una cuenta normal no suma nada", score, 0)
    check("y no inventa motivos", motivos, [])

    turbio = u(11, "user38472911", full_name="", sin_foto=True)
    score, motivos = score_sospecha(turbio, cfg.suspects)
    check("sin foto + dígitos + sin nombre suma", score, 60)
    check("y explica los tres motivos", len(motivos), 3)
    check("el primer motivo es legible", motivos[0], "Sin foto de perfil")

    # Señales caras: solo cuentan si hubo escaneo profundo.
    profundo = u(12, "bot9999", full_name="", sin_foto=True,
                 media_count=0, biography="", follower_count=3, following_count=4000)
    score_deep, motivos_deep = score_sospecha(profundo, cfg.suspects)
    check("con datos de perfil la puntuación se topa en 100", score_deep, 100)
    check("y el ratio extremo aparece como motivo",
          any("Sigue a 4000" in m for m in motivos_deep), True)

    sospechosos = analyze(
        snap("2026-01-03T10:00:00",
             seguidores=[limpio, turbio, u(2, "mi_pareja", sin_foto=True, full_name="")],
             siguiendo=[]),
        None, None, cfg,
    ).to_dict()["sospechosos"]
    check("solo entran los que pasan el umbral",
          [r["username"] for r in sospechosos], ["user38472911"])
    check("la pareja no se juzga aunque dé indicios",
          any(r["username"] == "mi_pareja" for r in sospechosos), False)
    # 60 puntos con umbral_alto=60: el umbral es inclusivo, así que es "alto".
    check("se etiqueta la banda", sospechosos[0]["banda"], "alto")

    # --------------------------------------------------------------- ORDEN
    print("\n\033[1mORDEN ESTABLE\033[0m")
    desordenado = analyze(
        snap("2026-01-04T10:00:00",
             seguidores=[u(1, "zulema"), u(2, "ana"), u(3, "Mario")],
             siguiendo=[]),
        None, None, cfg,
    ).to_dict()
    check("las listas salen alfabéticas, no al azar",
          [r["username"] for r in desordenado["listas"]["fans"]], ["ana", "Mario", "zulema"])

    # ------------------------------------------------------------ HISTÓRICO
    print("\n\033[1mHISTÓRICO Y PERSISTENCIA\033[0m")
    store = SnapshotStore(tmp / "snapshots", keep=3)
    for s in (viejo, medio, ahora):
        store.save(s)
    check("guarda cada escaneo", store.count(), 3)
    check("el último es el más reciente", store.latest()["ts"], ahora["ts"])
    check("el anterior es el de en medio", store.previous()["ts"], medio["ts"])

    store.save(snap("2026-02-01T10:00:00", [u(1, "colega")], []))
    check("respeta el máximo y borra el más viejo", store.count(), 3)
    check("el que se borró fue el más antiguo",
          any(s["ts"] == viejo["ts"] for s in store.all()), False)

    serie = serie_historica(store.all())
    check("la serie tiene un punto por escaneo", len(serie), 3)
    check("cada punto lleva sus totales", set(serie[0]), {"ts", "seguidores", "siguiendo", "mutuos"})

    # ------------------------------------------------------------------ CSV
    print("\n\033[1mEXPORT CSV\033[0m")
    csv = to_csv([{**turbio, "score": 60, "motivos": ["Sin foto de perfil", "Sin nombre completo"]}])
    check("cabecera correcta", csv.splitlines()[0].startswith("username,full_name"), True)
    check("une los motivos en una celda", "Sin foto de perfil · Sin nombre completo" in csv, True)

    # ------------------------------------------------------- CASOS EXTREMOS
    print("\n\033[1mCASOS EXTREMOS\033[0m")
    vacio = analyze(snap("2026-01-05T10:00:00", [], []), None, None, cfg).to_dict()
    check("cuenta sin seguidores no revienta", vacio["totales"]["seguidores"], 0)
    check("ni divide entre cero", vacio["metricas"]["reciprocidad"], 0)
    check("ni inventa ratio", vacio["metricas"]["ratio"], 0.0)

    shutil.rmtree(tmp, ignore_errors=True)

    ok, total = sum(results), len(results)
    color = "\033[92m" if ok == total else "\033[91m"
    print(f"\n{color}{ok}/{total} tests pasados\033[0m\n")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
