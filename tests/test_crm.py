"""Pruebas del CRM: qué se anota, cómo se calculan los KPIs y el panel.

Usan SQLite en una carpeta temporal: no tocan Postgres ni salen a internet.
"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agente import crm  # noqa: E402
from agente.crm.almacen import Almacen  # noqa: E402
from agente.crm.reporte import armar  # noqa: E402


@pytest.fixture
def base(tmp_path):
    almacen = Almacen(ruta_sqlite=str(tmp_path / "crm.sqlite"))
    crm.reiniciar(almacen)
    yield almacen
    crm.reiniciar(None)


def mensaje(conv, texto, tipo="incoming", privado=False, nombre="Ana", telefono="+59170000000", **extra):
    return {
        "event": "message_created",
        "id": int(time.time() * 1000) % 10**9,
        "content": texto,
        "message_type": tipo,
        "private": privado,
        "conversation": {"id": conv, "channel": "Channel::Whatsapp"},
        "sender": {"id": 1, "name": nombre, "phone_number": telefono},
        **extra,
    }


# -- Lo que se anota ---------------------------------------------------------


def test_un_mensaje_que_entra_crea_el_lead(base):
    crm.anotar_mensaje(mensaje(10, "Hola, quiero info de lotes"))
    leads = base.leads()
    assert len(leads) == 1
    assert leads[0]["conversacion"] == "10"
    assert leads[0]["nombre"] == "Ana"
    assert leads[0]["telefono"] == "+59170000000"
    assert leads[0]["origen"] == "WhatsApp directo"
    assert leads[0]["canal"] == "Whatsapp"
    assert [e["actor"] for e in base.eventos()] == ["cliente"]


def test_separa_lo_que_escribio_la_ia_de_lo_que_escribio_el_equipo(base):
    crm.anotar_mensaje(mensaje(11, "hola"))
    crm.lo_va_a_mandar_la_ia("11", "¡Buenas! ¿Contado o plazo?")
    crm.anotar_mensaje(mensaje(11, "¡Buenas! ¿Contado o plazo?", tipo="outgoing", nombre="Jose"))
    crm.anotar_mensaje(mensaje(11, "Le escribe Juan de Dios", tipo="outgoing", nombre="Juan de Dios"))
    crm.anotar_mensaje(mensaje(11, "nota interna", tipo="outgoing", privado=True))

    eventos = base.eventos()
    assert [e["actor"] for e in eventos] == ["cliente", "ia", "humano"]
    assert eventos[2]["detalle"]["agente"] == "Juan de Dios"


def test_detecta_los_anuncios_de_meta(base):
    referral = {"content_attributes": {"referral": {"headline": "Lotes Villa Real V", "source_type": "ad"}}}
    crm.anotar_mensaje(mensaje(12, "Quiero más información", **referral))
    crm.anotar_mensaje(mensaje(13, "Hola, vi su anuncio en Facebook"))
    leads = {l["conversacion"]: l for l in base.leads()}
    assert leads["12"]["origen"] == "Anuncio Meta"
    assert leads["12"]["campana"] == "Lotes Villa Real V"
    assert leads["13"]["origen"] == "Anuncio Meta"


def test_el_nombre_que_dio_al_agendar_no_lo_pisa_el_de_whatsapp(base):
    crm.anotar_mensaje(mensaje(14, "hola", nombre="Juancho 🔥"))
    crm.anotar_visita("14", "2026-10-15", "10:00", nombre="Juan Pérez", proyecto="Villa Real V")
    crm.anotar_mensaje(mensaje(14, "gracias", nombre="Juancho 🔥"))
    lead = base.leads()[0]
    assert lead["nombre"] == "Juan Pérez"
    assert lead["visita"] == "2026-10-15 10:00"


def test_lo_vacio_no_borra_lo_que_ya_sabia(base):
    crm.anotar_consulta_de_precios("15", forma_de_pago="contado", presupuesto_usd=12000, motivo_compra="inversión")
    crm.anotar_consulta_de_precios("15", forma_de_pago="contado", proyecto="Villa Real III")
    lead = base.leads()[0]
    assert lead["presupuesto_usd"] == 12000
    assert lead["motivo_compra"] == "inversion"
    assert lead["proyecto"] == "Villa Real III"


def test_si_el_crm_falla_no_rompe_nada(tmp_path):
    class Roto(Almacen):
        def _ejecutar(self, *a, **k):
            raise RuntimeError("base caída")

    crm.reiniciar(Roto(ruta_sqlite=str(tmp_path / "x.sqlite")))
    try:
        crm.anotar_mensaje(mensaje(1, "hola"))  # no levanta
        crm.anotar_derivacion("1", "quiere reservar")
    finally:
        crm.reiniciar(None)


def test_sin_postgres_ni_sqlite_esta_apagado(monkeypatch):
    crm.reiniciar(None)
    monkeypatch.delenv("CRM_SQLITE", raising=False)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    assert crm.almacen() is None
    crm.anotar_mensaje(mensaje(1, "hola"))  # no hace nada, no falla
    crm.reiniciar(None)


# -- Los KPIs -----------------------------------------------------------------


def test_los_numeros_del_reporte(base):
    t = time.time() - 3 * 86400  # hace 3 días

    def ev(conv, actor, segundos, tipo="mensaje", **detalle):
        base.evento(conv, tipo, actor, detalle, t + segundos)

    # 1: calificado, agenda visita, el ejecutivo lo llama 30 min después
    base.lead("1", primer_contacto=t, ultimo_mensaje=t + 600, forma_pago="contado", presupuesto_usd=12000,
              proyecto="villa real v", motivo_compra="inversion", visita="2026-10-15 10:00", visita_en=t + 500,
              origen="Anuncio Meta", campana="Lotes VR5")
    ev("1", "cliente", 0)
    ev("1", "ia", 8)
    ev("1", "cliente", 100)
    ev("1", "ia", 110)
    ev("1", "ia", 500, tipo="visita_agendada")
    ev("1", "humano", 500 + 1800, agente="Juan de Dios")
    # 2: derivado (quiere reservar), el ejecutivo responde en 10 min
    base.lead("2", primer_contacto=t + 50, ultimo_mensaje=t + 400, forma_pago="plazo", cuota_usd=150,
              proyecto="Villa Real IV", derivado_en=t + 300, derivado_motivo="quiere reservar", origen="WhatsApp directo")
    ev("2", "cliente", 50)
    ev("2", "ia", 60)
    ev("2", "ia", 300, tipo="derivacion", motivo="quiere reservar")
    ev("2", "humano", 900, agente="Juan de Dios")
    # 3: descartado
    base.lead("3", primer_contacto=t + 70, ultimo_mensaje=t + 90, descartado_en=t + 90, descarte_motivo="número equivocado", origen="WhatsApp directo")
    ev("3", "cliente", 70)
    ev("3", "ia", 90)
    # 4: preguntó y desapareció hace 3 días -> sin respuesta
    base.lead("4", primer_contacto=t + 80, ultimo_mensaje=t + 85, origen="WhatsApp directo")
    ev("4", "cliente", 80)
    ev("4", "ia", 85)
    # 5: fuera del rango (hace 60 días): no cuenta
    base.lead("5", primer_contacto=t - 57 * 86400, origen="WhatsApp directo")

    r = armar(base.leads(), base.eventos())
    g = r["general"]
    assert g["leads"] == 4
    assert g["calificados"] == 2
    assert g["visitas"] == 1
    assert g["derivados"] == 1
    assert g["visita_o_derivado"] == 2
    assert g["malos"] == 2
    assert g["conversion_visita"] == 25.0

    malos = {m["nombre"]: m["valor"] for m in r["calidad"]["malos"]}
    assert malos == {"Descartado": 1, "Sin respuesta": 1}
    assert {p["nombre"]: p["valor"] for p in r["calidad"]["por_proyecto"]}["Villa Real V"] == 1
    assert r["calidad"]["por_campana"] == [{"nombre": "Lotes VR5", "valor": 1}]

    p = r["productividad"]
    assert p["ejecutivo_desde_derivacion"]["mediana"] == 600
    assert p["ejecutivo_desde_visita"]["mediana"] == 1800
    # La IA: 8 s, 10 s, 10 s, 20 s, 5 s
    assert p["respuesta_ia"]["casos"] == 5
    assert p["respuesta_ia"]["mediana"] == 10
    agentes = {a["agente"]: a for a in p["por_agente"]}
    assert agentes["IA"]["conversaciones"] == 4
    assert agentes["Juan de Dios"]["conversaciones"] == 2

    estados = {f["conversacion"]: f["estado"] for f in r["leads"]}
    assert estados == {"1": "visita agendada", "2": "derivado a ventas", "3": "malo", "4": "malo"}


# -- La derivación -------------------------------------------------------------


def test_derivar_etiqueta_recien_despues_de_responder(base):
    from test_chatwoot import ChatwootFalso

    from agente.crm.herramientas import aplicar_derivacion_pendiente, derivar_a_ejecutivo

    canal = ChatwootFalso()
    texto = derivar_a_ejecutivo.invoke(
        {"motivo": "quiere reservar", "resumen": "Ana, contado USD 12.000, VR5"},
        config={"configurable": {"thread_id": "77"}},
    )
    assert "Juan de Dios" in texto
    assert canal.etiquetas_de("77") == []  # todavía no: primero sale la respuesta

    aplicar_derivacion_pendiente("77", canal)
    assert "humano" in canal.etiquetas_de("77")
    assert any("quiere reservar" in n for n in canal.notas())
    assert base.leads()[0]["derivado_motivo"] == "quiere reservar"


# -- El panel -----------------------------------------------------------------


def _web(monkeypatch, clave="secreta"):
    from test_agente import agente_falso
    from test_chatwoot import ChatwootFalso, cliente

    if clave:
        monkeypatch.setenv("CRM_CLAVE", clave)
    else:
        monkeypatch.delenv("CRM_CLAVE", raising=False)
    monkeypatch.setenv("CRM_USUARIO", "admin")
    return cliente(ChatwootFalso(), agente_falso(["hola"]))


def _auth(usuario="admin", clave="secreta"):
    return {"Authorization": "Basic " + base64.b64encode(f"{usuario}:{clave}".encode()).decode()}


def test_sin_clave_el_panel_no_existe(base, monkeypatch):
    with _web(monkeypatch, clave="") as web:
        assert web.get("/crm").status_code == 404


def test_el_panel_pide_usuario_y_clave(base, monkeypatch):
    with _web(monkeypatch) as web:
        assert web.get("/crm").status_code == 401
        assert web.get("/crm", headers=_auth(clave="otra")).status_code == 401
        assert web.get("/crm/datos", headers=_auth(clave="otra")).status_code == 401
        pagina = web.get("/crm", headers=_auth())
        assert pagina.status_code == 200
        assert "CRM Villa Real" in pagina.text


def test_el_webhook_alimenta_el_panel(base, monkeypatch):
    from test_chatwoot import TOKEN

    with _web(monkeypatch) as web:
        web.post(f"/chatwoot/{TOKEN}", json=mensaje(55, "hola, vi el anuncio"))
        datos = web.get("/crm/datos", headers=_auth()).json()
        csv = web.get("/crm/leads.csv", headers=_auth())

    assert datos["general"]["leads"] == 1
    assert datos["leads"][0]["origen"] == "Anuncio Meta"
    assert datos["chatwoot"].endswith("/conversations/")
    assert csv.status_code == 200
    assert "conversacion;nombre" in csv.text


def test_comparativo_acciones_y_segmentos(base):
    ahora = time.time()
    t = ahora - 2 * 86400
    # Período actual: uno calificado que no avanzó (recontactar) y una visita sin confirmar.
    base.lead("1", primer_contacto=t, ultimo_mensaje=t + 100, forma_pago="contado", presupuesto_usd=9000,
              origen="Anuncio Meta", proyecto="Villa Real IV")
    base.evento("1", "mensaje", "cliente", {}, t)
    manana = time.strftime("%Y-%m-%d", time.localtime(ahora + 86400 * 2))
    base.lead("2", primer_contacto=t, ultimo_mensaje=t, forma_pago="plazo", cuota_usd=150, visita=f"{manana} 10:00",
              visita_en=t + 50, origen="WhatsApp directo")
    base.evento("2", "mensaje", "cliente", {}, t)
    base.evento("2", "visita_agendada", "ia", {}, t + 50)
    # Derivado sin respuesta.
    base.lead("3", primer_contacto=t, ultimo_mensaje=t, derivado_en=t + 10, derivado_motivo="quiere reservar", origen="Anuncio Meta")
    # Período anterior: un lead.
    base.lead("4", primer_contacto=ahora - 40 * 86400, origen="WhatsApp directo")

    r = armar(base.leads(), base.eventos())
    assert r["comparativo"]["leads"] == 1
    assert r["general"]["leads"] == 3
    ac = r["acciones"]
    assert [x["conversacion"] for x in ac["visitas"]] == ["2"]
    assert [x["conversacion"] for x in ac["derivaciones"]] == ["3"]
    assert [x["conversacion"] for x in ac["recontactar"]] == ["1"]
    origen = {x["nombre"]: x for x in r["segmentos"]["origen"]}
    assert origen["Anuncio Meta"]["leads"] == 2
    assert origen["Anuncio Meta"]["derivacion"] == 50.0
    assert r["presupuestos"]["contado_mediana"] == 9000
    assert sum(sum(f) for f in r["horarios"]) == 2
    assert r["productividad"]["visitas_confirmadas"] == 0.0


def test_costos_del_mes_y_de_la_ia(base, monkeypatch):
    monkeypatch.setenv("PRECIO_WHATSAPP_MENSAJE", "0.01")
    ahora = time.time()
    t = ahora - 600
    base.lead("1", primer_contacto=t, origen="Anuncio Meta")
    base.lead("2", primer_contacto=t, origen="WhatsApp directo")
    for conv in ("1", "2"):
        base.evento(conv, "mensaje", "cliente", {}, t)
        base.evento(conv, "mensaje", "ia", {}, t + 10)
        base.evento(conv, "consumo_ia", "ia", {"entrada": 10000, "salida": 400, "cache": 0}, t + 10)
    r = armar(base.leads(), base.eventos())
    k = r["costos"]
    assert k["mensajes_mes"] == 2
    assert k["gratis_por_anuncio"] == 1  # el del anuncio, dentro de las 72 h
    assert k["cuentan_para_cupo"] == 1
    assert k["costo_whatsapp_mes"] == 0  # dentro de los 1.000 gratis
    # 2 respuestas x (10.000 x 0,75 + 400 x 3,75) / 1M = 0,018
    assert abs(k["costo_ia"] - 0.018) < 1e-9
    assert k["costo_ia_por_lead"] == 0.009
    assert k["costo_whatsapp_por_lead"] == 0.005


def test_suma_los_tokens_de_todo_el_turno():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from agente.agente import Respuesta, _sumar_el_turno

    msgs = [
        HumanMessage("viejo"),
        AIMessage("", usage_metadata={"input_tokens": 999, "output_tokens": 9, "total_tokens": 1008}),
        HumanMessage("precio?"),
        AIMessage("", usage_metadata={"input_tokens": 6000, "output_tokens": 50, "total_tokens": 6050}),
        ToolMessage("lista", tool_call_id="x"),
        AIMessage("hola", usage_metadata={"input_tokens": 6500, "output_tokens": 300, "total_tokens": 6800,
                                          "input_token_details": {"cache_read": 4000}}),
    ]
    r = Respuesta(texto="hola", modelo="m", tokens_entrada=6500, tokens_salida=300)
    _sumar_el_turno(r, msgs)
    assert (r.tokens_entrada, r.tokens_salida, r.tokens_cache_leidos) == (12500, 350, 4000)
