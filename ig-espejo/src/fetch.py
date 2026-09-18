"""Descarga de listas de seguidores/seguidos y enriquecido opcional.

Dos modos, y la diferencia entre ellos importa:

  Escaneo RÁPIDO: dos llamadas (user_followers / user_following). Trae lo que
  Instagram devuelve de serie en las listas: usuario, nombre, privada,
  verificada y si tiene foto de perfil por defecto. Es barato y suficiente
  para todo el análisis de reciprocidad.

  Escaneo PROFUNDO: además pide user_info CUENTA POR CUENTA. Es lo único que
  da publicaciones, biografía y ratios — y es también la vía más rápida a un
  rate-limit. Por eso va acotado (deep_scan_limit), con pausas aleatorias
  entre peticiones y pausas largas cada N cuentas, exactamente con la misma
  filosofía de ritmo que ig-unsender.

Verificado contra instagrapi 3.0.4:
  user_followers(user_id, amount=0) -> Dict[str, UserShort]
  user_following(user_id, amount=0) -> Dict[str, UserShort]
  user_info(user_id)                -> User
  user_id_from_username(username)   -> str
Si actualizas instagrapi, vuelve a comprobar esta lista antes de nada.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from typing import Any, Callable, Dict, List

log = logging.getLogger("ig-espejo.fetch")

Progress = Callable[[str, str], None]  # (nivel, mensaje)


def _noop(level: str, message: str) -> None:  # pragma: no cover - trivial
    pass


def normalizar(user: Any) -> Dict[str, Any]:
    """Convierte un UserShort de instagrapi en un dict plano y estable.

    Todo con getattr y valores por defecto: la API privada añade y quita
    campos sin avisar, y un escaneo nunca debe romperse por eso.
    """
    return {
        "pk": str(getattr(user, "pk", "") or ""),
        "username": str(getattr(user, "username", "") or ""),
        "full_name": str(getattr(user, "full_name", "") or ""),
        "is_private": bool(getattr(user, "is_private", False)),
        "is_verified": bool(getattr(user, "is_verified", False)),
        # Señal gratis para la heurística: Instagram marca las cuentas que
        # nunca han subido foto de perfil.
        "sin_foto": bool(getattr(user, "has_anonymous_profile_picture", False)),
    }


def normalizar_perfil(user: Any) -> Dict[str, Any]:
    """Campos extra que solo existen tras un user_info (escaneo profundo)."""
    return {
        "media_count": int(getattr(user, "media_count", 0) or 0),
        "follower_count": int(getattr(user, "follower_count", 0) or 0),
        "following_count": int(getattr(user, "following_count", 0) or 0),
        "biography": str(getattr(user, "biography", "") or ""),
        "is_business": bool(getattr(user, "is_business", False)),
    }


def resolver_objetivo(client: Any, target: str) -> Dict[str, str]:
    """Decide qué cuenta se analiza: la tuya (por defecto) o una ajena."""
    target = (target or "").strip().lstrip("@")
    if not target:
        return {"pk": str(client.user_id), "username": str(getattr(client, "username", ""))}
    pk = str(client.user_id_from_username(target))
    return {"pk": pk, "username": target}


def escanear(
    client: Any,
    cfg: Any,
    on_progress: Progress = _noop,
    should_continue: Callable[[], bool] = lambda: True,
) -> Dict[str, Any]:
    """Toma una foto fija completa: seguidores, seguidos y (opcional) perfiles.

    Devuelve el snapshot listo para guardar y analizar.
    """
    scan = cfg.scan
    me = resolver_objetivo(client, scan.target)
    on_progress("INFO", f"Analizando @{me['username']}…")

    on_progress("INFO", "Descargando seguidores…")
    seguidores_raw = client.user_followers(me["pk"], amount=scan.followers_amount or 0) or {}
    seguidores = {str(pk): normalizar(u) for pk, u in seguidores_raw.items()}
    on_progress("INFO", f"{len(seguidores)} seguidores.")

    if not should_continue():
        raise InterruptedError("Escaneo detenido por el usuario.")

    # Pequeña pausa entre las dos peticiones grandes: no hay ninguna prisa.
    time.sleep(random.uniform(2.0, 5.0))

    on_progress("INFO", "Descargando a quién sigues…")
    siguiendo_raw = client.user_following(me["pk"], amount=scan.following_amount or 0) or {}
    siguiendo = {str(pk): normalizar(u) for pk, u in siguiendo_raw.items()}
    on_progress("INFO", f"Sigues a {len(siguiendo)} cuentas.")

    snapshot = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "me": me,
        "followers": seguidores,
        "following": siguiendo,
        "deep": {},
    }

    if scan.deep_scan:
        snapshot["deep"] = _escaneo_profundo(
            client, cfg, seguidores, on_progress, should_continue
        )
        # El enriquecido se mezcla dentro de cada ficha para que el análisis
        # no tenga que saber de dónde vino cada campo.
        for pk, extra in snapshot["deep"].items():
            if pk in snapshot["followers"]:
                snapshot["followers"][pk].update(extra)

    return snapshot


def _escaneo_profundo(
    client: Any,
    cfg: Any,
    seguidores: Dict[str, Dict],
    on_progress: Progress,
    should_continue: Callable[[], bool],
) -> Dict[str, Dict]:
    """Pide user_info de las cuentas con más pinta de sospechosas.

    No recorre todos los seguidores: prioriza a quienes ya tienen indicios
    baratos (sin foto, sin nombre, usuario con dígitos), porque son las
    únicas en las que los datos caros cambian la conclusión.
    """
    from .analysis import score_sospecha

    scan = cfg.scan
    candidatos = sorted(
        seguidores.items(),
        key=lambda kv: -score_sospecha(kv[1], cfg.suspects)[0],
    )
    limite = max(0, scan.deep_scan_limit)
    candidatos = [kv for kv in candidatos if not cfg.is_whitelisted(kv[1].get("username", ""))]
    candidatos = candidatos[:limite]

    if not candidatos:
        return {}

    on_progress("INFO", f"Escaneo profundo de {len(candidatos)} cuentas (las de más indicios)…")
    enriquecidos: Dict[str, Dict] = {}

    for i, (pk, rec) in enumerate(candidatos, start=1):
        if not should_continue():
            on_progress("WARNING", "Escaneo profundo interrumpido. Se conserva lo ya obtenido.")
            break
        try:
            perfil = client.user_info(pk)
            enriquecidos[pk] = normalizar_perfil(perfil)
        except Exception as exc:  # noqa: BLE001
            # Una cuenta que falla no puede tumbar el escaneo entero: se
            # anota y se sigue. Puede estar borrada, bloqueada o privada.
            on_progress("WARNING", f"No se pudo leer @{rec.get('username','?')}: {exc}")

        if i % 10 == 0:
            on_progress("INFO", f"Escaneo profundo: {i}/{len(candidatos)}")

        if scan.pause_every and i % scan.pause_every == 0:
            pausa = random.uniform(scan.long_pause_min, scan.long_pause_max)
            on_progress("INFO", f"Pausa larga de {pausa:.0f}s (cada {scan.pause_every} cuentas).")
            time.sleep(pausa)
        else:
            time.sleep(random.uniform(scan.deep_min_delay, scan.deep_max_delay))

    return enriquecidos
