#!/usr/bin/env python3
"""ig-espejo — panel web.

  python dashboard.py                  # http://127.0.0.1:8788
  python dashboard.py --port 9100
  python dashboard.py --host 0.0.0.0   # ¡solo si sabes lo que haces!

Imprime en consola la URL con el token de acceso. Sin ese token no se entra.
Puerto 8788 por defecto para poder tenerlo abierto a la vez que ig-unsender.
"""
from __future__ import annotations

import argparse
import logging
import secrets
import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.runner import ScanRunner  # noqa: E402
from src.web.server import create_app  # noqa: E402

BANNER = """
╭───────────────────────────────────────────────────────────╮
│  ig-espejo · quién te devuelve el reflejo                 │
╰───────────────────────────────────────────────────────────╯

  Abre esta URL en el navegador:

  \033[96m{url}\033[0m

  Solo lectura: esta herramienta no sigue, no deja de seguir
  y no borra nada. Escuchando en {host}:{port}.
  Ctrl+C para parar el servidor.
"""


def main() -> int:
    p = argparse.ArgumentParser(description="Panel de ig-espejo")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("Falta uvicorn. Ejecuta: pip install -r requirements.txt")
        return 1

    try:
        runner = ScanRunner(args.config)
    except FileNotFoundError as exc:
        print(f"\n{exc}\n")
        return 1

    cfg = runner.cfg
    host = args.host or cfg.dashboard.host
    port = args.port or cfg.dashboard.port
    token = cfg.dashboard.auth_token or secrets.token_urlsafe(24)

    logging.basicConfig(
        level=getattr(logging, str(cfg.logging.level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-20s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(cfg.path(cfg.logging.file), encoding="utf-8"),
        ],
    )

    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{display_host}:{port}/?token={token}"
    print(BANNER.format(url=url, host=host, port=port))

    if host in ("0.0.0.0", "::"):
        print("  \033[93m⚠  Escuchando en todas las interfaces. El panel maneja tu")
        print("     sesión de Instagram: ponlo detrás de HTTPS o de un túnel SSH.\033[0m\n")

    app = create_app(runner, token)

    if cfg.dashboard.open_browser and not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
