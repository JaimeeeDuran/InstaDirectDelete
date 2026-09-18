"""Paginación por cursor de mensajes en hilos largos.

La API privada de Instagram pagina internamente pero en hilos muy largos
no devuelve el histórico completo de forma fiable. Este módulo implementa
paginación explícita por cursor, permitiendo reanudar exactamente donde
se quedó tras un corte.

Se importa de forma perezosa (como el cliente de instagrapi) para que
los tests corran sin necesidad de la librería si no usan paginación.
"""
from __future__ import annotations

import logging
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Optional, Tuple

log = logging.getLogger("ig-unsender.pagination")

# Señal de fin: oldest_cursor vacío o ausente significa "no hay más".
# Si la API trae has_older, úsalo como confirmación, nunca como único criterio.


def _extract_message(item: dict) -> Any:
    """Convierte un item crudo de la API privada en un objeto de mensaje.

    Usa `instagrapi.extractors.extract_direct_message` cuando está disponible
    (es lo verificado contra la API real). Si instagrapi no está instalado —
    por ejemplo, en los tests, que corren sin la librería a propósito — cae
    a un extractor propio y simplificado que solo rellena los campos que
    MessageCtx.build() necesita (todos vía getattr con default, así que un
    objeto plano vale igual que el DirectMessage real de instagrapi).
    """
    try:
        from instagrapi.extractors import extract_direct_message
        return extract_direct_message(item)
    except ImportError:
        pass  # sin instagrapi instalado: usamos el extractor de respaldo

    return SimpleNamespace(
        id=item.get("id") or item.get("item_id", ""),
        item_type=item.get("item_type", ""),
        text=item.get("text"),
        media=item.get("media"),
        link=item.get("link"),
        timestamp=_parse_timestamp(item.get("timestamp")),
        user_id=item.get("user_id", ""),
    )


def _parse_timestamp(raw: Any) -> Optional[datetime]:
    """Normaliza el timestamp crudo del item a datetime.

    La API real de Instagram trae microsegundos Unix (int). En los tests,
    el FakeClient usa cadenas ISO. Aceptamos ambos; si no reconocemos el
    formato, devolvemos None en vez de reventar (MessageCtx.build() ya
    trata None como "sin fecha").
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw / 1_000_000)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    return None


def iter_thread_messages(
    client: Any,
    thread_id: str,
    page_size: int = 20,
    max_pages: int = 0,
    start_cursor: str = "",
    should_continue: Callable[[], bool] | None = None,
) -> Iterator[Tuple[list[Any], str, bool]]:
    """Genera (mensajes_de_página, cursor_siguiente, hay_más) página a página.

    Hace paginación explícita sobre direct_v2/threads/{thread_id}/,
    usando los cursores de la API privada. Permite reanudar desde el último
    cursor guardado sin repetir trabajo.

    Args:
        client: Cliente de instagrapi con método private_request().
        thread_id: ID del hilo a paginar.
        page_size: Mensajes por petición (por defecto 20, igual que instagrapi).
        max_pages: Límite de páginas (0 = sin límite, paginar hasta el fondo).
        start_cursor: Cursor para arrancar (vacío = desde la más nueva).
        should_continue: Callable que devuelve False si toca parar.
                         Se comprueba entre páginas.

    Yields:
        Tuplas (mensajes, cursor_siguiente, hay_más):
        - mensajes: lista de objetos DirectMessage crudos (extraídos de items).
        - cursor_siguiente: cursor para la siguiente página (vacío si fin).
        - hay_más: bool, True si quedan más páginas.

    Excepciones:
        Cualquier excepción de la API privada se propaga (backoff,
        LoginRequired, etc. lo manejan Engine._delete_with_retry y similares).
    """
    if should_continue is None:
        should_continue = lambda: True

    cursor = start_cursor
    pages_fetched = 0

    while should_continue():
        if max_pages > 0 and pages_fetched >= max_pages:
            log.debug(f"Límite de {max_pages} páginas alcanzado en hilo {thread_id}.")
            return

        params = {
            "visual_message_return_type": "unseen",
            "direction": "older",
            "seq_id": "40065",
            "limit": str(page_size),
        }
        if cursor:
            params["cursor"] = cursor

        response = client.private_request(
            f"direct_v2/threads/{thread_id}/",
            params=params,
        )

        thread_data = response.get("thread", {})
        items = thread_data.get("items", [])
        oldest_cursor = thread_data.get("oldest_cursor", "")
        has_older = thread_data.get("has_older", False)

        # Convertir items crudos a objetos de mensaje.
        messages = []
        for item in items:
            try:
                msg = _extract_message(item)
                if msg:
                    messages.append(msg)
            except Exception:  # noqa: BLE001
                # Si un item no se puede extraer, lo ignoramos (rareza de API).
                log.warning(f"No se pudo extraer mensaje en hilo {thread_id}: {item}")

        pages_fetched += 1
        hay_mas = bool(oldest_cursor or has_older)

        log.debug(
            f"Página {pages_fetched} del hilo {thread_id}: "
            f"{len(messages)} mensajes, hay_más={hay_mas}"
        )

        yield messages, oldest_cursor, hay_mas

        if not oldest_cursor and not has_older:
            # Fin del hilo: no hay cursor y no hay_older. Paramos.
            return

        cursor = oldest_cursor


def iter_with_fallback(
    client: Any,
    thread_id: str,
    page_size: int = 20,
    max_pages: int = 0,
    start_cursor: str = "",
    should_continue: Callable[[], bool] | None = None,
    fallback: bool = True,
) -> Iterator[Tuple[list[Any], str, bool, bool]]:
    """Paginación con fallback automático al método antiguo.

    Si la paginación falla (endpoint cambiado, permisos, etc.) ANTES de haber
    emitido ninguna página, intenta revertir a direct_messages() para que la
    herramienta siga funcionando aunque Instagram haya roto el endpoint.

    Importante: si la paginación ya emitió páginas reales y falla a mitad de
    camino, NO caemos a direct_messages() — eso duplicaría mensajes ya vistos
    (direct_messages(amount=0) trae todo desde el principio otra vez). En ese
    caso propagamos la excepción; el cursor de la última página buena ya
    quedó guardado por el llamante, así que la siguiente ejecución retoma
    justo ahí sin repetir trabajo.

    Args:
        fallback: Si True (por defecto), cae a direct_messages() si falla
                  antes de la primera página.

    Yields:
        Tuplas (mensajes, cursor_siguiente, hay_más, es_paginación_real).
        `es_paginación_real` es False cuando la página vino del fallback
        direct_messages(), para que el llamante no confíe en que el hilo
        quedó completamente recorrido.
    """
    pages_yielded = 0
    try:
        for messages, cursor, hay_mas in iter_thread_messages(
            client,
            thread_id,
            page_size=page_size,
            max_pages=max_pages,
            start_cursor=start_cursor,
            should_continue=should_continue,
        ):
            pages_yielded += 1
            yield messages, cursor, hay_mas, True
    except Exception as exc:  # noqa: BLE001
        if not fallback or pages_yielded > 0:
            # Ya paginamos algo de verdad: no mezclamos con el fallback.
            raise
        log.warning(
            f"Paginación fallida en hilo {thread_id}: {exc}. "
            "Cayendo a descarga directa (comportamiento antiguo)."
        )
        # Fallback: usar direct_messages() que pagina internamente pero
        # devuelve todo de una vez (hasta donde Instagram lo devuelva).
        messages = list(client.direct_messages(thread_id, amount=0) or [])
        if should_continue():
            yield messages, "", False, False
