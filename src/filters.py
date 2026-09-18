"""Clasificación de mensajes y motor de decisión (whitelist + reglas).

Jerarquía de decisión, de mayor a menor prioridad:

  1. WHITELIST  -> PROTECTED. Nunca se toca. Gana sobre todo lo demás.
  2. REGLAS     -> se evalúan EN ORDEN; la primera que casa decide (keep/delete).
  3. DEFECTO    -> `rules.default_action` (por defecto: keep, o sea, no tocar).

Toda decisión lleva un motivo legible, que es lo que va al audit trail.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, List, Optional

from .config import Config, MatchCriteria, Rule

# ---------------------------------------------------------------------------
# Clasificación de tipos de mensaje
# ---------------------------------------------------------------------------
# instagrapi devuelve `item_type` crudo de la API privada. Lo normalizamos a
# categorías que un humano entiende y puede poner en el config.

ITEM_TYPE_MAP = {
    "text": "text",
    "link": "link",
    "voice_media": "voice",
    "animated_media": "gif",
    "media_share": "share",
    "xma_media_share": "share",
    "generic_xma": "share",
    "story_share": "story",
    "reel_share": "story",
    "clip": "reel",
    "xma_reel_share": "reel",
    "felix_share": "video",
    "raven_media": "disappearing",
    "visual_media": "disappearing",
    "like": "like",
    "location": "location",
    "profile": "profile",
    "video_call_event": "call",
    "action_log": "system",
    "placeholder": "other",
}

# Categorías válidas para `types:` en el config. Documentadas en el YAML.
KNOWN_TYPES = [
    "text", "photo", "video", "voice", "gif", "link", "share",
    "story", "reel", "disappearing", "like", "location", "profile",
    "call", "system", "other",
]

# media_type de Instagram dentro de un item_type == "media"
_MEDIA_TYPE = {1: "photo", 2: "video", 8: "photo"}  # 8 = carrusel/álbum


def classify(msg: Any) -> str:
    """Devuelve la categoría normalizada de un mensaje."""
    item_type = (getattr(msg, "item_type", "") or "").lower()

    if item_type == "media":
        media = getattr(msg, "media", None)
        mt = getattr(media, "media_type", None) if media else None
        return _MEDIA_TYPE.get(mt, "photo")

    return ITEM_TYPE_MAP.get(item_type, "other")


def extract_text(msg: Any) -> str:
    """Junta todo el texto buscable de un mensaje (cuerpo, caption, link)."""
    parts: List[str] = []

    text = getattr(msg, "text", None)
    if text:
        parts.append(str(text))

    link = getattr(msg, "link", None)
    if link is not None:
        for attr in ("text", "link_url"):
            val = getattr(link, attr, None)
            if val:
                parts.append(str(val))
        ctx = getattr(link, "link_context", None)
        if ctx is not None:
            for attr in ("link_title", "link_url", "link_summary"):
                val = getattr(ctx, attr, None)
                if val:
                    parts.append(str(val))

    media = getattr(msg, "media", None)
    if media is not None:
        caption = getattr(media, "caption_text", None)
        if caption:
            parts.append(str(caption))

    return " ".join(parts).strip()


def preview(msg: Any, limit: int = 70) -> str:
    """Resumen corto y seguro de un mensaje, para logs y dashboard."""
    text = extract_text(msg).replace("\n", " ").strip()
    if text:
        return (text[: limit - 1] + "…") if len(text) > limit else text
    return f"<{classify(msg)}>"


# ---------------------------------------------------------------------------
# Contexto de evaluación
# ---------------------------------------------------------------------------

@dataclass
class MessageCtx:
    """Todo lo que el motor necesita para decidir sobre un mensaje."""
    message_id: str
    thread_id: str
    thread_title: str
    is_group: bool
    participants: List[str]          # usernames en minúscula
    msg_type: str
    text: str
    timestamp: Optional[datetime]
    length: int
    preview: str

    @classmethod
    def build(cls, msg: Any, thread: Any) -> "MessageCtx":
        ts = getattr(msg, "timestamp", None)
        if isinstance(ts, datetime) and ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)

        users = getattr(thread, "users", None) or []
        participants = [
            str(getattr(u, "username", "")).lower()
            for u in users
            if getattr(u, "username", None)
        ]

        text = extract_text(msg)
        return cls(
            message_id=str(getattr(msg, "id", "")),
            thread_id=str(getattr(thread, "id", "")),
            thread_title=_thread_title(thread),
            is_group=bool(getattr(thread, "is_group", False)),
            participants=participants,
            msg_type=classify(msg),
            text=text,
            timestamp=ts if isinstance(ts, datetime) else None,
            length=len(text),
            preview=preview(msg),
        )


def _thread_title(thread: Any) -> str:
    title = getattr(thread, "thread_title", None)
    if title:
        return str(title)
    users = getattr(thread, "users", None) or []
    names = [str(getattr(u, "username", "?")) for u in users[:3]]
    return ", ".join(names) or str(getattr(thread, "id", "?"))


# ---------------------------------------------------------------------------
# Decisión
# ---------------------------------------------------------------------------

PROTECTED = "protected"   # whitelist: intocable
DELETE = "delete"
KEEP = "keep"


@dataclass
class Decision:
    action: str          # protected | delete | keep
    reason: str          # motivo legible, va al audit trail
    rule: str = ""       # nombre de la regla que decidió

    @property
    def deletes(self) -> bool:
        return self.action == DELETE


# ---------------------------------------------------------------------------
# Evaluación de criterios
# ---------------------------------------------------------------------------

def _norm(values: Iterable[str]) -> List[str]:
    return [str(v).strip().lower() for v in values if str(v).strip()]


def _evaluate(c: MatchCriteria, ctx: MessageCtx) -> List[tuple[bool, str]]:
    """Evalúa cada criterio DEFINIDO por separado.

    Devuelve una lista de (casa, motivo_legible). Los criterios que el usuario
    ha dejado vacíos no aparecen: no opinan.
    """
    checks: List[tuple[bool, str]] = []
    haystack = ctx.text if c.case_sensitive else ctx.text.lower()

    if c.types:
        checks.append((ctx.msg_type in _norm(c.types), f"tipo={ctx.msg_type}"))

    if c.keywords_any:
        needles = c.keywords_any if c.case_sensitive else _norm(c.keywords_any)
        found = [n for n in needles if n and n in haystack]
        checks.append((bool(found), f"palabra='{found[0]}'" if found else "palabra"))

    if c.keywords_all:
        needles = [n for n in (c.keywords_all if c.case_sensitive else _norm(c.keywords_all)) if n]
        checks.append((all(n in haystack for n in needles), f"contiene las {len(needles)}"))

    if c.regex_any:
        flags = 0 if c.case_sensitive else re.IGNORECASE
        matched = None
        for pattern in c.regex_any:
            try:
                if re.search(pattern, ctx.text, flags):
                    matched = pattern
                    break
            except re.error:
                continue  # patrón inválido: se ignora aquí, se avisa en validate()
        checks.append((matched is not None, f"regex='{matched}'" if matched else "regex"))

    if c.before:
        try:
            limit = datetime.strptime(c.before, "%Y-%m-%d")
            ok = ctx.timestamp is not None and ctx.timestamp < limit
        except ValueError:
            ok = False
        checks.append((ok, f"anterior a {c.before}"))

    if c.after:
        try:
            limit = datetime.strptime(c.after, "%Y-%m-%d")
            ok = ctx.timestamp is not None and ctx.timestamp > limit
        except ValueError:
            ok = False
        checks.append((ok, f"posterior a {c.after}"))

    if c.min_length:
        checks.append((ctx.length >= c.min_length, f"longitud>={c.min_length}"))

    if c.max_length:
        checks.append((ctx.length <= c.max_length, f"longitud<={c.max_length}"))

    if c.threads:
        wanted = {str(t).strip() for t in c.threads}
        checks.append((ctx.thread_id in wanted, "chat listado"))

    if c.usernames:
        wanted = set(_norm(c.usernames))
        common = sorted(wanted.intersection(ctx.participants))
        checks.append((bool(common), f"usuario={common[0]}" if common else "usuario"))

    if c.is_group is not None:
        checks.append((ctx.is_group == c.is_group,
                       "grupo" if c.is_group else "chat individual"))

    return checks


def _match_criteria(c: MatchCriteria, ctx: MessageCtx, mode: str = "and") -> Optional[str]:
    """Combina los criterios de un bloque y devuelve el motivo si casa.

    mode="and"  -> REGLAS. Todos los criterios rellenados deben cumplirse.
                   Así puedes pedir "fotos Y anteriores a 2025".
    mode="or"   -> WHITELIST. Basta con que uno se cumpla para proteger.
                   Así "proteger a mi_pareja" y "proteger notas de voz" son
                   dos escudos independientes, no una condición conjunta.
    """
    if c.catch_all:
        return "catch-all"

    checks = _evaluate(c, ctx)
    if not checks:
        return None

    if mode == "or":
        hits = [reason for ok, reason in checks if ok]
        return hits[0] if hits else None

    if all(ok for ok, _ in checks):
        return ", ".join(reason for _, reason in checks)
    return None


# ---------------------------------------------------------------------------
# Motor
# ---------------------------------------------------------------------------

class FilterEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.whitelist = cfg.whitelist
        self.rules: List[Rule] = [r for r in cfg.rules.items if r.enabled]
        self.default_action = cfg.rules.default_action

    # -- whitelist ---------------------------------------------------------
    def _whitelisted(self, ctx: MessageCtx) -> Optional[str]:
        wl = self.whitelist

        if ctx.message_id in [str(m).strip() for m in wl.message_ids]:
            return "ID de mensaje en whitelist"

        if wl.protect_groups and ctx.is_group:
            return "grupo protegido (protect_groups)"

        # OR: cada protección de la whitelist actúa por su cuenta.
        reason = _match_criteria(wl.match, ctx, mode="or")
        if reason:
            return f"whitelist: {reason}"

        return None

    # -- decisión ----------------------------------------------------------
    def decide(self, ctx: MessageCtx) -> Decision:
        protected = self._whitelisted(ctx)
        if protected:
            return Decision(PROTECTED, protected, rule="whitelist")

        for rule in self.rules:
            reason = _match_criteria(rule.when, ctx)
            if reason:
                return Decision(rule.action, reason, rule=rule.name)

        return Decision(
            self.default_action,
            "ninguna regla casó -> acción por defecto",
            rule="(defecto)",
        )

    # -- validación --------------------------------------------------------
    def validate(self) -> List[str]:
        """Devuelve una lista de avisos de configuración (regex rotos, tipos
        desconocidos, reglas inalcanzables...). Se muestran en el dashboard."""
        warnings: List[str] = []

        def check(c: MatchCriteria, label: str) -> None:
            for pattern in c.regex_any:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    warnings.append(f"{label}: regex inválida '{pattern}' ({exc})")
            for t in c.types:
                if str(t).strip().lower() not in KNOWN_TYPES:
                    warnings.append(
                        f"{label}: tipo desconocido '{t}'. Válidos: {', '.join(KNOWN_TYPES)}"
                    )
            for field_name in ("before", "after"):
                val = getattr(c, field_name)
                if val:
                    try:
                        datetime.strptime(val, "%Y-%m-%d")
                    except ValueError:
                        warnings.append(f"{label}: fecha inválida en '{field_name}': {val}")

        check(self.whitelist.match, "whitelist")

        seen_catch_all = False
        for rule in self.rules:
            check(rule.when, f"regla '{rule.name}'")
            if seen_catch_all:
                warnings.append(
                    f"regla '{rule.name}' es inalcanzable: hay un catch_all antes."
                )
            if rule.when.catch_all:
                seen_catch_all = True
            if rule.action not in (DELETE, KEEP):
                warnings.append(
                    f"regla '{rule.name}': acción '{rule.action}' inválida (usa delete o keep)."
                )

        if not self.rules:
            warnings.append("No hay reglas activas: no se borrará nada.")

        return warnings
