"""Login y sesión persistente con instagrapi.

instagrapi se importa de forma perezosa para que el dashboard pueda
arrancar y dejarte configurar el stack aunque aún no lo hayas instalado.
"""
from __future__ import annotations

import logging
from typing import Any

from .config import Config

log = logging.getLogger("ig-unsender.client")


class LoginError(RuntimeError):
    pass


def build_client(cfg: Config, on_2fa=None) -> Any:
    """Devuelve un Client de instagrapi ya autenticado.

    `on_2fa` es un callable opcional que devuelve el código 2FA cuando no hay
    semilla TOTP configurada (en CLI se pide por consola; en el dashboard se
    exige la semilla, porque no hay nadie mirando la terminal).
    """
    try:
        from instagrapi import Client
        from instagrapi.exceptions import LoginRequired
    except ImportError as exc:  # noqa: BLE001
        raise LoginError(
            "instagrapi no está instalado. Ejecuta: pip install -r requirements.txt"
        ) from exc

    if not cfg.account.username or not cfg.account.password:
        raise LoginError("Faltan usuario o contraseña. Rellénalos en Ajustes.")

    session_path = cfg.path(cfg.session.file)

    if session_path.exists():
        cl = _new_client(Client)
        try:
            cl.load_settings(session_path)
            cl.login(cfg.account.username, cfg.account.password)
            cl.get_timeline_feed()  # comprueba que la sesión sigue viva
            log.info("Sesión reutilizada desde %s", session_path.name)
            return cl
        except LoginRequired:
            log.warning("La sesión guardada caducó. Login desde cero.")
        except Exception as exc:  # noqa: BLE001
            log.warning("La sesión guardada no sirve (%s). Login desde cero.", exc)

    cl = _new_client(Client)
    _fresh_login(cl, cfg, on_2fa)
    cl.dump_settings(session_path)
    log.info("Sesión nueva guardada en %s", session_path.name)
    return cl


def _new_client(client_cls) -> Any:
    cl = client_cls()
    cl.delay_range = [1, 3]  # jitter interno de instagrapi entre llamadas
    return cl


def _fresh_login(cl: Any, cfg: Config, on_2fa=None) -> None:
    code = ""
    if cfg.account.totp_seed:
        code = cl.totp_generate_code(cfg.account.totp_seed)
        log.info("Código 2FA generado desde la semilla TOTP.")

    try:
        cl.login(cfg.account.username, cfg.account.password, verification_code=code)
        return
    except Exception as exc:  # noqa: BLE001
        if cfg.account.totp_seed or on_2fa is None:
            raise LoginError(
                f"No se pudo iniciar sesión: {exc}. "
                "Si tienes 2FA, configura 'account.totp_seed'."
            ) from exc

    manual = (on_2fa() or "").strip()
    if not manual:
        raise LoginError("Se requiere código 2FA y no se proporcionó ninguno.")
    cl.login(cfg.account.username, cfg.account.password, verification_code=manual)
