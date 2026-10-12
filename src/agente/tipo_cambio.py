"""Herramienta: pasar a bolivianos un precio en dólares, al tipo de cambio del día.

Los precios se venden en dólares. Solo si la persona pregunta cuánto es en
bolivianos, el agente usa esta herramienta, y la cuenta se hace acá, no en el
modelo: así no hay errores de multiplicación.

De dónde sale el tipo de cambio, en este orden:

1. La pestaña «Tipo de cambio» de la hoja de precios (celda B1), que el dueño
   actualiza con el valor del Banco Central. Manda sobre todo lo demás.
2. La página del Banco Central de Bolivia
   (https://www.bcb.gob.bo/?q=cotizaciones_tc, fila «ESTADOS UNIDOS · DÓLAR ·
   USD»), si la hoja no tiene el dato.
3. TIPO_DE_CAMBIO_BOB, como último respaldo.

Variables de entorno (opcionales):

    TIPO_DE_CAMBIO_URL   de dónde leer la tabla (por defecto, la del BCB)
    TIPO_DE_CAMBIO_BOB   valor de respaldo (ej. 11.73) por si la página del
                         BCB no responde. Si no está y el BCB no responde, el
                         agente no convierte y dice que el asesor lo confirma.

Para probarlo en el servidor:  python probar_tipo_de_cambio.py
"""

from __future__ import annotations

import html
import logging
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from langchain_core.tools import tool

registro = logging.getLogger(__name__)

URL_BCB = "https://www.bcb.gob.bo/?q=cotizaciones_tc"
ESPERA = 15
VIGENCIA = 3 * 3600  # se vuelve a leer cada 3 horas (el BCB publica una vez al día)

_cache: dict = {"momento": 0.0, "valor": None, "fecha": ""}
_candado = threading.Lock()


@tool
def convertir_a_bolivianos(montos_usd: list[float]) -> str:
    """Convierte precios en dólares a bolivianos con el tipo de cambio oficial
    del Banco Central de Bolivia de HOY.

    Usala SOLO si la persona pregunta cuánto es en bolivianos. Pasale los
    montos en dólares que ya le diste (los de `consultar_lotes`). Recordale
    siempre que los precios son en dólares y que en bolivianos es una
    referencia al tipo de cambio del día.

    Args:
        montos_usd: Los montos en dólares a convertir, por ejemplo [16500, 11550].
    """
    tasa, origen = tipo_de_cambio()
    if not tasa:
        return (
            "No se pudo leer el tipo de cambio del Banco Central en este momento. "
            "No conviertas: decí que los precios son en dólares y que el ejecutivo "
            "de ventas le confirma el monto en bolivianos."
        )
    lineas = [f"Tipo de cambio oficial BCB ({origen}): Bs {_bs(tasa, 2)} por dólar."]
    for monto in montos_usd or []:
        try:
            usd = float(monto)
        except (TypeError, ValueError):
            continue
        lineas.append(f"- USD {_bs(usd, 0)} = Bs {_bs(usd * tasa, 0)}")
    lineas.append(
        "CÓMO DECIRLO: aclará primero que los precios son en DÓLARES, y después "
        "el equivalente en bolivianos como referencia al tipo de cambio oficial "
        "del BCB de hoy (decí el tipo de cambio)."
    )
    return "\n".join(lineas)


def tipo_de_cambio() -> tuple[float | None, str]:
    """(Bs por dólar, de dónde salió)."""
    from .precios import tipo_de_cambio_de_la_hoja

    de_la_hoja = tipo_de_cambio_de_la_hoja()
    if de_la_hoja:
        return de_la_hoja, "vigente hoy " + _hoy_bolivia()
    return _del_bcb_o_respaldo()


def _del_bcb_o_respaldo() -> tuple[float | None, str]:
    """El valor del BCB (cacheado unas horas) o el respaldo del .env."""
    ahora = time.time()
    with _candado:
        if _cache["valor"] and ahora - _cache["momento"] < VIGENCIA:
            return _cache["valor"], _cache["fecha"]
    try:
        valor = leer_bcb()
    except Exception as e:  # noqa: BLE001
        registro.warning("no pude leer el tipo de cambio del BCB: %s", e)
        valor = None
    if valor:
        fecha = "hoy " + _hoy_bolivia()
        with _candado:
            _cache.update(momento=ahora, valor=valor, fecha=fecha)
        return valor, fecha
    # El BCB no respondió: lo último que se leyó, o el respaldo del .env.
    with _candado:
        if _cache["valor"]:
            return _cache["valor"], _cache["fecha"]
    respaldo = _numero(os.getenv("TIPO_DE_CAMBIO_BOB") or "")
    if respaldo and 1 < respaldo < 100:
        return respaldo, "valor de referencia configurado"
    return None, ""


def leer_bcb() -> float | None:
    url = (os.getenv("TIPO_DE_CAMBIO_URL") or URL_BCB).strip()
    pedido = urllib.request.Request(
        url,
        headers={
            # Algunas páginas públicas rechazan pedidos sin navegador.
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept-Language": "es-BO,es;q=0.9",
        },
    )
    with urllib.request.urlopen(pedido, timeout=ESPERA) as respuesta:
        crudo = respuesta.read().decode("utf-8", errors="replace")
    return extraer_tco(crudo)


def extraer_tco(pagina: str) -> float | None:
    """Busca la fila «ESTADOS UNIDOS … USD 11.73» en el HTML del BCB."""
    texto = re.sub(r"<[^>]+>", " ", pagina)
    texto = html.unescape(texto)
    texto = re.sub(r"\s+", " ", texto)
    encontrado = re.search(
        r"ESTADOS\s+UNIDOS\s+D[ÓO]LAR\s+USD\s+([0-9]+(?:[.,][0-9]+)?)", texto, re.I
    )
    if not encontrado:
        return None
    valor = _numero(encontrado.group(1))
    # Control de cordura: si sale algo absurdo, mejor no convertir.
    return valor if valor and 1 < valor < 100 else None


# -- Por dentro -------------------------------------------------------------


def _numero(texto: str) -> float | None:
    texto = str(texto).strip().replace(",", ".")
    try:
        return float(texto)
    except ValueError:
        return None


def _bs(valor: float, decimales: int) -> str:
    """Formato boliviano: 135.498 / 11,73."""
    texto = f"{valor:,.{decimales}f}"
    return texto.replace(",", "§").replace(".", ",").replace("§", ".")


def _hoy_bolivia() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%d/%m/%Y")
