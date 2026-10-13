"""El CRM del agente: registra cada lead y lo que pasa en su conversación.

Qué se anota y desde dónde:

    mensajes          el webhook de Chatwoot: del cliente, de la IA o de una
                      persona del equipo (con su nombre)
    calificación      `consultar_lotes`: forma de pago, presupuesto o cuota,
                      proyecto y para qué compra (inversión o vivienda)
    visitas           `agendar_visita` / `reagendar_visita`
    derivaciones      `derivar_a_ejecutivo` (la IA le pasa el cliente a ventas)
                      y los traspasos automáticos (fallas, tope de mensajes)
    descartes         `descartar_lead` (no le interesa, número equivocado, spam)
    asignaciones      Chatwoot, cuando a la conversación le asignan un agente
                      (evento «conversation_updated»)
    origen            el primer mensaje: anuncio de Meta (clic a WhatsApp) o
                      WhatsApp directo

Se prende solo en producción (usa POSTGRES_DSN). En la computadora, con
CRM_SQLITE=crm.sqlite. CRM=0 lo apaga.

Todo acá se traga sus errores: un problema del CRM se ve en los registros,
nunca en la conversación con el cliente.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque

from .almacen import Almacen

registro = logging.getLogger(__name__)

_almacen: Almacen | None = None
_iniciado = False
_candado = threading.Lock()


def almacen() -> Almacen | None:
    """El almacén del CRM, o None si está apagado."""
    global _almacen, _iniciado
    with _candado:
        if _iniciado:
            return _almacen
        _iniciado = True
        if (os.getenv("CRM") or "1").strip().lower() in {"0", "no", "false", "off"}:
            return None
        ruta = (os.getenv("CRM_SQLITE") or "").strip()
        dsn = (os.getenv("POSTGRES_DSN") or "").strip()
        modo = (os.getenv("MODO") or "test").strip().lower()
        if ruta:
            _almacen = Almacen(ruta_sqlite=ruta)
        elif dsn and modo == "produccion":
            _almacen = Almacen(dsn=dsn)
        return _almacen


def reiniciar(nuevo: Almacen | None = None) -> None:
    """Para los tests: cambia el almacén (o lo vuelve a leer del entorno)."""
    global _almacen, _iniciado
    with _candado:
        _almacen = nuevo
        _iniciado = nuevo is not None


def _seguro(funcion):
    """Que un error del CRM nunca le llegue a la conversación."""

    def envoltura(*args, **kwargs):
        try:
            base = almacen()
            if base is None:
                return None
            return funcion(base, *args, **kwargs)
        except Exception as e:  # noqa: BLE001
            registro.warning("CRM: no se pudo anotar (%s): %s", funcion.__name__, e)
            return None

    envoltura.__name__ = funcion.__name__
    envoltura.__doc__ = funcion.__doc__
    return envoltura


# -- Lo que manda la IA ------------------------------------------------------
#
# El agente le escribe a Chatwoot con el token de un usuario, así que en
# Chatwoot sus mensajes figuran como de esa persona. Para separar «lo escribió
# la IA» de «lo escribió alguien del equipo» se anota acá cada texto que manda
# el agente ANTES de mandarlo (el aviso de Chatwoot puede llegar antes de que
# termine el envío).

_de_la_ia: deque[tuple[str, str]] = deque(maxlen=2000)


def lo_va_a_mandar_la_ia(conversacion: str, texto: str) -> None:
    _de_la_ia.append((str(conversacion), (texto or "").strip()))


def lo_mando_la_ia(conversacion: str, texto: str) -> bool:
    return (str(conversacion), (texto or "").strip()) in _de_la_ia


# -- Anotaciones -------------------------------------------------------------


@_seguro
def anotar_mensaje(base: Almacen, evento: dict) -> None:
    """Un `message_created` de Chatwoot: del cliente, de la IA o de una persona."""
    if not isinstance(evento, dict) or evento.get("event") != "message_created":
        return
    if evento.get("private"):
        return
    conversacion = evento.get("conversation") or {}
    id_conversacion = conversacion.get("id") if isinstance(conversacion, dict) else None
    if isinstance(id_conversacion, bool) or not str(id_conversacion or "").isdigit():
        return
    conv = str(id_conversacion)

    tipo = evento.get("message_type")
    tipo = {0: "incoming", 1: "outgoing"}.get(tipo, tipo) if isinstance(tipo, int) else str(tipo or "")
    texto = evento.get("content") if isinstance(evento.get("content"), str) else ""
    ahora = time.time()
    remitente = evento.get("sender") if isinstance(evento.get("sender"), dict) else {}

    if tipo == "incoming":
        origen, campana = origen_del(evento)
        base.lead(
            conv,
            solo_si_falta=("nombre", "telefono"),
            telefono=str(remitente.get("phone_number") or ""),
            nombre=str(remitente.get("name") or ""),
            origen=origen,
            campana=campana,
            canal=str(conversacion.get("channel") or "").replace("Channel::", ""),
            primer_contacto=ahora,
            ultimo_mensaje=ahora,
        )
        base.evento(conv, "mensaje", "cliente", {"largo": len(texto)}, ahora)
    elif tipo == "outgoing":
        if lo_mando_la_ia(conv, texto):
            base.evento(conv, "mensaje", "ia", {"largo": len(texto)}, ahora)
        else:
            nombre = str(remitente.get("name") or remitente.get("available_name") or "Equipo")
            base.evento(conv, "mensaje", "humano", {"agente": nombre, "largo": len(texto)}, ahora)
        base.lead(conv, ultimo_mensaje=ahora)


@_seguro
def anotar_asignacion(base: Almacen, evento: dict) -> None:
    """`conversation_updated` de Chatwoot con un cambio de agente asignado."""
    if not isinstance(evento, dict):
        return
    id_conversacion = evento.get("id")
    if isinstance(id_conversacion, bool) or not str(id_conversacion or "").isdigit():
        return
    cambios = evento.get("changed_attributes") or []
    cambio_de_asignado = any(
        isinstance(c, dict) and "assignee_id" in c for c in cambios
    ) if isinstance(cambios, list) else False
    if not cambio_de_asignado:
        return
    meta = evento.get("meta") if isinstance(evento.get("meta"), dict) else {}
    asignado = meta.get("assignee") if isinstance(meta.get("assignee"), dict) else {}
    nombre = str(asignado.get("name") or asignado.get("available_name") or "")
    if not nombre:
        return  # le sacaron el asignado
    ahora = time.time()
    base.evento(str(id_conversacion), "asignacion", "chatwoot", {"agente": nombre}, ahora)
    base.lead(str(id_conversacion), asignado_a=nombre, asignado_en=ahora)


@_seguro
def anotar_consulta_de_precios(
    base: Almacen,
    conversacion: str,
    forma_de_pago: str = "",
    presupuesto_usd: float = 0,
    cuota_usd: float = 0,
    proyecto: str = "",
    motivo_compra: str = "",
    sin_opciones: bool = False,
) -> None:
    if not conversacion:
        return
    motivo = _motivo_normal(motivo_compra)
    base.lead(
        conversacion,
        forma_pago=forma_de_pago or None,
        presupuesto_usd=float(presupuesto_usd) if presupuesto_usd else None,
        cuota_usd=float(cuota_usd) if cuota_usd else None,
        proyecto=proyecto or None,
        motivo_compra=motivo or None,
        fuera_de_presupuesto=1 if sin_opciones else (0 if (presupuesto_usd or cuota_usd) else None),
    )
    base.evento(
        conversacion,
        "consulta_precios",
        "ia",
        {
            "forma_pago": forma_de_pago,
            "presupuesto_usd": presupuesto_usd,
            "cuota_usd": cuota_usd,
            "proyecto": proyecto,
            "motivo_compra": motivo,
            "sin_opciones": sin_opciones,
        },
    )


@_seguro
def anotar_visita(
    base: Almacen,
    conversacion: str,
    fecha: str,
    hora: str,
    nombre: str = "",
    telefono: str = "",
    proyecto: str = "",
    forma_de_pago: str = "",
    reagendada: bool = False,
) -> None:
    if not conversacion:
        return
    ahora = time.time()
    base.lead(
        conversacion,
        nombre=nombre or None,
        telefono=telefono or None,
        proyecto=proyecto or None,
        forma_pago=_forma_normal(forma_de_pago) or None,
        visita=f"{fecha} {hora}".strip(),
        visita_en=ahora,
    )
    base.evento(
        conversacion,
        "visita_reagendada" if reagendada else "visita_agendada",
        "ia",
        {"fecha": fecha, "hora": hora, "proyecto": proyecto},
        ahora,
    )


@_seguro
def anotar_derivacion(base: Almacen, conversacion: str, motivo: str, resumen: str = "") -> None:
    """La IA le pasó el cliente al ejecutivo de ventas."""
    if not conversacion:
        return
    ahora = time.time()
    base.lead(conversacion, derivado_en=ahora, derivado_motivo=motivo[:200])
    base.evento(conversacion, "derivacion", "ia", {"motivo": motivo[:200], "resumen": resumen[:500]}, ahora)


@_seguro
def anotar_traspaso(base: Almacen, conversacion: str, motivo: str) -> None:
    """El sistema calló al agente (falla, tope, conversación resuelta…): no es una venta."""
    if not conversacion:
        return
    base.evento(conversacion, "traspaso", "sistema", {"motivo": motivo[:300]})


@_seguro
def anotar_descarte(base: Almacen, conversacion: str, motivo: str) -> None:
    if not conversacion:
        return
    ahora = time.time()
    base.lead(conversacion, descartado_en=ahora, descarte_motivo=motivo[:200])
    base.evento(conversacion, "descarte", "ia", {"motivo": motivo[:200]}, ahora)


@_seguro
def anotar_consumo_ia(base: Almacen, conversacion: str, respuesta) -> None:
    """Los tokens que usó la IA para responder (para el costo por lead)."""
    if not conversacion:
        return
    base.evento(
        conversacion,
        "consumo_ia",
        "ia",
        {
            "modelo": getattr(respuesta, "modelo", ""),
            "entrada": int(getattr(respuesta, "tokens_entrada", 0) or 0),
            "salida": int(getattr(respuesta, "tokens_salida", 0) or 0),
            "cache": int(getattr(respuesta, "tokens_cache_leidos", 0) or 0),
        },
    )


@_seguro
def borrar(base: Almacen, conversacion: str) -> None:
    base.borrar_conversacion(conversacion)


# -- De dónde vino el lead ---------------------------------------------------

FRASES_DE_ANUNCIO = (
    "anuncio",
    "publicidad",
    "publicación",
    "publicacion",
    "vi en facebook",
    "vi en instagram",
    "vi su post",
    "quiero más información sobre esto",
    "quiero mas informacion sobre esto",
)


def origen_del(evento: dict) -> tuple[str, str]:
    """(origen, campaña) del primer mensaje.

    1. Si Chatwoot trae el «referral» de Meta (clic a WhatsApp desde un
       anuncio), es un anuncio y la campaña es su título.
    2. Si no, por el texto: los anuncios de Meta suelen abrir el chat con un
       mensaje armado («Quiero más información sobre esto»). Se pueden sumar
       frases en CRM_FRASES_ANUNCIO, separadas por coma.
    3. Si no, WhatsApp directo.
    """
    referral = _buscar_referral(evento)
    if referral:
        campana = str(
            referral.get("headline")
            or referral.get("source_id")
            or referral.get("source_url")
            or ""
        )[:150]
        return "Anuncio Meta", campana

    texto = str(evento.get("content") or "").lower()
    extra = [f.strip().lower() for f in (os.getenv("CRM_FRASES_ANUNCIO") or "").split(",") if f.strip()]
    if texto and any(f in texto for f in (*FRASES_DE_ANUNCIO, *extra)):
        return "Anuncio Meta", ""
    return "WhatsApp directo", ""


def _buscar_referral(datos, profundidad: int = 0) -> dict | None:
    if profundidad > 4:
        return None
    if isinstance(datos, dict):
        for clave, valor in datos.items():
            if "referral" in str(clave).lower() and isinstance(valor, dict) and valor:
                return valor
        for clave in ("content_attributes", "additional_attributes", "conversation", "external_source"):
            encontrado = _buscar_referral(datos.get(clave), profundidad + 1)
            if encontrado:
                return encontrado
    return None


def _motivo_normal(texto: str) -> str:
    t = (texto or "").lower()
    if any(p in t for p in ("invers", "resguard", "ahorro", "capital")):
        return "inversion"
    if any(p in t for p in ("vivi", "casa", "constru", "alquiler", "vivir")):
        return "vivienda"
    return ""


def _forma_normal(texto: str) -> str:
    t = (texto or "").lower()
    if "contado" in t:
        return "contado"
    if "plaz" in t or "cuota" in t or "credito" in t or "crédito" in t:
        return "plazo"
    return ""
