#!/usr/bin/env python3
"""ig-espejo — quién te devuelve el reflejo.

Analiza tus seguidores y a quién sigues, y te dice quién te corresponde y
quién no. SOLO LECTURA: no sigue, no deja de seguir, no bloquea y no borra
nada. Lo peor que puede pasar aquí es que Instagram te pida esperar.

  python run.py --scan            # escaneo rápido y informe
  python run.py --scan --deep     # además pide perfil de los más sospechosos
  python run.py --report          # informe del último escaneo, sin tocar la red
  python run.py --historico       # evolución de seguidores entre escaneos
  python run.py --csv no_te_siguen > lista.csv
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.analysis import to_csv  # noqa: E402
from src.runner import ScanRunner  # noqa: E402

VERDE, AMBAR, AZUL, GRIS, NEGRITA, FIN = (
    "\033[92m", "\033[93m", "\033[96m", "\033[90m", "\033[1m", "\033[0m"
)

LISTAS = {
    "mutuos": ("listas", "mutuos"),
    "no_te_siguen": ("listas", "no_te_siguen"),
    "fans": ("listas", "fans"),
    "nuevos": ("movimientos", "nuevos"),
    "perdidos": ("movimientos", "perdidos"),
    "recuperados": ("movimientos", "recuperados"),
    "sospechosos": (None, "sospechosos"),
}


def _extraer(rep: dict, nombre: str) -> list:
    seccion, clave = LISTAS[nombre]
    return rep[seccion][clave] if seccion else rep[clave]


def _signo(simbolo: str, n: int) -> str:
    """'+12', '−3' … pero un 0 se queda en '0': '−0' no significa nada."""
    return f"{simbolo}{n}" if n else "0"


def imprimir_informe(rep: dict, detalle: int = 15) -> None:
    t, m, mov = rep["totales"], rep["metricas"], rep["movimientos"]

    print(f"\n{NEGRITA}@{rep['cuenta'] or '¿?'} · {rep['generado_en']}{FIN}\n")
    print(f"  {t['seguidores']:>6} seguidores")
    print(f"  {t['siguiendo']:>6} siguiendo")
    print(f"  {VERDE}{t['mutuos']:>6}{FIN} mutuos          "
          f"{GRIS}({m['reciprocidad']}% de los que sigues te devuelven el seguimiento){FIN}")
    print(f"  {AMBAR}{t['no_te_siguen']:>6}{FIN} no te siguen")
    print(f"  {AZUL}{t['fans']:>6}{FIN} fans            {GRIS}(te siguen y tú no){FIN}")
    print(f"  {t['verificados']:>6} verificados · {t['privados']} privadas"
          f" · {t['protegidos']} intocables")

    if mov["comparado_con"]:
        print(f"\n{NEGRITA}Movimientos{FIN} {GRIS}desde {mov['comparado_con']}{FIN}")
        print(f"  {VERDE}{_signo('+', len(mov['nuevos'])):<5}{FIN} nuevos seguidores")
        print(f"  {AMBAR}{_signo('−', len(mov['perdidos'])):<5}{FIN} te dejaron de seguir")
        if mov["recuperados"]:
            print(f"  {AZUL}↩{len(mov['recuperados']):<5}{FIN} recuperados "
                  f"{GRIS}(se fueron y han vuelto){FIN}")
        for r in mov["perdidos"][:detalle]:
            print(f"      {AMBAR}−{FIN} @{r['username']}")
        if len(mov["perdidos"]) > detalle:
            print(f"      {GRIS}… y {len(mov['perdidos']) - detalle} más{FIN}")
    else:
        print(f"\n{GRIS}  Primer escaneo: sin movimientos que comparar todavía.{FIN}")

    if rep["sospechosos"]:
        print(f"\n{NEGRITA}Cuentas con indicios{FIN} {GRIS}(mira los motivos, no la cifra){FIN}")
        for s in rep["sospechosos"][:detalle]:
            print(f"  {s['score']:>3}  @{s['username']:<24} {GRIS}{' · '.join(s['motivos'])}{FIN}")
        if len(rep["sospechosos"]) > detalle:
            print(f"  {GRIS}… y {len(rep['sospechosos']) - detalle} más{FIN}")
        if not rep["escaneo_profundo"]:
            print(f"  {GRIS}Solo señales baratas. Usa --deep para mirar posts, bio y ratios.{FIN}")

    print()


def imprimir_historico(serie: list) -> None:
    if not serie:
        print("\nTodavía no hay escaneos guardados.\n")
        return
    print(f"\n{NEGRITA}Evolución{FIN}\n")
    ancho = 34
    maximo = max(p["seguidores"] for p in serie) or 1
    previo = None
    for p in serie:
        barra = "█" * max(1, round(p["seguidores"] / maximo * ancho))
        delta = ""
        if previo is not None:
            d = p["seguidores"] - previo
            if d > 0:
                delta = f"{VERDE}+{d}{FIN}"
            elif d < 0:
                delta = f"{AMBAR}{d}{FIN}"
            else:
                delta = f"{GRIS}={FIN}"
        previo = p["seguidores"]
        print(f"  {p['ts'][:16]}  {barra:<{ancho}} {p['seguidores']:>6}  {delta}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="ig-espejo — análisis de seguidores (solo lectura)",
    )
    ap.add_argument("--scan", action="store_true", help="tomar una foto fija nueva")
    ap.add_argument("--deep", action="store_true",
                    help="con --scan: además consulta el perfil de los más sospechosos")
    ap.add_argument("--report", action="store_true", help="informe del último escaneo")
    ap.add_argument("--historico", action="store_true", help="evolución entre escaneos")
    ap.add_argument("--csv", metavar="LISTA", choices=sorted(LISTAS),
                    help="volcar una lista en CSV por stdout")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    if not any([args.scan, args.report, args.historico, args.csv]):
        ap.print_help()
        return 0

    try:
        runner = ScanRunner(args.config)
    except FileNotFoundError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1

    logging.basicConfig(
        level=getattr(logging, runner.cfg.logging.level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(message)s",
        handlers=[logging.FileHandler(runner.cfg.path(runner.cfg.logging.file), encoding="utf-8")],
    )

    if args.scan:
        runner.bus.publish = _eco(runner.bus.publish)
        resultado = runner.start(deep=args.deep or None)
        if not resultado.get("ok"):
            print(resultado.get("error"), file=sys.stderr)
            return 1
        runner._thread.join()  # la CLI sí espera a que termine
        if runner.state == "error":
            print(f"\nEl escaneo falló: {runner.error}\n", file=sys.stderr)
            return 1

    if args.csv:
        rep = runner.last_report()
        if rep is None:
            print("No hay ningún escaneo todavía. Lanza --scan primero.", file=sys.stderr)
            return 1
        sys.stdout.write(to_csv(_extraer(rep, args.csv)))
        return 0

    if args.historico:
        imprimir_historico(runner.history())
        return 0

    rep = runner.last_report()
    if rep is None:
        print("No hay ningún escaneo todavía. Lanza --scan primero.", file=sys.stderr)
        return 1
    imprimir_informe(rep)
    return 0


def _eco(publish):
    """Hace que los eventos del bus se vean también por consola."""
    def envoltura(kind, **payload):
        if kind == "log":
            nivel = payload.get("level", "INFO")
            color = {"WARNING": AMBAR, "ERROR": AMBAR}.get(nivel, GRIS)
            print(f"  {color}{payload.get('message','')}{FIN}")
        return publish(kind, **payload)
    return envoltura


if __name__ == "__main__":
    raise SystemExit(main())
