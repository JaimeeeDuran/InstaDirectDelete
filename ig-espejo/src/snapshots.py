"""Histórico de fotos fijas.

Cada escaneo se guarda como un JSON con fecha en el nombre. El histórico es
lo que convierte esta herramienta en algo más que una lista: sin dos fotos
no hay "te dejó de seguir", y sin tres no hay "volvió".

Escritura atómica (.tmp + replace) por la misma razón que en ig-unsender: un
corte a mitad no puede dejar un archivo a medias.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("ig-espejo.snapshots")

SUFIJO = ".json"


class SnapshotStore:
    def __init__(self, directory: Path, keep: int = 60):
        self.dir = Path(directory)
        self.keep = keep

    # -- escritura ---------------------------------------------------------
    def save(self, snapshot: Dict[str, Any]) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        # ':' no vale en nombres de archivo en Windows; el ts va saneado.
        nombre = (snapshot.get("ts") or "").replace(":", "-") or "sin-fecha"
        destino = self.dir / f"{nombre}{SUFIJO}"
        tmp = destino.with_suffix(destino.suffix + ".tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(destino)
        self._prune()
        return destino

    def _prune(self) -> None:
        archivos = self.paths()
        sobran = len(archivos) - self.keep
        for p in archivos[:max(0, sobran)]:
            try:
                p.unlink()
            except OSError:  # pragma: no cover - carrera improbable
                pass

    # -- lectura -----------------------------------------------------------
    def paths(self) -> List[Path]:
        """Rutas ordenadas de más antigua a más reciente."""
        if not self.dir.exists():
            return []
        return sorted(p for p in self.dir.glob(f"*{SUFIJO}") if p.is_file())

    def load(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Snapshot ilegible %s: %s", path.name, exc)
            return None

    def all(self) -> List[Dict[str, Any]]:
        """Todos los snapshots, de más antiguo a más reciente."""
        salida = []
        for p in self.paths():
            snap = self.load(p)
            if snap:
                salida.append(snap)
        return salida

    def latest(self) -> Optional[Dict[str, Any]]:
        rutas = self.paths()
        return self.load(rutas[-1]) if rutas else None

    def previous(self) -> Optional[Dict[str, Any]]:
        """El anterior al último: con el que se comparan los movimientos."""
        rutas = self.paths()
        return self.load(rutas[-2]) if len(rutas) >= 2 else None

    def count(self) -> int:
        return len(self.paths())
