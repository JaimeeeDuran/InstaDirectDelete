"""API HTTP y servidor del dashboard (FastAPI).

Seguridad: el dashboard maneja tu sesión de Instagram, así que por defecto
escucha SOLO en 127.0.0.1 y exige un token. Si expones el puerto a la red,
pon un token largo y ponlo detrás de HTTPS.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse

from ..audit import AuditLog
from ..config import save_config
from ..filters import KNOWN_TYPES
from ..runner import EngineRunner

log = logging.getLogger("ig-unsender.web")

STATIC_DIR = Path(__file__).parent / "static"


def create_app(runner: EngineRunner, token: str) -> FastAPI:
    app = FastAPI(title="ig-unsender", docs_url=None, redoc_url=None)

    # ---------------------------------------------------------------- auth
    def require_token(
        request: Request,
        access_token: Optional[str] = Query(None, alias="token"),
    ) -> None:
        supplied = access_token
        if not supplied:
            header = request.headers.get("authorization", "")
            if header.lower().startswith("bearer "):
                supplied = header[7:]
        if not supplied:
            supplied = request.cookies.get("ig_token")
        if not supplied or not secrets.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="Token inválido o ausente.")

    auth = [Depends(require_token)]

    # ------------------------------------------------------------- estático
    @app.get("/", include_in_schema=False)
    async def index(token_q: Optional[str] = Query(None, alias="token")):
        response = FileResponse(STATIC_DIR / "index.html")
        if token_q and secrets.compare_digest(token_q, token):
            # Cookie de sesión: evita arrastrar el token en cada URL.
            response.set_cookie("ig_token", token_q, httponly=False, samesite="strict")
        return response

    @app.get("/health", include_in_schema=False)
    async def health():
        return {"ok": True, "state": runner.ctl.state.value}

    # -------------------------------------------------------------- estado
    @app.get("/api/status", dependencies=auth)
    async def status():
        return runner.status()

    @app.get("/api/meta", dependencies=auth)
    async def meta():
        return {
            "known_types": KNOWN_TYPES,
            "config_path": str(runner.cfg.source_path),
            "base_dir": str(runner.cfg.base_dir),
        }

    # -------------------------------------------------------------- config
    @app.get("/api/config", dependencies=auth)
    async def get_config():
        runner.reload_config()
        return runner.cfg.to_dict(redact=True)

    @app.post("/api/config", dependencies=auth)
    async def post_config(payload: Dict[str, Any]):
        try:
            save_config(runner.cfg, payload)
            runner.reload_config()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"No se pudo guardar: {exc}") from exc
        return {"ok": True, "warnings": runner.config_warnings()}

    # ------------------------------------------------------------- control
    @app.post("/api/control/start", dependencies=auth)
    async def start(payload: Optional[Dict[str, Any]] = None):
        payload = payload or {}
        dry_run = payload.get("dry_run")

        # Salvaguarda: un borrado real exige confirmación explícita.
        if dry_run is False and payload.get("confirm") != "BORRAR":
            raise HTTPException(
                status_code=400,
                detail="Para el borrado real hay que confirmar escribiendo BORRAR.",
            )

        result = runner.start(dry_run=dry_run)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("error"))
        return result

    @app.post("/api/control/{action}", dependencies=auth)
    async def control(action: str):
        fn = {"pause": runner.pause, "resume": runner.resume, "stop": runner.stop}.get(action)
        if fn is None:
            raise HTTPException(status_code=404, detail=f"Acción desconocida: {action}")
        result = fn()
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("error"))
        return result

    # --------------------------------------------------------------- chats
    @app.get("/api/threads", dependencies=auth)
    async def threads():
        """Inventario de chats para construir la whitelist con un clic."""
        from ..engine import Engine

        if runner.busy:
            raise HTTPException(
                status_code=409,
                detail="Hay una ejecución en marcha. Párala antes de listar chats.",
            )
        try:
            engine = Engine(runner.cfg, runner.bus, runner.stats, runner.ctl,
                            client=runner._client)
            data = await asyncio.to_thread(engine.list_threads)
            runner._client = engine.cl
            return {"threads": data}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ------------------------------------------------------------ auditoría
    @app.get("/api/audit", dependencies=auth)
    async def audit(
        limit: int = 200,
        search: str = "",
        action: str = "",
        run_id: str = "",
    ):
        alog = AuditLog(runner.cfg.path(runner.cfg.audit.file))
        return {
            "entries": alog.query(limit=limit, search=search, action=action, run_id=run_id),
            "summary": alog.summary(),
        }

    @app.get("/api/audit.csv", dependencies=auth)
    async def audit_csv(run_id: str = "", action: str = ""):
        alog = AuditLog(runner.cfg.path(runner.cfg.audit.file))
        csv_data = alog.to_csv(run_id=run_id, action=action)
        return PlainTextResponse(
            csv_data,
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="ig-unsender-audit.csv"'},
        )

    # --------------------------------------------------------- eventos SSE
    @app.get("/api/events", dependencies=auth)
    async def events(request: Request):
        queue = runner.bus.subscribe()

        async def stream():
            try:
                # Contexto inicial para quien se conecta a mitad de partido.
                for event in runner.bus.history(limit=120):
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'kind': 'status', **runner.status()})}\n\n"

                idle = 0
                while True:
                    if await request.is_disconnected():
                        break
                    batch = runner.bus.drain(queue)
                    if batch:
                        idle = 0
                        for event in batch:
                            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    else:
                        idle += 1
                        if idle >= 40:  # keepalive cada ~10s
                            idle = 0
                            yield ": keepalive\n\n"
                    await asyncio.sleep(0.25)
            finally:
                runner.bus.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app
