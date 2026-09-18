"""Audit trail append-only en formato JSONL.

Cada decisión del motor (borrado, protegido, descartado, error) queda
registrada con su motivo. Es el registro que puedes exportar, auditar y
enseñar si alguien pregunta qué se hizo y por qué.

Append-only a propósito: el stack nunca reescribe ni borra líneas.
"""
from __future__ import annotations

import csv
import io
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

FIELDS = [
    "ts", "run_id", "action", "reason", "rule", "dry_run",
    "thread_id", "thread_title", "message_id", "message_type",
    "message_ts", "preview", "error",
]


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, **fields: Any) -> Dict[str, Any]:
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        entry.update({k: v for k, v in fields.items() if v is not None})
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return entry

    # -- lectura -----------------------------------------------------------
    def _iter_lines(self) -> Iterator[Dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def query(
        self,
        limit: int = 200,
        search: str = "",
        action: str = "",
        run_id: str = "",
    ) -> List[Dict[str, Any]]:
        needle = search.strip().lower()
        results: List[Dict[str, Any]] = []
        for entry in self._iter_lines():
            if action and entry.get("action") != action:
                continue
            if run_id and entry.get("run_id") != run_id:
                continue
            if needle:
                blob = " ".join(str(v) for v in entry.values()).lower()
                if needle not in blob:
                    continue
            results.append(entry)
        return results[-limit:][::-1]  # más recientes primero

    def summary(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        runs: Dict[str, Dict[str, Any]] = {}
        total = 0
        for entry in self._iter_lines():
            total += 1
            action = entry.get("action", "?")
            counts[action] = counts.get(action, 0) + 1
            rid = entry.get("run_id")
            if rid:
                run = runs.setdefault(rid, {"run_id": rid, "first": entry["ts"], "count": 0,
                                            "deleted": 0, "dry_run": entry.get("dry_run")})
                run["count"] += 1
                run["last"] = entry["ts"]
                if action == "deleted":
                    run["deleted"] += 1
        return {
            "total": total,
            "by_action": counts,
            "runs": sorted(runs.values(), key=lambda r: r["first"], reverse=True)[:20],
        }

    def to_csv(self, run_id: str = "", action: str = "") -> str:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for entry in self._iter_lines():
            if run_id and entry.get("run_id") != run_id:
                continue
            if action and entry.get("action") != action:
                continue
            writer.writerow(entry)
        return buf.getvalue()

    @property
    def size_bytes(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0
