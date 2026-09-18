#!/usr/bin/env python3
"""ig-unsender — CLI.

Ejemplos:
  python run.py                     # simulación (dry-run por defecto del config)
  python run.py --dry-run           # fuerza simulación
  python run.py --go                # borrado real (pide confirmación)
  python run.py --go --yes          # borrado real sin preguntar (para cron)
  python run.py --explain           # muestra cómo quedarían las reglas y sale
  python run.py --config otro.yaml
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.config import load_config  # noqa: E402
from src.filters import KNOWN_TYPES  # noqa: E402
from src.runner import EngineRunner  # noqa: E402


def setup_logging(cfg) -> None:
    level = getattr(logging, str(cfg.logging.level).upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)-22s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(cfg.path(cfg.logging.file), encoding="utf-8"),
        ],
    )


def explain(cfg) -> None:
    wl = cfg.whitelist
    print("\n\033[1mORDEN DE DECISIÓN\033[0m\n")
    print("  1. WHITELIST (intocable)")
    m = wl.match
    protections = []
    if m.usernames:    protections.append(f"usuarios: {', '.join(m.usernames)}")
    if m.types:        protections.append(f"tipos: {', '.join(m.types)}")
    if m.keywords_any: protections.append(f"palabras: {len(m.keywords_any)}")
    if m.regex_any:    protections.append(f"regex: {len(m.regex_any)}")
    if m.threads:      protections.append(f"chats: {len(m.threads)}")
    if wl.message_ids: protections.append(f"mensajes: {len(wl.message_ids)}")
    if wl.protect_groups: protections.append("todos los grupos")
    print(f"     {'; '.join(protections) if protections else '(vacía)'}\n")

    print("  2. REGLAS (la primera que casa decide)")
    if not cfg.rules.items:
        print("     (ninguna: no se borrará nada)")
    for i, rule in enumerate(cfg.rules.items, 1):
        mark = "✓" if rule.enabled else "·"
        icon = "🗑" if rule.action == "delete" else "✓"
        crit = []
        w = rule.when
        if w.catch_all:    crit.append("TODO")
        if w.types:        crit.append(f"tipos={','.join(w.types)}")
        if w.keywords_any: crit.append(f"contiene alguna de {len(w.keywords_any)}")
        if w.keywords_all: crit.append(f"contiene las {len(w.keywords_all)}")
        if w.regex_any:    crit.append(f"regex×{len(w.regex_any)}")
        if w.before:       crit.append(f"antes de {w.before}")
        if w.after:        crit.append(f"después de {w.after}")
        if w.usernames:    crit.append(f"usuarios={','.join(w.usernames)}")
        if w.is_group is not None: crit.append("grupos" if w.is_group else "directos")
        print(f"   {mark} {i}. {icon} {rule.action:<7} {rule.name}")
        print(f"         cuando: {' Y '.join(crit) if crit else '(sin criterios: nunca casa)'}")
    print(f"\n  3. POR DEFECTO: {cfg.rules.default_action}\n")
    print(f"\033[2mTipos válidos: {', '.join(KNOWN_TYPES)}\033[0m\n")


def main() -> int:
    p = argparse.ArgumentParser(description="Anula en bloque tus mensajes de Instagram DM.")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--explain", action="store_true", help="Muestra el árbol de decisión y sale.")
    p.add_argument("--yes", action="store_true", help="No pedir confirmación (para cron).")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="Fuerza simulación.")
    g.add_argument("--go", action="store_true", help="Fuerza borrado real.")
    args = p.parse_args()

    cfg = load_config(args.config)
    if args.explain:
        explain(cfg)
        return 0

    setup_logging(cfg)
    log = logging.getLogger("ig-unsender")

    dry_run = True if args.dry_run else (False if args.go else cfg.behavior.dry_run)

    if not dry_run and not args.yes:
        print("\n\033[91m⚠  BORRADO REAL. Esto no se puede deshacer.\033[0m")
        print("   Los mensajes desaparecerán también para la otra persona.")
        if input("   Escribe BORRAR para confirmar: ").strip() != "BORRAR":
            print("   Cancelado.")
            return 1

    runner = EngineRunner(args.config)

    # Ctrl+C = parada ordenada, no un tajo a mitad de una llamada a la API.
    def _sigint(signum, frame):  # noqa: ARG001
        log.warning("Señal recibida: parando de forma ordenada… (Ctrl+C otra vez para forzar)")
        runner.stop()
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    signal.signal(signal.SIGINT, _sigint)
    signal.signal(signal.SIGTERM, _sigint)

    # Espejo de los eventos del motor en consola.
    queue = runner.bus.subscribe()

    result = runner.start(dry_run=dry_run)
    if not result.get("ok"):
        log.error(result.get("error"))
        return 1

    while runner.busy:
        for event in runner.bus.drain(queue):
            if event["kind"] == "decision" and event["action"] in ("deleted", "would_delete"):
                tag = "SIM " if event["action"] == "would_delete" else "DEL "
                log.info("%s [%s] %s · %s", tag, event["type"], event["thread"], event["preview"])
        runner.join(timeout=0.3)

    snap = runner.stats.snapshot()
    print(f"\n  Examinados : {snap['scanned']}")
    print(f"  {'Simulados ' if dry_run else 'Anulados  '} : "
          f"{snap['would_delete'] if dry_run else snap['deleted']}")
    print(f"  Protegidos : {snap['protected']}")
    print(f"  Descartados: {snap['kept']}")
    print(f"  Errores    : {snap['errors']}\n")
    return 0 if snap["errors"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
