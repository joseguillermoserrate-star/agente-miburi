"""Herramientas: ver los horarios libres y agendar una visita al lote.

La agenda vive en el Google Calendar del dueño y los leads en una hoja de
Google. El agente no les habla directo: un Apps Script publicado como
aplicación web (el de la hoja «Leads y visitas») hace el trabajo y aplica las
reglas (visitas de 1 hora, hasta 2 a la misma hora, horario de atención).
Las reglas viven allá a propósito: así una conversación no puede saltearlas.

Variables de entorno:

    AGENDA_URL    la URL /exec de la aplicación web del Apps Script
    AGENDA_TOKEN  el mismo TOKEN que está escrito en el script

El teléfono del cliente NO lo pregunta el modelo: se toma de Chatwoot, del
contacto que está escribiendo. Así no hay forma de anotar un número mal
copiado (ni uno inventado).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

registro = logging.getLogger(__name__)

ESPERA = 30  # Apps Script con Calendar puede tardar en despertar


@tool
def consultar_disponibilidad(fecha: str, dias: int = 1) -> str:
    """Muestra los horarios libres para visitar un lote.

    Usala ANTES de proponer o confirmar una visita. Cada visita dura 1 hora.

    Args:
        fecha: Primer día a consultar, en formato AAAA-MM-DD (por ejemplo
            "2026-10-15"). Calculala con la fecha de hoy que tenés en las
            instrucciones si la persona dice "mañana", "el sábado", etc.
        dias: Cuántos días seguidos mirar desde esa fecha (1 a 7). Usá 3 si la
            persona no tiene un día fijo.
    """
    try:
        datos = _pedir({"accion": "disponibilidad", "fecha": fecha, "dias": dias})
    except Exception as e:  # noqa: BLE001
        return (
            f"No se pudo consultar la agenda ({type(e).__name__}). No confirmes "
            "horarios: decí que un asesor le escribe para coordinar la visita."
        )

    lineas = []
    for dia in datos.get("dias") or []:
        nombre = f"{dia.get('dia_semana', '')} {dia.get('fecha', '')}".strip()
        if not dia.get("atiende"):
            lineas.append(f"- {nombre}: no hay visitas ese día.")
        elif not dia.get("horarios_libres"):
            lineas.append(f"- {nombre}: sin horarios libres.")
        else:
            horas = ", ".join(h["hora"] for h in dia["horarios_libres"])
            lineas.append(f"- {nombre}: libre a las {horas}.")
    if not lineas:
        return "La agenda no devolvió horarios."
    return (
        "Horarios libres (cada visita dura 1 hora). Ofrecé 2 o 3, no todos:\n"
        + "\n".join(lineas)
    )


@tool
def agendar_visita(
    nombre: str,
    fecha: str,
    hora: str,
    proyecto: str = "",
    forma_de_pago: str = "",
    presupuesto: str = "",
    notas: str = "",
    config: RunnableConfig = None,
) -> str:
    """Agenda la visita al lote en el calendario y registra al interesado.

    Usala SOLO cuando la persona ya eligió día y hora y te dio su nombre.
    El teléfono se toma solo, no lo pidas.

    Args:
        nombre: Nombre de la persona, como te lo dijo.
        fecha: Día de la visita, AAAA-MM-DD.
        hora: Hora de inicio, HH:MM en 24 horas (por ejemplo "15:00").
        proyecto: Proyecto que quiere visitar, por ejemplo "Villa Real V".
        forma_de_pago: "contado" o "plazo", si ya lo dijo.
        presupuesto: Su presupuesto o cuota mensual, como lo dijo (ej. "USD 8.000
            al contado" o "USD 150 por mes").
        notas: Cualquier dato útil para el asesor, en una línea.
    """
    conversacion = str(((config or {}).get("configurable") or {}).get("thread_id") or "")
    telefono = _telefono_de(conversacion)

    try:
        datos = _pedir(
            {
                "accion": "agendar",
                "nombre": nombre,
                "telefono": telefono,
                "fecha": fecha,
                "hora": hora,
                "proyecto": proyecto,
                "forma_de_pago": forma_de_pago,
                "presupuesto": presupuesto,
                "notas": notas,
                "conversacion": conversacion,
            },
            exigir_ok=False,
        )
    except Exception as e:  # noqa: BLE001
        return (
            f"No se pudo agendar ({type(e).__name__}). No digas que quedó "
            "agendada: decí que un asesor le confirma el horario en breve."
        )

    if datos.get("ok"):
        return (
            f"VISITA AGENDADA: {datos.get('dia_semana', '')} {datos.get('fecha')} "
            f"de {datos.get('hora')} a {datos.get('hora_fin')}. Confirmáselo a la "
            "persona con día y hora, y decile que un asesor la acompañará."
        )

    sugerencias = datos.get("sugerencias") or []
    motivo = datos.get("error") or "no se pudo"
    texto = f"NO se agendó: {motivo}."
    if sugerencias:
        texto += " Horarios libres cercanos: " + "; ".join(sugerencias) + ". Ofrecé 2 o 3."
    return texto


HERRAMIENTAS_DE_AGENDA = [consultar_disponibilidad, agendar_visita]


# -- Por dentro -------------------------------------------------------------


def _pedir(cuerpo: dict, exigir_ok: bool = True) -> dict:
    url = (os.getenv("AGENDA_URL") or "").strip()
    token = (os.getenv("AGENDA_TOKEN") or "").strip()
    if not url or not token:
        raise RuntimeError("Faltan AGENDA_URL o AGENDA_TOKEN")

    pedido = urllib.request.Request(
        url,
        data=json.dumps({**cuerpo, "token": token}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    # Apps Script contesta un POST con una redirección (302) a
    # googleusercontent.com que hay que pedir con GET: urllib lo hace solo.
    with urllib.request.urlopen(pedido, timeout=ESPERA) as respuesta:
        datos = json.loads(respuesta.read().decode("utf-8") or "{}")

    if datos.get("error") == "no autorizado":
        raise RuntimeError("El AGENDA_TOKEN no coincide con el TOKEN del script")
    if exigir_ok and not datos.get("ok"):
        raise RuntimeError(f"La agenda contestó: {str(datos)[:200]}")
    return datos


def _telefono_de(conversacion: str) -> str:
    """El teléfono del contacto que escribe, sacado de Chatwoot."""
    url = (os.getenv("CHATWOOT_URL") or "").strip().rstrip("/")
    token = (os.getenv("CHATWOOT_TOKEN") or "").strip()
    cuenta = (os.getenv("CHATWOOT_CUENTA_ID") or "1").strip()
    if not (url and token and conversacion.isdigit()):
        return ""
    pedido = urllib.request.Request(
        f"{url}/api/v1/accounts/{urllib.parse.quote(cuenta)}/conversations/{conversacion}",
        headers={"api_access_token": token},
    )
    try:
        with urllib.request.urlopen(pedido, timeout=15) as respuesta:
            datos = json.loads(respuesta.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, ValueError) as e:
        registro.warning("[%s] no pude leer el teléfono en Chatwoot: %s", conversacion, e)
        return ""
    remitente = ((datos.get("meta") or {}).get("sender") or {})
    return str(remitente.get("phone_number") or "")
