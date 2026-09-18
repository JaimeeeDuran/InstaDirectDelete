"""API HTTP y servidor del panel de ig-espejo (FastAPI).

Misma postura de seguridad que ig-unsender: escucha solo en 127.0.0.1 por
defecto y exige token, porque maneja tu sesión de Instagram.

Esta herramienta es de SOLO LECTURA: no sigue, no deja de seguir, no bloquea
y no borra nada. No hay ningún endpoint que modifique tu cuenta.
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

from ..analysis import to_csv
from ..config import save_config
from ..runner import ScanRunner

log = logging.getLogger("ig-espejo.web")

STATIC_DIR = Path(__file__).parent / "static"

LISTAS_EXPORTABLES = {
    "mutuos": ("listas", "mutuos"),
    "no_te_siguen": ("listas", "no_te_siguen"),
    "fans": ("listas", "fans"),
    "nuevos": ("movimientos", "nuevos"),
    "perdidos": ("movimientos", "perdidos"),
    "recuperados": ("movimientos", "recuperados"),
    "sospechosos": (None, "sospechosos"),
}


def create_app(runner: ScanRunner, token: str) -> FastAPI:
    app = FastAPI(title="ig-espejo", docs_url=None, redoc_url=None)

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
            supplied = request.cookies.get("espejo_token")
        if not supplied or not secrets.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="Token inválido o ausente.")

    auth = [Depends(require_token)]

    # ------------------------------------------------------------- estático
    @app.get("/", include_in_schema=False)
    async def index(token_q: Optional[str] = Query(None, alias="token")):
        response = FileResponse(STATIC_DIR / "index.html")
        if token_q and secrets.compare_digest(token_q, token):
            response.set_cookie("espejo_token", token_q, httponly=False, samesite="strict")
        return response

    @app.get("/health", include_in_schema=False)
    async def health():
        return {"ok": True, "state": runner.state}

    # -------------------------------------------------------------- estado
    @app.get("/api/status", dependencies=auth)
    async def status():
        return runner.status()

    @app.get("/api/report", dependencies=auth)
    async def report():
        rep = runner.last_report()
        if rep is None:
            raise HTTPException(
                status_code=404,
                detail="Todavía no hay ningún escaneo. Pulsa Escanear para tomar la primera foto.",
            )
        return rep

    @app.get("/api/history", dependencies=auth)
    async def history():
        return {"serie": runner.history()}

    @app.get("/api/report.csv", dependencies=auth)
    async def report_csv(lista: str = "no_te_siguen"):
        if lista not in LISTAS_EXPORTABLES:
            raise HTTPException(status_code=400, detail=f"Lista desconocida: {lista}")
        rep = runner.last_report()
        if rep is None:
            raise HTTPException(status_code=404, detail="No hay ningún escaneo todavía.")
        seccion, clave = LISTAS_EXPORTABLES[lista]
        registros = rep[seccion][clave] if seccion else rep[clave]
        return PlainTextResponse(
            to_csv(registros),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="espejo-{lista}.csv"'},
        )

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
        return {"ok": True}

    # ------------------------------------------------------------- control
    @app.post("/api/scan", dependencies=auth)
    async def scan(payload: Optional[Dict[str, Any]] = None):
        payload = payload or {}
        result = runner.start(deep=payload.get("deep"))
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("error"))
        return result

    @app.post("/api/stop", dependencies=auth)
    async def stop():
        result = runner.stop()
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("error"))
        return result

    # --------------------------------------------------------- eventos SSE
    @app.get("/api/events", dependencies=auth)
    async def events(request: Request):
        queue = runner.bus.subscribe()

        async def stream():
            try:
                for event in runner.bus.history():
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
