"""Herramientas del CRM: derivar un cliente a ventas y descartar un lead.

`derivar_a_ejecutivo` es la que hace que una derivación EXISTA: una vez
mandada la respuesta, le pone a la conversación la etiqueta del traspaso (el
agente se calla), le deja al ejecutivo una nota privada con el resumen del
cliente y, si está configurado CHATWOOT_EJECUTIVO_ID, se la asigna. Y queda anotada en el CRM con su hora,
que es desde donde se mide cuánto tarda el ejecutivo en responder.
"""

from __future__ import annotations

import logging
import os
import threading

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from . import anotar_derivacion, anotar_descarte

registro = logging.getLogger(__name__)


# Las derivaciones se aplican en Chatwoot DESPUÉS de mandar la respuesta: si la
# etiqueta se pusiera antes, el agente vería «humano» y no mandaría el mensaje
# que le avisa al cliente que lo contacta el ejecutivo.
_pendientes: dict[str, tuple[str, str]] = {}
_candado = threading.Lock()


def aplicar_derivacion_pendiente(conversacion: str, canal) -> None:
    """Etiqueta, nota para el ejecutivo y asignación. Lo llama el webhook al terminar."""
    with _candado:
        pendiente = _pendientes.pop(str(conversacion), None)
    if not pendiente:
        return
    motivo, resumen = pendiente
    try:
        canal.pasar_a_persona(conversacion)
        canal._nota_privada(conversacion, f"🤝 Derivado a ventas: {motivo}\n{resumen}")
        asignar = (os.getenv("CHATWOOT_EJECUTIVO_ID") or "").strip()
        if asignar.isdigit():
            canal._api(
                "POST",
                f"conversations/{conversacion}/assignments",
                {"assignee_id": int(asignar)},
            )
    except Exception as e:  # noqa: BLE001
        registro.error("[%s] no se pudo derivar en Chatwoot: %s", conversacion, e)


def _conversacion(config: RunnableConfig | None) -> str:
    return str(((config or {}).get("configurable") or {}).get("thread_id") or "")


@tool
def derivar_a_ejecutivo(motivo: str, resumen: str, config: RunnableConfig = None) -> str:
    """Pasa el cliente al ejecutivo de ventas (Juan de Dios Rodríguez).

    Usala cuando la persona quiere reservar, firmar o pagar; pide condiciones
    especiales o más descuento que el máximo; pregunta por papeles, trámites o
    algo que no sabés; tiene un reclamo; o pide hablar con una persona.
    Después de usarla no sigas negociando: el ejecutivo continúa la charla.

    Args:
        motivo: Por qué se deriva, en pocas palabras (ej. "quiere reservar",
            "pide más descuento", "consulta por títulos", "pide hablar con una persona").
        resumen: Lo que el ejecutivo tiene que saber del cliente en una o dos
            líneas: nombre si lo sabés, forma de pago, presupuesto o cuota,
            proyecto y lote que le interesó, para qué lo busca (inversión o
            vivienda) y qué pidió.
    """
    conversacion = _conversacion(config)
    anotar_derivacion(conversacion, motivo, resumen)
    ejecutivo = (os.getenv("EJECUTIVO_VENTAS") or "Juan de Dios Rodríguez").strip()

    if conversacion:
        with _candado:
            _pendientes[conversacion] = (motivo[:200], resumen[:800])

    return (
        "DERIVADO A VENTAS. Decile a la persona, con calidez, que el ejecutivo de "
        f"ventas {ejecutivo} se comunicará con él/ella en breve para ayudarle con "
        "eso. No sigas negociando ni prometas nada más."
    )


@tool
def descartar_lead(motivo: str, config: RunnableConfig = None) -> str:
    """Marca que esta persona NO es un cliente potencial, para el reporte.

    Usala solo si queda claro: dice que no le interesa comprar, se equivocó
    de número, busca otra cosa (alquiler, empleo, vender su terreno), o es
    spam. NO la uses porque dude, diga que lo va a pensar o que está caro.
    Seguí atendiendo con amabilidad después de usarla.

    Args:
        motivo: En pocas palabras: "no le interesa", "número equivocado",
            "busca alquiler", "busca empleo", "spam", etc.
    """
    anotar_descarte(_conversacion(config), motivo)
    return "Anotado. Despedite con amabilidad y dejá la puerta abierta por si cambia de idea."


HERRAMIENTAS_DEL_CRM = [derivar_a_ejecutivo, descartar_lead]
