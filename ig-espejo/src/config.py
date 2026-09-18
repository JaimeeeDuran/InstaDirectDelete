"""Carga, validación y guardado de la configuración de ig-espejo.

Mismo patrón que ig-unsender: dataclasses como única fuente de verdad, YAML
legible que el usuario puede editar a mano, y credenciales por variable de
entorno con prioridad sobre el archivo.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import yaml


@dataclass
class AccountCfg:
    username: str = ""
    password: str = ""
    totp_seed: str = ""


@dataclass
class SessionCfg:
    """Sesión persistente.

    Por defecto apunta a la sesión de ig-unsender (un directorio más arriba):
    reutilizar el mismo login es justo lo que evita levantar sospechas. Si no
    existe, se crea una nueva ahí mismo y ambas herramientas la comparten.
    """
    file: str = "../session.json"


@dataclass
class ScanCfg:
    """Qué se descarga y a qué ritmo."""
    target: str = ""              # vacío = tu propia cuenta
    followers_amount: int = 0     # 0 = todos
    following_amount: int = 0     # 0 = todos
    # El escaneo profundo pide user_info CUENTA POR CUENTA: es lo que permite
    # saber nº de publicaciones, bio y ratios, pero es también lo que más
    # rápido te lleva a un rate-limit. Por eso va desactivado y acotado.
    deep_scan: bool = False
    deep_scan_limit: int = 60     # cuántas cuentas enriquecer como máximo
    deep_min_delay: float = 2.0   # pausa entre peticiones del escaneo profundo
    deep_max_delay: float = 5.0
    pause_every: int = 25         # cada N cuentas, pausa larga
    long_pause_min: float = 20.0
    long_pause_max: float = 45.0


@dataclass
class SuspectCfg:
    """Heurística de cuentas sospechosas.

    Cada señal suma puntos y aporta un motivo legible. No es un detector de
    bots infalible y no pretende serlo: es una lista de indicios ordenada,
    para que el usuario decida mirando los motivos, no la puntuación.
    """
    enabled: bool = True
    umbral_medio: int = 35
    umbral_alto: int = 60
    # Señales baratas (salen de la propia lista de seguidores)
    peso_sin_foto: int = 30
    peso_username_numerico: int = 20
    peso_sin_nombre: int = 10
    # Señales caras (requieren escaneo profundo)
    peso_sin_publicaciones: int = 25
    peso_sin_bio: int = 10
    peso_ratio_extremo: int = 25
    ratio_extremo: float = 20.0   # sigue a >20x las cuentas que le siguen
    minimo_digitos: int = 4       # dígitos seguidos en el username para marcar


@dataclass
class WhitelistCfg:
    """Intocables: nunca se marcan como sospechosos ni se sugiere nada sobre
    ellos. Tu gente. La lista se compara siempre en minúsculas."""
    usernames: List[str] = field(default_factory=list)


@dataclass
class SnapshotsCfg:
    dir: str = "snapshots"
    keep: int = 60                # cuántas fotos fijas conservar


@dataclass
class DashboardCfg:
    host: str = "127.0.0.1"
    port: int = 8788              # distinto del 8787 de ig-unsender
    auth_token: str = ""
    open_browser: bool = True


@dataclass
class LoggingCfg:
    file: str = "espejo.log"
    level: str = "INFO"


@dataclass
class Config:
    account: AccountCfg
    session: SessionCfg
    scan: ScanCfg
    suspects: SuspectCfg
    whitelist: WhitelistCfg
    snapshots: SnapshotsCfg
    dashboard: DashboardCfg
    logging: LoggingCfg
    base_dir: Path
    source_path: Path

    def path(self, filename: str) -> Path:
        p = Path(filename).expanduser()
        return p if p.is_absolute() else (self.base_dir / p).resolve()

    def is_whitelisted(self, username: str) -> bool:
        low = (username or "").lower().lstrip("@")
        return low in {u.lower().lstrip("@") for u in self.whitelist.usernames if u}

    def to_dict(self, redact: bool = False) -> Dict[str, Any]:
        account = asdict(self.account)
        if redact:
            account["password"] = "********" if account["password"] else ""
            account["totp_seed"] = "********" if account["totp_seed"] else ""
        return {
            "account": account,
            "session": asdict(self.session),
            "scan": asdict(self.scan),
            "suspects": asdict(self.suspects),
            "whitelist": asdict(self.whitelist),
            "snapshots": asdict(self.snapshots),
            "dashboard": asdict(self.dashboard),
            "logging": asdict(self.logging),
        }


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
    account.username = os.environ.get("IG_USERNAME", account.username)
    account.password = os.environ.get("IG_PASSWORD", account.password)
    account.totp_seed = os.environ.get("IG_TOTP_SEED", account.totp_seed)

    return Config(
        account=account,
        session=_section(raw, "session", SessionCfg),
        scan=_section(raw, "scan", ScanCfg),
        suspects=_section(raw, "suspects", SuspectCfg),
        whitelist=_section(raw, "whitelist", WhitelistCfg),
        snapshots=_section(raw, "snapshots", SnapshotsCfg),
        dashboard=_section(raw, "dashboard", DashboardCfg),
        logging=_section(raw, "logging", LoggingCfg),
        base_dir=path.parent,
        source_path=path,
    )


HEADER = """\
# ---------------------------------------------------------------------------
# ig-espejo — configuración
# Guardado automáticamente desde el dashboard el {ts}
# Se conserva una copia del archivo anterior en config.yaml.bak
# Documentación completa de cada campo: ver config.example.yaml
# ---------------------------------------------------------------------------
"""


def save_config(cfg: Config, data: Dict[str, Any]) -> None:
    """Guarda cambios en el YAML, con backup previo.

    Las credenciales redactadas ("********") se ignoran para no machacar la
    contraseña real, igual que hace ig-unsender.
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
