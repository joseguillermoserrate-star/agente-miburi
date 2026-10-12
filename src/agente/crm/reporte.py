"""Los números del panel: se calculan de las tablas del CRM, para un rango de fechas.

Definiciones (las mismas que muestra el panel en sus ayudas):

    Lead / contactado   una conversación cuyo primer mensaje cayó en el rango.
    Calificado          dio forma de pago Y presupuesto (o cuota), o agendó visita.
    Visita agendada     la IA agendó una visita (queda para el ejecutivo).
    Derivado a ventas   la IA le pasó el cliente al ejecutivo (quiere reservar,
                        pagar, más descuento, papeles, reclamo…).
    Lead «malo»         descartado (no le interesa, número equivocado, spam…),
                        fuera de presupuesto (nada le alcanzó y no avanzó), o
                        sin respuesta (más de 24 h callado sin calificar).
    En conversación     ninguna de las anteriores todavía.

    Tiempo de conversación   del primer al último mensaje de cada lead.
    Respuesta de la IA       desde el mensaje del cliente hasta la respuesta.
    Respuesta del ejecutivo  desde la derivación, la visita agendada o la
                             asignación hasta el primer mensaje de una persona
                             del equipo en esa conversación.
"""

from __future__ import annotations

import os
import statistics
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone, tzinfo

UN_DIA = 86400
SIN_RESPUESTA_TRAS = UN_DIA


def zona() -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo((os.getenv("ZONA_HORARIA") or "America/La_Paz").strip())
    except Exception:  # noqa: BLE001
        return timezone(timedelta(hours=-4))


def rango(desde: str, hasta: str) -> tuple[float, float, date, date]:
    """Fechas AAAA-MM-DD (inclusive, hora de Bolivia) a segundos UTC [inicio, fin)."""
    tz = zona()
    hoy = datetime.now(tz).date()
    try:
        d = date.fromisoformat(desde) if desde else hoy - timedelta(days=29)
    except ValueError:
        d = hoy - timedelta(days=29)
    try:
        h = date.fromisoformat(hasta) if hasta else hoy
    except ValueError:
        h = hoy
    if h < d:
        d, h = h, d
    inicio = datetime(d.year, d.month, d.day, tzinfo=tz).timestamp()
    fin = datetime(h.year, h.month, h.day, tzinfo=tz).timestamp() + UN_DIA
    return inicio, fin, d, h


def dia(momento: float) -> str:
    return datetime.fromtimestamp(momento, zona()).date().isoformat()


def armar(leads: list[dict], eventos: list[dict], desde: str = "", hasta: str = "", ahora: float | None = None) -> dict:
    """Todo el reporte, listo para el panel (JSON)."""
    ahora = ahora or time.time()
    inicio, fin, d, h = rango(desde, hasta)

    def en_rango(t) -> bool:
        return bool(t) and inicio <= float(t) < fin

    por_conv: dict[str, list[dict]] = defaultdict(list)
    for e in eventos:
        por_conv[e["conversacion"]].append(e)

    fichas = _fichas(leads, por_conv, inicio, fin, ahora)

    total = len(fichas)
    calificados = sum(1 for f in fichas if _calificado(f))
    con_visita = sum(1 for f in fichas if f.get("visita"))
    derivados = sum(1 for f in fichas if f.get("derivado_en"))
    visita_o_derivado = sum(1 for f in fichas if f.get("visita") or f.get("derivado_en"))
    malos = Counter(f["calidad"] for f in fichas if f["estado"] == "malo")

    # Los eventos del rango (para series y tiempos), sean de leads nuevos o no.
    del_rango = [e for e in eventos if en_rango(e["momento"])]

    dias = [(d + timedelta(days=i)).isoformat() for i in range((h - d).days + 1)]
    leads_por_dia = Counter(dia(f["primer_contacto"]) for f in fichas)
    mensajes_por_dia: dict[str, Counter] = defaultdict(Counter)
    for e in del_rango:
        if e["tipo"] == "mensaje":
            mensajes_por_dia[dia(e["momento"])][e["actor"]] += 1
    visitas_por_dia = Counter(
        dia(e["momento"]) for e in del_rango if e["tipo"] == "visita_agendada"
    )
    derivaciones_por_dia = Counter(
        dia(e["momento"]) for e in del_rango if e["tipo"] == "derivacion"
    )

    tiempos = _tiempos(por_conv, inicio, fin, ahora)
    tiempos.update(_calidad_de_atencion(fichas, por_conv, inicio, fin))

    # El período anterior, del mismo largo: ¿vamos mejor o peor?
    largo = fin - inicio
    anteriores = _fichas(leads, por_conv, inicio - largo, inicio, ahora)
    anterior = _generales(anteriores)

    return {
        "comparativo": {
            "desde": (d - timedelta(days=(h - d).days + 1)).isoformat(),
            "hasta": (d - timedelta(days=1)).isoformat(),
            **anterior,
        },
        "acciones": _acciones(leads, por_conv, ahora),
        "segmentos": _segmentos(fichas),
        "presupuestos": _presupuestos(fichas),
        "horarios": _horarios(del_rango),
        "rango": {"desde": d.isoformat(), "hasta": h.isoformat()},
        "generado": datetime.fromtimestamp(ahora, zona()).strftime("%d/%m/%Y %H:%M"),
        "general": {
            "leads": total,
            "calificados": calificados,
            "visitas": con_visita,
            "derivados": derivados,
            "visita_o_derivado": visita_o_derivado,
            "malos": sum(malos.values()),
            "en_conversacion": sum(1 for f in fichas if f["estado"] == "en conversación"),
            "conversion_visita": _pct(con_visita, total),
            "conversion_derivacion": _pct(derivados, total),
            "conversion_calificado": _pct(calificados, total),
            "conversion_avance": _pct(visita_o_derivado, total),
            "visitas_agendadas_en_rango": sum(visitas_por_dia.values()),
            "reagendadas_en_rango": sum(1 for e in del_rango if e["tipo"] == "visita_reagendada"),
            "traspasos_sistema": sum(1 for e in del_rango if e["tipo"] == "traspaso"),
        },
        "embudo": [
            {"etapa": "Contactados", "valor": total},
            {"etapa": "Calificados", "valor": calificados},
            {"etapa": "Visita o derivación", "valor": visita_o_derivado},
        ],
        "series": {
            "dias": dias,
            "leads": [leads_por_dia.get(x, 0) for x in dias],
            "visitas": [visitas_por_dia.get(x, 0) for x in dias],
            "derivaciones": [derivaciones_por_dia.get(x, 0) for x in dias],
            "mensajes_cliente": [mensajes_por_dia[x].get("cliente", 0) for x in dias],
            "mensajes_ia": [mensajes_por_dia[x].get("ia", 0) for x in dias],
            "mensajes_humano": [mensajes_por_dia[x].get("humano", 0) for x in dias],
        },
        "calidad": {
            "por_proyecto": _por(fichas, "proyecto", "Sin definir"),
            "por_origen": _por(fichas, "origen", "Sin dato"),
            "por_campana": _por([f for f in fichas if f.get("campana")], "campana", ""),
            "por_motivo": _por(
                fichas,
                "motivo_compra",
                "Sin dato",
                nombres={"inversion": "Inversión", "vivienda": "Vivienda"},
            ),
            "por_forma_pago": _por(
                fichas, "forma_pago", "Sin dato", nombres={"contado": "Contado", "plazo": "A plazo"}
            ),
            "calificados": calificados,
            "malos": [{"nombre": k, "valor": v} for k, v in malos.most_common()],
            "descartes": _contar(f.get("descarte_motivo") for f in fichas if f.get("descarte_motivo")),
            "derivaciones": _contar(f.get("derivado_motivo") for f in fichas if f.get("derivado_motivo")),
            "visitas_por_proyecto": _por([f for f in fichas if f.get("visita")], "proyecto", "Sin definir"),
        },
        "productividad": tiempos,
        "leads": [_fila(f) for f in sorted(fichas, key=lambda f: -(f.get("ultimo_mensaje") or f["primer_contacto"]))],
    }


# -- Por dentro -------------------------------------------------------------


def _fichas(leads: list[dict], por_conv: dict, inicio: float, fin: float, ahora: float) -> list[dict]:
    """Los leads cuyo primer mensaje cayó en [inicio, fin), con su estado.

    Si no se registró el primer mensaje (por ejemplo, la persona del equipo
    escribió primero), vale el primer evento de esa conversación.
    """
    fichas = []
    for lead in leads:
        conv = lead["conversacion"]
        primero = lead.get("primer_contacto") or min(
            (e["momento"] for e in por_conv.get(conv, [])), default=None
        )
        if not primero or not (inicio <= float(primero) < fin):
            continue
        ficha = dict(lead)
        ficha["primer_contacto"] = float(primero)
        ficha["estado"], ficha["calidad"] = _estado(ficha, ahora)
        ficha["mensajes"] = Counter(
            e["actor"] for e in por_conv.get(conv, []) if e["tipo"] == "mensaje"
        )
        fichas.append(ficha)
    return fichas


def _generales(fichas: list[dict]) -> dict:
    total = len(fichas)
    calificados = sum(1 for f in fichas if _calificado(f))
    visitas = sum(1 for f in fichas if f.get("visita"))
    derivados = sum(1 for f in fichas if f.get("derivado_en"))
    avanzan = sum(1 for f in fichas if f.get("visita") or f.get("derivado_en"))
    return {
        "leads": total,
        "calificados": calificados,
        "visitas": visitas,
        "derivados": derivados,
        "visita_o_derivado": avanzan,
        "malos": sum(1 for f in fichas if f["estado"] == "malo"),
        "conversion_calificado": _pct(calificados, total),
        "conversion_visita": _pct(visitas, total),
        "conversion_derivacion": _pct(derivados, total),
        "conversion_avance": _pct(avanzan, total),
    }


def _segmentos(fichas: list[dict]) -> dict:
    """Conversión por origen, campaña, proyecto, motivo y forma de pago.

    Para decidir dónde poner la plata y el esfuerzo: no alcanza con saber
    cuántos leads trae un anuncio, hay que ver cuántos llegan a visita.
    """
    dimensiones = {
        "origen": ("origen", "Sin dato", None),
        "campana": ("campana", "Sin campaña", None),
        "proyecto": ("proyecto", "Sin definir", None),
        "motivo": ("motivo_compra", "Sin dato", {"inversion": "Inversión", "vivienda": "Vivienda"}),
        "forma_pago": ("forma_pago", "Sin dato", {"contado": "Contado", "plazo": "A plazo"}),
    }
    salida = {}
    for clave, (campo, vacio, nombres) in dimensiones.items():
        grupos: dict[str, list[dict]] = defaultdict(list)
        for f in fichas:
            valor = str(f.get(campo) or "").strip()
            if valor:
                valor = (nombres or {}).get(valor.lower(), valor)
                if campo == "proyecto":
                    valor = _titulo(valor)
            grupos[valor or vacio].append(f)
        filas = []
        for nombre, grupo in grupos.items():
            g = _generales(grupo)
            filas.append(
                {
                    "nombre": nombre,
                    "leads": g["leads"],
                    "calificados": g["conversion_calificado"],
                    "visita": g["conversion_visita"],
                    "derivacion": g["conversion_derivacion"],
                    "avance": g["conversion_avance"],
                    "malos": _pct(g["malos"], g["leads"]),
                }
            )
        salida[clave] = sorted(filas, key=lambda x: -x["leads"])
    return salida


TRAMOS_CONTADO = [(0, 8000), (8000, 10000), (10000, 12000), (12000, 15000), (15000, None)]
TRAMOS_CUOTA = [(0, 100), (100, 150), (150, 200), (200, 300), (300, None)]


def _tramos(valores: list[float], tramos) -> list[dict]:
    salida = []
    for desde, hasta in tramos:
        n = sum(1 for v in valores if v >= desde and (hasta is None or v < hasta))
        if hasta is None:
            nombre = f"USD {desde:,.0f} o más"
        elif desde == 0:
            nombre = f"Menos de USD {hasta:,.0f}"
        else:
            nombre = f"USD {desde:,.0f} a {hasta:,.0f}"
        salida.append({"nombre": nombre.replace(",", "."), "valor": n})
    return salida


def _presupuestos(fichas: list[dict]) -> dict:
    contado = [float(f["presupuesto_usd"]) for f in fichas if f.get("presupuesto_usd")]
    cuotas = [float(f["cuota_usd"]) for f in fichas if f.get("cuota_usd")]
    con_visita_contado = [
        float(f["presupuesto_usd"])
        for f in fichas
        if f.get("presupuesto_usd") and (f.get("visita") or f.get("derivado_en"))
    ]
    return {
        "contado": _tramos(contado, TRAMOS_CONTADO),
        "cuota": _tramos(cuotas, TRAMOS_CUOTA),
        "contado_mediana": round(statistics.median(contado)) if contado else None,
        "cuota_mediana": round(statistics.median(cuotas)) if cuotas else None,
        "contado_casos": len(contado),
        "cuota_casos": len(cuotas),
        "potencial_contado": round(sum(con_visita_contado)),
        "potencial_contado_casos": len(con_visita_contado),
        "fuera_de_presupuesto": sum(1 for f in fichas if f.get("fuera_de_presupuesto")),
    }


def _horarios(eventos_del_rango: list[dict]) -> list[list[int]]:
    """Mensajes de clientes por día de la semana (lunes=0) y hora: cuándo escriben."""
    grilla = [[0] * 24 for _ in range(7)]
    tz = zona()
    for e in eventos_del_rango:
        if e["tipo"] == "mensaje" and e["actor"] == "cliente":
            momento = datetime.fromtimestamp(e["momento"], tz)
            grilla[momento.weekday()][momento.hour] += 1
    return grilla


def _primer_humano_despues(evs: list[dict], momento: float) -> dict | None:
    return next(
        (
            e
            for e in evs
            if e["tipo"] == "mensaje" and e["actor"] == "humano" and e["momento"] >= momento
        ),
        None,
    )


def _acciones(leads: list[dict], por_conv: dict, ahora: float) -> dict:
    """Lo que el equipo tiene que hacer HOY, sin importar el rango elegido.

    - Visitas por confirmar: agendadas (próximas o de hoy) sin que nadie del
      equipo le haya escrito al cliente después.
    - Derivaciones sin respuesta: la IA pasó el cliente a ventas y nadie le
      escribió todavía.
    - Calificados para recontactar: dieron forma de pago y presupuesto, no
      agendaron ni se derivaron, y llevan más de 24 h sin escribir.
    """
    tz = zona()
    hoy = datetime.now(tz).date()
    limite = ahora - 60 * UN_DIA  # más viejo que esto ya no es «para hoy»
    visitas, derivaciones, recontactar = [], [], []
    for lead in leads:
        conv = lead["conversacion"]
        evs = sorted(por_conv.get(conv, []), key=lambda e: e["momento"])
        base = {
            "conversacion": conv,
            "nombre": lead.get("nombre") or "Sin nombre",
            "telefono": lead.get("telefono") or "",
            "proyecto": _titulo(lead["proyecto"]) if lead.get("proyecto") else "",
        }
        if lead.get("descartado_en"):
            continue

        if lead.get("visita") and lead.get("visita_en"):
            try:
                cuando = datetime.strptime(lead["visita"][:16], "%Y-%m-%d %H:%M").date()
            except ValueError:
                cuando = None
            if cuando and cuando >= hoy and not _primer_humano_despues(evs, float(lead["visita_en"])):
                visitas.append({**base, "visita": lead["visita"], "dias": (cuando - hoy).days})

        if lead.get("derivado_en") and float(lead["derivado_en"]) >= limite:
            if not _primer_humano_despues(evs, float(lead["derivado_en"])):
                derivaciones.append(
                    {
                        **base,
                        "motivo": lead.get("derivado_motivo") or "",
                        "espera": round(ahora - float(lead["derivado_en"])),
                    }
                )

        ultimo = float(lead.get("ultimo_mensaje") or lead.get("primer_contacto") or 0)
        if (
            _calificado(lead)
            and not lead.get("visita")
            and not lead.get("derivado_en")
            and limite <= ultimo <= ahora - UN_DIA
        ):
            monto = (
                f"USD {float(lead['cuota_usd']):,.0f}/mes" if lead.get("forma_pago") == "plazo" and lead.get("cuota_usd")
                else f"USD {float(lead.get('presupuesto_usd') or lead.get('cuota_usd') or 0):,.0f}"
            ).replace(",", ".")
            recontactar.append(
                {**base, "monto": monto, "forma_pago": lead.get("forma_pago") or "", "inactivo": round(ahora - ultimo)}
            )

    visitas.sort(key=lambda x: x["visita"])
    derivaciones.sort(key=lambda x: -x["espera"])
    recontactar.sort(key=lambda x: x["inactivo"])
    return {"visitas": visitas, "derivaciones": derivaciones, "recontactar": recontactar}


def _calidad_de_atencion(fichas: list[dict], por_conv: dict, inicio: float, fin: float) -> dict:
    """Automatización y cumplimiento de tiempos (SLA) del equipo."""
    solo_ia = sum(1 for f in fichas if f["mensajes"].get("ia") and not f["mensajes"].get("humano"))
    con_respuesta = sum(1 for f in fichas if f["mensajes"].get("ia") or f["mensajes"].get("humano"))
    a_tiempo = total = 0
    visitas = confirmadas = 0
    for evs in por_conv.values():
        evs = sorted(evs, key=lambda e: e["momento"])
        for e in evs:
            if not (inicio <= e["momento"] < fin):
                continue
            if e["tipo"] == "derivacion":
                total += 1
                r = _primer_humano_despues(evs, e["momento"])
                if r and r["momento"] - e["momento"] <= 3600:
                    a_tiempo += 1
            elif e["tipo"] == "visita_agendada":
                visitas += 1
                if _primer_humano_despues(evs, e["momento"]):
                    confirmadas += 1
    return {
        "automatizacion": _pct(solo_ia, con_respuesta),
        "solo_ia": solo_ia,
        "sla_derivacion_1h": _pct(a_tiempo, total),
        "derivaciones": total,
        "visitas_confirmadas": _pct(confirmadas, visitas),
        "visitas": visitas,
    }


def _calificado(f: dict) -> bool:
    con_monto = bool(f.get("presupuesto_usd") or f.get("cuota_usd"))
    return bool(f.get("visita")) or bool(f.get("forma_pago") and con_monto)


def _estado(f: dict, ahora: float) -> tuple[str, str]:
    """(estado, calidad) del lead."""
    if f.get("descartado_en"):
        return "malo", "Descartado"
    if f.get("visita"):
        return "visita agendada", "Bueno"
    if f.get("derivado_en"):
        return "derivado a ventas", "Bueno"
    if f.get("fuera_de_presupuesto") and not _calificado_sin_presupuesto(f):
        if ahora - float(f.get("ultimo_mensaje") or f.get("primer_contacto") or ahora) > SIN_RESPUESTA_TRAS:
            return "malo", "Fuera de presupuesto"
    if _calificado(f):
        return "calificado", "Bueno"
    ultimo = float(f.get("ultimo_mensaje") or f.get("primer_contacto") or ahora)
    if ahora - ultimo > SIN_RESPUESTA_TRAS:
        return "malo", "Sin respuesta"
    return "en conversación", "Pendiente"


def _calificado_sin_presupuesto(f: dict) -> bool:
    return bool(f.get("visita") or f.get("derivado_en"))


def _tiempos(por_conv: dict, inicio: float, fin: float, ahora: float) -> dict:
    duraciones = []
    respuesta_ia = []
    ejecutivo = {"derivacion": [], "visita_agendada": [], "asignacion": []}
    pendientes = {"derivacion": 0, "visita_agendada": 0, "asignacion": 0}
    por_agente: dict[str, dict] = defaultdict(lambda: {"conversaciones": set(), "mensajes": 0, "tiempos": []})

    for conv, evs in por_conv.items():
        evs = sorted(evs, key=lambda e: e["momento"])
        mensajes = [e for e in evs if e["tipo"] == "mensaje"]
        en = [e for e in mensajes if inicio <= e["momento"] < fin]
        if len(en) >= 2:
            duraciones.append(en[-1]["momento"] - en[0]["momento"])

        esperando_desde = None
        for e in mensajes:
            if e["actor"] == "cliente":
                if esperando_desde is None:
                    esperando_desde = e["momento"]
            else:
                if e["actor"] == "ia" and esperando_desde is not None and inicio <= e["momento"] < fin:
                    respuesta_ia.append(e["momento"] - esperando_desde)
                esperando_desde = None
            if inicio <= e["momento"] < fin and e["actor"] in ("ia", "humano"):
                nombre = "IA" if e["actor"] == "ia" else str(e["detalle"].get("agente") or "Equipo")
                por_agente[nombre]["conversaciones"].add(conv)
                por_agente[nombre]["mensajes"] += 1

        humanos = [e for e in mensajes if e["actor"] == "humano"]
        for e in evs:
            if e["tipo"] not in ejecutivo or not (inicio <= e["momento"] < fin):
                continue
            siguiente = next((m for m in humanos if m["momento"] >= e["momento"]), None)
            if siguiente is None:
                pendientes[e["tipo"]] += 1
                continue
            demora = siguiente["momento"] - e["momento"]
            ejecutivo[e["tipo"]].append(demora)
            nombre = str(siguiente["detalle"].get("agente") or "Equipo")
            por_agente[nombre]["tiempos"].append(demora)

    if respuesta_ia:
        por_agente["IA"]["tiempos"].extend(respuesta_ia)

    agentes = []
    for nombre, datos in sorted(por_agente.items(), key=lambda x: (x[0] != "IA", x[0])):
        agentes.append(
            {
                "agente": nombre,
                "tipo": "IA" if nombre == "IA" else "Humano",
                "conversaciones": len(datos["conversaciones"]),
                "mensajes": datos["mensajes"],
                "tiempo_respuesta": _resumen(datos["tiempos"]),
            }
        )

    return {
        "duracion_conversacion": _resumen(duraciones),
        "respuesta_ia": _resumen(respuesta_ia),
        "ejecutivo_desde_derivacion": {**_resumen(ejecutivo["derivacion"]), "sin_respuesta": pendientes["derivacion"]},
        "ejecutivo_desde_visita": {**_resumen(ejecutivo["visita_agendada"]), "sin_respuesta": pendientes["visita_agendada"]},
        "ejecutivo_desde_asignacion": {**_resumen(ejecutivo["asignacion"]), "sin_respuesta": pendientes["asignacion"]},
        "por_agente": agentes,
    }


def _resumen(valores: list[float]) -> dict:
    if not valores:
        return {"promedio": None, "mediana": None, "casos": 0}
    return {
        "promedio": round(statistics.fmean(valores), 1),
        "mediana": round(statistics.median(valores), 1),
        "casos": len(valores),
    }


def _por(fichas: list[dict], campo: str, vacio: str, nombres: dict | None = None) -> list[dict]:
    cuenta: Counter = Counter()
    for f in fichas:
        valor = str(f.get(campo) or "").strip()
        if valor:
            valor = (nombres or {}).get(valor.lower(), valor)
            if campo == "proyecto":
                valor = _titulo(valor)
        else:
            valor = vacio
        if valor:
            cuenta[valor] += 1
    return [{"nombre": k, "valor": v} for k, v in cuenta.most_common()]


def _titulo(texto: str) -> str:
    # "villa real v" y "Villa Real V" son el mismo proyecto.
    partes = texto.split()
    return " ".join(p.upper() if set(p.lower()) <= set("ivx") else p.capitalize() for p in partes)


def _contar(valores) -> list[dict]:
    cuenta = Counter(str(v).strip().capitalize() for v in valores if str(v).strip())
    return [{"nombre": k, "valor": v} for k, v in cuenta.most_common()]


def _pct(parte: int, total: int) -> float:
    return round(100 * parte / total, 1) if total else 0.0


def _fila(f: dict) -> dict:
    def hora(t):
        return datetime.fromtimestamp(float(t), zona()).strftime("%d/%m %H:%M") if t else ""

    return {
        "conversacion": f["conversacion"],
        "nombre": f.get("nombre") or "",
        "telefono": f.get("telefono") or "",
        "origen": f.get("origen") or "",
        "campana": f.get("campana") or "",
        "proyecto": _titulo(f["proyecto"]) if f.get("proyecto") else "",
        "forma_pago": f.get("forma_pago") or "",
        "presupuesto_usd": f.get("presupuesto_usd"),
        "cuota_usd": f.get("cuota_usd"),
        "motivo_compra": f.get("motivo_compra") or "",
        "estado": f["estado"],
        "calidad": f["calidad"],
        "detalle": f.get("descarte_motivo") or f.get("derivado_motivo") or "",
        "visita": f.get("visita") or "",
        "asignado_a": f.get("asignado_a") or "",
        "primer_contacto": hora(f["primer_contacto"]),
        "ultimo_mensaje": hora(f.get("ultimo_mensaje")),
        "mensajes_cliente": f["mensajes"].get("cliente", 0),
        "mensajes_ia": f["mensajes"].get("ia", 0),
        "mensajes_humano": f["mensajes"].get("humano", 0),
    }
