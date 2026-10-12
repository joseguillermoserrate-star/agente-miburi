"""Dónde guarda el CRM lo que pasa: dos tablas en la misma base del agente.

    crm_leads    una fila por conversación (= un lead): quién es, de dónde
                 vino, qué busca, si agendó visita, si se derivó, si se descartó.
    crm_eventos  todo lo que pasó, con su hora: cada mensaje (del cliente, de
                 la IA o de una persona del equipo), cada visita, cada
                 derivación, cada asignación. De acá salen los tiempos.

En producción es el mismo Postgres de la memoria (POSTGRES_DSN), así el panel
lee en vivo y Power BI se puede conectar a las mismas tablas. Sin Postgres
(pruebas en la computadora) usa un archivo SQLite (CRM_SQLITE).

Las horas se guardan como segundos desde 1970 (UTC): sirven igual en las dos
bases y se pasan a hora de Bolivia recién al mostrarlas.

Regla de oro: el CRM NUNCA puede frenar al agente. Si la base no responde, se
registra el error y la conversación sigue como si nada.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from typing import Any

registro = logging.getLogger(__name__)

# Columnas de crm_leads que se pueden escribir (todo lo demás se ignora: así
# nada que venga de afuera puede armar SQL).
CAMPOS_LEAD = (
    "telefono",
    "nombre",
    "origen",
    "campana",
    "canal",
    "primer_contacto",
    "ultimo_mensaje",
    "forma_pago",
    "presupuesto_usd",
    "cuota_usd",
    "motivo_compra",
    "proyecto",
    "fuera_de_presupuesto",
    "visita",
    "visita_en",
    "derivado_en",
    "derivado_motivo",
    "descartado_en",
    "descarte_motivo",
    "asignado_a",
    "asignado_en",
)

# Estos se escriben una sola vez: el primero que llega queda.
SOLO_LA_PRIMERA_VEZ = {"primer_contacto", "origen", "campana", "canal"}

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS crm_leads (
    conversacion TEXT PRIMARY KEY,
    telefono TEXT,
    nombre TEXT,
    origen TEXT,
    campana TEXT,
    canal TEXT,
    primer_contacto DOUBLE PRECISION,
    ultimo_mensaje DOUBLE PRECISION,
    forma_pago TEXT,
    presupuesto_usd DOUBLE PRECISION,
    cuota_usd DOUBLE PRECISION,
    motivo_compra TEXT,
    proyecto TEXT,
    fuera_de_presupuesto INTEGER,
    visita TEXT,
    visita_en DOUBLE PRECISION,
    derivado_en DOUBLE PRECISION,
    derivado_motivo TEXT,
    descartado_en DOUBLE PRECISION,
    descarte_motivo TEXT,
    asignado_a TEXT,
    asignado_en DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS crm_eventos (
    id {autoincremento},
    momento DOUBLE PRECISION NOT NULL,
    conversacion TEXT NOT NULL,
    tipo TEXT NOT NULL,
    actor TEXT,
    detalle TEXT
);
CREATE INDEX IF NOT EXISTS crm_eventos_momento ON crm_eventos (momento);
CREATE INDEX IF NOT EXISTS crm_eventos_conversacion ON crm_eventos (conversacion);
"""


class Almacen:
    """Las dos tablas, en Postgres (dsn) o en SQLite (ruta)."""

    def __init__(self, dsn: str = "", ruta_sqlite: str = "") -> None:
        if not dsn and not ruta_sqlite:
            raise ValueError("El CRM necesita POSTGRES_DSN o CRM_SQLITE")
        self.dsn = dsn
        self.ruta_sqlite = ruta_sqlite
        self.es_postgres = bool(dsn)
        self._conexion = None
        self._candado = threading.Lock()
        self._listo = False

    # -- Conexión --------------------------------------------------------------

    def _conectar(self):
        if self._conexion is not None:
            return self._conexion
        if self.es_postgres:
            import psycopg

            self._conexion = psycopg.connect(self.dsn, autocommit=True, connect_timeout=10)
        else:
            self._conexion = sqlite3.connect(self.ruta_sqlite, check_same_thread=False)
            self._conexion.isolation_level = None  # autocommit
        if not self._listo:
            auto = (
                "BIGSERIAL PRIMARY KEY"
                if self.es_postgres
                else "INTEGER PRIMARY KEY AUTOINCREMENT"
            )
            for sentencia in _ESQUEMA.format(autoincremento=auto).split(";"):
                if sentencia.strip():
                    self._conexion.execute(sentencia)
            self._listo = True
        return self._conexion

    def _ejecutar(self, sql: str, parametros: tuple = (), traer: bool = False) -> list:
        """Corre una sentencia. Si la conexión se cayó, reintenta una vez."""
        if self.es_postgres:
            sql = sql.replace("?", "%s")
        with self._candado:
            for intento in (1, 2):
                try:
                    cursor = self._conectar().execute(sql, parametros)
                    return list(cursor.fetchall()) if traer else []
                except Exception:
                    # Conexión rota (Postgres reiniciado, red): se tira y se
                    # abre otra. La segunda vez el error sube.
                    try:
                        if self._conexion is not None:
                            self._conexion.close()
                    except Exception:
                        pass
                    self._conexion = None
                    if intento == 2:
                        raise
        return []

    # -- Escribir --------------------------------------------------------------

    def evento(
        self,
        conversacion: str,
        tipo: str,
        actor: str = "",
        detalle: dict | None = None,
        momento: float | None = None,
    ) -> None:
        self._ejecutar(
            "INSERT INTO crm_eventos (momento, conversacion, tipo, actor, detalle) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                float(momento or time.time()),
                str(conversacion),
                tipo,
                actor or "",
                json.dumps(detalle or {}, ensure_ascii=False),
            ),
        )

    def lead(self, conversacion: str, solo_si_falta: tuple = (), **campos: Any) -> None:
        """Crea el lead o le completa datos. Lo vacío no pisa lo que ya había.

        Los campos de `solo_si_falta` se escriben solo si estaban vacíos (por
        ejemplo el nombre de perfil de WhatsApp: no pisa el que dio al agendar).
        """
        campos = {
            k: v
            for k, v in campos.items()
            if k in CAMPOS_LEAD and v is not None and v != ""
        }
        columnas = ["conversacion", *campos]
        valores = [str(conversacion), *campos.values()]
        actualizar = [
            f"{c} = COALESCE(crm_leads.{c}, excluded.{c})"
            if c in SOLO_LA_PRIMERA_VEZ or c in solo_si_falta
            else f"{c} = excluded.{c}"
            for c in campos
        ]
        sql = (
            f"INSERT INTO crm_leads ({', '.join(columnas)}) "
            f"VALUES ({', '.join('?' for _ in columnas)}) "
            "ON CONFLICT (conversacion) DO "
            + (f"UPDATE SET {', '.join(actualizar)}" if actualizar else "NOTHING")
        )
        self._ejecutar(sql, tuple(valores))

    def borrar_conversacion(self, conversacion: str) -> None:
        """Para el /reset de pruebas: que una charla de prueba no ensucie el CRM."""
        self._ejecutar("DELETE FROM crm_eventos WHERE conversacion = ?", (str(conversacion),))
        self._ejecutar("DELETE FROM crm_leads WHERE conversacion = ?", (str(conversacion),))

    # -- Leer ------------------------------------------------------------------

    def leads(self) -> list[dict]:
        columnas = ["conversacion", *CAMPOS_LEAD]
        filas = self._ejecutar(f"SELECT {', '.join(columnas)} FROM crm_leads", traer=True)
        return [dict(zip(columnas, f)) for f in filas]

    def eventos(self, desde: float = 0.0, hasta: float = 0.0) -> list[dict]:
        sql = "SELECT momento, conversacion, tipo, actor, detalle FROM crm_eventos"
        parametros: tuple = ()
        if desde or hasta:
            sql += " WHERE momento >= ? AND momento < ?"
            parametros = (float(desde), float(hasta or 9e12))
        sql += " ORDER BY momento, id"
        salida = []
        for momento, conversacion, tipo, actor, detalle in self._ejecutar(sql, parametros, traer=True):
            try:
                datos = json.loads(detalle or "{}")
            except ValueError:
                datos = {}
            salida.append(
                {
                    "momento": float(momento),
                    "conversacion": str(conversacion),
                    "tipo": tipo,
                    "actor": actor or "",
                    "detalle": datos,
                }
            )
        return salida
