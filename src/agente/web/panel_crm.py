"""El panel del CRM en línea: https://<dominio del agente>/crm

Lo protege un usuario y una clave (CRM_USUARIO / CRM_CLAVE): el navegador los
pide la primera vez. Sin CRM_CLAVE el panel no existe (da 404): así nunca
queda abierto por olvido.

    GET /crm             el panel
    GET /crm/datos       los números, en JSON (?desde=AAAA-MM-DD&hasta=AAAA-MM-DD)
    GET /crm/leads.csv   la lista de leads del rango, para Excel o Power BI
"""

from __future__ import annotations

import asyncio
import base64
import csv
import io
import logging
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .. import crm
from ..crm.reporte import armar

registro = logging.getLogger(__name__)

PAGINA = Path(__file__).parent / "static" / "crm.html"


def montar_crm(app: FastAPI, chatwoot_url: str = "", cuenta_id: str = "1") -> None:
    def credenciales() -> tuple[str, str]:
        return (
            (os.getenv("CRM_USUARIO") or "admin").strip(),
            (os.getenv("CRM_CLAVE") or "").strip(),
        )

    def autorizado(pedido: Request) -> Response | None:
        """None si puede pasar; si no, la respuesta que corresponde."""
        usuario, clave = credenciales()
        if not clave:
            return JSONResponse({"error": "no encontrado"}, status_code=404)
        cabecera = pedido.headers.get("authorization") or ""
        if cabecera.lower().startswith("basic "):
            try:
                dado_usuario, _, dada_clave = (
                    base64.b64decode(cabecera[6:]).decode("utf-8").partition(":")
                )
            except Exception:  # noqa: BLE001
                dado_usuario, dada_clave = "", ""
            ok_usuario = secrets.compare_digest(dado_usuario.encode(), usuario.encode())
            ok_clave = secrets.compare_digest(dada_clave.encode(), clave.encode())
            if ok_usuario and ok_clave:
                return None
            registro.warning("CRM: usuario o clave equivocados")
        return Response(
            "Usuario o clave del CRM",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="CRM", charset="UTF-8"'},
        )

    async def reporte(pedido: Request) -> dict:
        base = crm.almacen()
        if base is None:
            raise RuntimeError(
                "El CRM no está activo: necesita MODO=produccion con POSTGRES_DSN "
                "(o CRM_SQLITE en pruebas)."
            )
        leads, eventos = await asyncio.gather(
            asyncio.to_thread(base.leads), asyncio.to_thread(base.eventos)
        )
        datos = armar(
            leads,
            eventos,
            pedido.query_params.get("desde", ""),
            pedido.query_params.get("hasta", ""),
        )
        url = (chatwoot_url or "").strip().rstrip("/")
        datos["chatwoot"] = f"{url}/app/accounts/{cuenta_id}/conversations/" if url else ""
        datos["ejecutivo"] = (os.getenv("EJECUTIVO_VENTAS") or "Juan de Dios Rodríguez").strip()
        return datos

    sin_cache = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}

    @app.get("/crm")
    async def panel(pedido: Request) -> Response:
        rechazo = autorizado(pedido)
        if rechazo is not None:
            return rechazo
        return HTMLResponse(PAGINA.read_text(encoding="utf-8"), headers=sin_cache)

    @app.get("/crm/datos")
    async def datos(pedido: Request) -> Response:
        rechazo = autorizado(pedido)
        if rechazo is not None:
            return rechazo
        try:
            return JSONResponse(await reporte(pedido), headers=sin_cache)
        except Exception as e:  # noqa: BLE001
            registro.error("CRM: no se pudo armar el reporte: %s", e)
            return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)

    @app.get("/crm/leads.csv")
    async def exportar(pedido: Request) -> Response:
        rechazo = autorizado(pedido)
        if rechazo is not None:
            return rechazo
        try:
            filas = (await reporte(pedido))["leads"]
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
        salida = io.StringIO()
        columnas = list(filas[0].keys()) if filas else ["conversacion"]
        escritor = csv.DictWriter(salida, fieldnames=columnas, delimiter=";")
        escritor.writeheader()
        escritor.writerows(filas)
        return Response(
            "﻿" + salida.getvalue(),  # el BOM hace que Excel lea bien los acentos
            media_type="text/csv; charset=utf-8",
            headers={**sin_cache, "Content-Disposition": 'attachment; filename="leads.csv"'},
        )
