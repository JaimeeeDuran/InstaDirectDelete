"""Carga, validación y guardado de la configuración.

El config es la única fuente de verdad del comportamiento del stack. El
dashboard lo lee y lo escribe (haciendo backup antes de cada guardado).
"""
from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# ---------------------------------------------------------------------------
# Bloques de configuración
# ---------------------------------------------------------------------------


@dataclass
class MatchCriteria:
    """Criterios de coincidencia. Dentro de un bloque se aplican en AND."""
    catch_all: bool = False
    types: List[str] = field(default_factory=list)
    keywords_any: List[str] = field(default_factory=list)
    keywords_all: List[str] = field(default_factory=list)
    regex_any: List[str] = field(default_factory=list)
    before: str = ""
    after: str = ""
    min_length: int = 0
    max_length: int = 0
    threads: List[str] = field(default_factory=list)
    usernames: List[str] = field(default_factory=list)
    is_group: Optional[bool] = None
    case_sensitive: bool = False

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "MatchCriteria":
        raw = dict(raw or {})
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass
class Rule:
    name: str = "sin nombre"
    action: str = "keep"          # delete | keep
    enabled: bool = True
    when: MatchCriteria = field(default_factory=MatchCriteria)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Rule":
        return cls(
            name=str(raw.get("name", "sin nombre")),
            action=str(raw.get("action", "keep")).lower(),
            enabled=bool(raw.get("enabled", True)),
            when=MatchCriteria.from_dict(raw.get("when")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "action": self.action,
            "enabled": self.enabled,
            "when": _clean(asdict(self.when)),
        }


@dataclass
class Whitelist:
    """Protección absoluta. Gana sobre cualquier regla."""
    message_ids: List[str] = field(default_factory=list)
    protect_groups: bool = False
    match: MatchCriteria = field(default_factory=MatchCriteria)

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "Whitelist":
        raw = dict(raw or {})
        # Atajo cómodo: los campos de criterio se pueden poner al nivel raíz
        # de `whitelist:` sin anidarlos bajo `match:`.
        nested = raw.get("match")
        if nested is None:
            nested = {
                k: v for k, v in raw.items()
                if k in MatchCriteria.__dataclass_fields__  # type: ignore[attr-defined]
            }
        return cls(
            message_ids=[str(m) for m in raw.get("message_ids", []) or []],
            protect_groups=bool(raw.get("protect_groups", False)),
            match=MatchCriteria.from_dict(nested),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_ids": self.message_ids,
            "protect_groups": self.protect_groups,
            "match": _clean(asdict(self.match)),
        }


@dataclass
class RulesCfg:
    default_action: str = "keep"
    items: List[Rule] = field(default_factory=list)


@dataclass
class AccountCfg:
    username: str = ""
    password: str = ""
    totp_seed: str = ""


@dataclass
class SessionCfg:
    file: str = "session.json"


@dataclass
class ScopeCfg:
    """Qué se descarga de Instagram antes siquiera de filtrar."""
    threads_amount: int = 0          # 0 = todos
    messages_per_thread: int = 0     # 0 = todos
    only_thread_ids: List[str] = field(default_factory=list)
    skip_thread_ids: List[str] = field(default_factory=list)
    include_requests: bool = False   # bandeja de solicitudes


@dataclass
class BehaviorCfg:
    dry_run: bool = True
    daily_limit: int = 200
    min_delay: float = 3.0
    max_delay: float = 10.0
    pause_every: int = 40
    long_pause_min: float = 45.0
    long_pause_max: float = 90.0
    max_retries: int = 3
    backoff_base: float = 30.0
    rate_limit_cooldown: float = 300.0
    stop_on_consecutive_errors: int = 5


@dataclass
class DashboardCfg:
    host: str = "127.0.0.1"
    port: int = 8787
    auth_token: str = ""          # vacío => se genera uno al arrancar
    open_browser: bool = True


@dataclass
class StateCfg:
    file: str = "state.json"


@dataclass
class AuditCfg:
    file: str = "audit.jsonl"
    record_keeps: bool = True     # registrar también lo que NO se borra


@dataclass
class LoggingCfg:
    file: str = "unsender.log"
    level: str = "INFO"


@dataclass
class Config:
    account: AccountCfg
    session: SessionCfg
    dashboard: DashboardCfg
    scope: ScopeCfg
    behavior: BehaviorCfg
    whitelist: Whitelist
    rules: RulesCfg
    state: StateCfg
    audit: AuditCfg
    logging: LoggingCfg
    base_dir: Path
    source_path: Path

    def path(self, filename: str) -> Path:
        p = Path(filename).expanduser()
        return p if p.is_absolute() else self.base_dir / p

    # -- serialización -----------------------------------------------------
    def to_dict(self, redact: bool = False) -> Dict[str, Any]:
        account = asdict(self.account)
        if redact:
            account["password"] = "********" if account["password"] else ""
            account["totp_seed"] = "********" if account["totp_seed"] else ""
        return {
            "account": account,
            "session": asdict(self.session),
            "dashboard": asdict(self.dashboard),
            "scope": asdict(self.scope),
            "behavior": asdict(self.behavior),
            "whitelist": self.whitelist.to_dict(),
            "rules": {
                "default_action": self.rules.default_action,
                "items": [r.to_dict() for r in self.rules.items],
            },
            "state": asdict(self.state),
            "audit": asdict(self.audit),
            "logging": asdict(self.logging),
        }


def _clean(d: Dict[str, Any]) -> Dict[str, Any]:
    """Quita valores vacíos para que el YAML guardado no sea un muro de ruido."""
    out = {}
    for k, v in d.items():
        if v in (None, "", [], 0, False):
            continue
        out[k] = v
    return out


# ---------------------------------------------------------------------------
# Carga / guardado
# ---------------------------------------------------------------------------

def _section(raw: Dict[str, Any], key: str, cls):
    defaults = cls().__dict__
    provided = raw.get(key) or {}
    merged = {**defaults, **{k: v for k, v in provided.items() if k in defaults}}
    return cls(**merged)


def load_config(config_path: str = "config.yaml") -> Config:
    path = Path(config_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(
            f"No se encuentra {path}. Copia config.example.yaml a config.yaml y rellénalo."
        )

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    account = _section(raw, "account", AccountCfg)
    # Permitir credenciales por variable de entorno (mejor práctica en servidor).
    account.username = os.environ.get("IG_USERNAME", account.username)
    account.password = os.environ.get("IG_PASSWORD", account.password)
    account.totp_seed = os.environ.get("IG_TOTP_SEED", account.totp_seed)

    rules_raw = raw.get("rules") or {}
    if isinstance(rules_raw, list):          # forma corta: rules: [ ... ]
        rules_items, default_action = rules_raw, "keep"
    else:
        rules_items = rules_raw.get("items", []) or []
        default_action = str(rules_raw.get("default_action", "keep")).lower()

    return Config(
        account=account,
        session=_section(raw, "session", SessionCfg),
        dashboard=_section(raw, "dashboard", DashboardCfg),
        scope=_section(raw, "scope", ScopeCfg),
        behavior=_section(raw, "behavior", BehaviorCfg),
        whitelist=Whitelist.from_dict(raw.get("whitelist")),
        rules=RulesCfg(
            default_action=default_action,
            items=[Rule.from_dict(r) for r in rules_items],
        ),
        state=_section(raw, "state", StateCfg),
        audit=_section(raw, "audit", AuditCfg),
        logging=_section(raw, "logging", LoggingCfg),
        base_dir=path.parent,
        source_path=path,
    )


HEADER = """\
# ---------------------------------------------------------------------------
# ig-unsender — configuración
# Guardado automáticamente desde el dashboard el {ts}
# Se conserva una copia del archivo anterior en config.yaml.bak
# Documentación completa de cada campo: ver config.example.yaml
# ---------------------------------------------------------------------------
"""


def save_config(cfg: Config, data: Dict[str, Any]) -> None:
    """Guarda cambios en el YAML, haciendo backup del anterior.

    Las credenciales redactadas ("********") que llegan del dashboard se
    ignoran para no machacar la contraseña real.
    """
    current = cfg.to_dict()
    merged = _deep_merge(current, data)

    account = merged.get("account", {})
    for key in ("password", "totp_seed"):
        if account.get(key) == "********":
            account[key] = current["account"][key]

    target = cfg.source_path
    if target.exists():
        shutil.copy2(target, target.with_suffix(target.suffix + ".bak"))

    body = yaml.safe_dump(merged, allow_unicode=True, sort_keys=False, indent=2)
    header = HEADER.format(ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    target.write_text(header + "\n" + body, encoding="utf-8")


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
