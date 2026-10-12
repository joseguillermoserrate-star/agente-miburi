"""Herramienta: consultar la lista de precios de lotes (Villa Real).

La lista vive en un Google Sheet privado que se actualiza a mano. El agente
no lee la hoja directamente: un Apps Script publicado como aplicación web
la devuelve en JSON, y solo si el pedido trae el token correcto. Así la
hoja sigue privada y no hace falta ninguna cuenta de servicio de Google.

Variables de entorno:

    PRECIOS_URL    la URL de la aplicación web del Apps Script (termina en /exec)
    PRECIOS_TOKEN  el mismo token que está en las propiedades del script

Se guarda la lista en memoria durante PRECIOS_CACHE_SEGUNDOS (60 por
defecto): si diez personas preguntan en el mismo minuto, se consulta la hoja
una sola vez. Un cambio en la hoja se ve, como mucho, un minuto después.

Columnas que espera la primera pestaña de la hoja (la primera fila):

    proyecto · ubicacion · superficie_m2 · precio_m2_lista_usd ·
    precio_lista_usd · descuento_contado_inicial · descuento_contado_maximo ·
    plazo_anios · cuota_inicial_usd · cuota_mensual_usd · estado ·
    observaciones

Cualquier otra columna de la hoja (por ejemplo, una de uso interno) se
ignora: el modelo solo ve lo que arma _redactar(), así que un dato que no
esté ahí nunca le llega al cliente.

El precio al contado NO se escribe en la hoja: se calcula acá con los dos
descuentos, así nunca queda desactualizado respecto del precio de lista.
"""

from __future__ import annotations

import json
import os
import time
import unicodedata
import urllib.parse
import urllib.request

from langchain_core.tools import tool

ESPERA = 20  # segundos: Apps Script a veces tarda en despertar

# Cuántas opciones se le pasan al modelo. Pocas a propósito: en WhatsApp una
# lista larga de precios abruma, y el modelo tiende a repetir todo lo que ve.
OPCIONES_POR_DEFECTO = 3
OPCIONES_MAXIMAS = 5

_cache: dict = {"momento": 0.0, "filas": None}


@tool
def consultar_lotes(
    forma_de_pago: str = "",
    presupuesto_contado_maximo_usd: float = 0,
    cuota_mensual_maxima_usd: float = 0,
    proyecto: str = "",
    cantidad_de_opciones: int = OPCIONES_POR_DEFECTO,
    incluir_no_disponibles: bool = False,
) -> str:
    """Busca en la lista de precios ACTUALIZADA las opciones de lotes que mejor
    le encajan a la persona, según cómo quiere pagar y cuánto tiene.

    Usala SIEMPRE que hables de precios, descuentos, cuotas, plazos o
    disponibilidad. Nunca inventes un precio ni un descuento: si no está en el
    resultado de esta herramienta, no lo sabés.

    Devuelve solo unas pocas opciones (las que mejor aprovechan el presupuesto),
    no la lista completa. Pasale todo lo que la persona ya te dijo.

    Args:
        forma_de_pago: "contado" o "plazo". Si la persona todavía no lo dijo,
            dejalo vacío (pero antes preguntáselo).
        presupuesto_contado_maximo_usd: Para contado: lo máximo que puede pagar,
            en dólares. 0 = no lo dijo.
        cuota_mensual_maxima_usd: Para plazo: la cuota mensual máxima que puede
            pagar, en dólares. 0 = no lo dijo.
        proyecto: Solo si pregunta por un proyecto puntual, por ejemplo
            "Villa Real V". Vacío = todos los proyectos.
        cantidad_de_opciones: Cuántas opciones devolver (1 a 5). Por defecto 3.
        incluir_no_disponibles: True solo si preguntan específicamente por
            lotes reservados o vendidos.
    """
    try:
        filas = _traer_filas()
    except Exception as e:  # noqa: BLE001
        # Devolverlo como texto y no levantarlo: así la charla sigue y el
        # modelo le dice a la persona que un asesor le confirma el dato.
        return (
            "No se pudo consultar la lista de precios en este momento "
            f"({type(e).__name__}). No des precios: decí que un asesor "
            "le confirma la información."
        )

    forma = _normal(forma_de_pago)
    if "plaz" in forma or "cuota" in forma or "credito" in forma or "financ" in forma:
        forma = "plazo"
    elif "contado" in forma or "cash" in forma or "efectivo" in forma:
        forma = "contado"
    else:
        forma = ""
    # Si dio un monto, la forma de pago se deduce del monto.
    if not forma and presupuesto_contado_maximo_usd and not cuota_mensual_maxima_usd:
        forma = "contado"
    if not forma and cuota_mensual_maxima_usd and not presupuesto_contado_maximo_usd:
        forma = "plazo"

    cantidad = max(1, min(int(cantidad_de_opciones or OPCIONES_POR_DEFECTO), OPCIONES_MAXIMAS))

    candidatas = [
        f for f in filas
        if (incluir_no_disponibles or _normal(f.get("estado")) == "disponible")
        and (not proyecto or _coincide(proyecto, f.get("proyecto")))
    ]
    if not candidatas:
        proyectos = sorted({str(f.get("proyecto", "")).strip() for f in filas if f.get("proyecto")})
        return (
            "No hay lotes disponibles con ese criterio. "
            f"Proyectos que existen en la lista: {', '.join(proyectos) or 'ninguno'}."
        )

    def contado(f):
        return _precios_contado(f)[0]

    def cuota(f):
        return _numero(f.get("cuota_mensual_usd"))

    encabezado = []
    if presupuesto_contado_maximo_usd:
        tope = presupuesto_contado_maximo_usd
        entran = [f for f in candidatas if contado(f) <= tope]
        # Lo que mejor aprovecha el presupuesto primero: el más cercano por debajo.
        entran.sort(key=contado, reverse=True)
        if entran:
            elegidas = entran
            encabezado.append(f"Opciones al contado que entran en USD {_formato(tope)}:")
        else:
            elegidas = sorted(candidatas, key=contado)
            encabezado.append(
                f"NINGUNA opción entra en USD {_formato(tope)} al contado. Estas son "
                "las más cercanas: decíselo con tacto y, si le sirve, ofrecé el plan a plazo."
            )
    elif cuota_mensual_maxima_usd:
        tope = cuota_mensual_maxima_usd
        entran = [f for f in candidatas if cuota(f) <= tope]
        entran.sort(key=cuota, reverse=True)
        if entran:
            elegidas = entran
            encabezado.append(f"Opciones a plazo con cuota de hasta USD {_formato(tope)} por mes:")
        else:
            elegidas = sorted(candidatas, key=cuota)
            encabezado.append(
                f"NINGUNA opción tiene cuota de hasta USD {_formato(tope)}. Estas son "
                "las más cercanas: decíselo con tacto."
            )
    else:
        # Sin monto: de cada proyecto, desde la opción más accesible.
        elegidas = sorted(
            candidatas, key=lambda f: (cuota(f) if forma == "plazo" else contado(f))
        )
        encabezado.append(
            "Opciones más accesibles. La persona todavía no dijo su presupuesto: "
            "mostrá como mucho 2 y preguntale cuánto tiene"
            + (" por mes." if forma == "plazo" else " para invertir.")
        )

    unicas = list(dict.fromkeys(_redactar(f, forma) for f in elegidas))
    lineas = encabezado + unicas[:cantidad]
    if len(unicas) > cantidad:
        resto = len(unicas) - cantidad
        lineas.append(
            f"(Hay {resto} {'opción' if resto == 1 else 'opciones'} más; "
            "no las menciones salvo que las pida.)"
        )
    if forma != "plazo":
        lineas.append(
            "REGLA DE DESCUENTO: ofrecé el precio con el descuento inicial. El "
            "descuento máximo es solo si la persona pide un mejor precio al contado; "
            "nunca lo ofrezcas de entrada."
        )
    return "\n".join(lineas)


def _traer_filas() -> list[dict]:
    """La lista completa, desde el caché si es reciente."""
    vigencia = float(os.getenv("PRECIOS_CACHE_SEGUNDOS", "60") or 60)
    if _cache["filas"] is not None and time.time() - _cache["momento"] < vigencia:
        return _cache["filas"]

    url = (os.getenv("PRECIOS_URL") or "").strip()
    token = (os.getenv("PRECIOS_TOKEN") or "").strip()
    if not url or not token:
        raise RuntimeError("Faltan PRECIOS_URL o PRECIOS_TOKEN")

    completa = f"{url}?{urllib.parse.urlencode({'token': token})}"
    # Apps Script responde con una redirección a googleusercontent.com;
    # urllib la sigue sola.
    with urllib.request.urlopen(completa, timeout=ESPERA) as respuesta:
        datos = json.loads(respuesta.read().decode("utf-8"))

    if not isinstance(datos, dict) or not datos.get("ok"):
        raise RuntimeError(f"Respuesta inválida de la hoja: {str(datos)[:200]}")

    filas = datos.get("filas") or []
    _cache["filas"] = filas
    _cache["momento"] = time.time()
    return filas


def _precios_contado(f: dict) -> tuple[float, float]:
    """(precio con descuento inicial, precio con descuento máximo)."""
    lista = _numero(f.get("precio_lista_usd"))
    inicial = _porcentaje(f.get("descuento_contado_inicial"))
    maximo = _porcentaje(f.get("descuento_contado_maximo")) or inicial
    return lista * (1 - inicial), lista * (1 - maximo)


def _redactar(f: dict, forma: str = "") -> str:
    """Una línea por opción, en texto: menos chances de que el modelo se confunda.

    Solo con los datos de la forma de pago que eligió la persona: si quiere a
    plazo, el modelo no ve los precios de contado (y no los mezcla), y al revés.
    """
    lista = _numero(f.get("precio_lista_usd"))
    inicial = _porcentaje(f.get("descuento_contado_inicial"))
    maximo = _porcentaje(f.get("descuento_contado_maximo")) or inicial
    contado_inicial, contado_maximo = _precios_contado(f)

    partes = [f"- {f.get('proyecto', '?')}"]
    if f.get("ubicacion"):
        partes.append(str(f.get("ubicacion")))
    partes.append(f"{_formato(f.get('superficie_m2'))} m²")
    if forma != "plazo":
        partes.append(f"precio de lista USD {_formato(lista)}")
        if inicial:
            partes.append(
                f"AL CONTADO con {_pct(inicial)} de descuento: USD {_formato(contado_inicial)}"
            )
        if maximo > inicial:
            partes.append(
                f"(descuento máximo autorizado {_pct(maximo)}: USD {_formato(contado_maximo)}, "
                "solo si pide mejor precio)"
            )
    if forma != "contado" and f.get("plazo_anios"):
        partes.append(
            f"A PLAZO hasta {_formato(f.get('plazo_anios'))} años: cuota inicial USD "
            f"{_formato(f.get('cuota_inicial_usd'))} y cuota mensual USD "
            f"{_formato(f.get('cuota_mensual_usd'), 2)} (sin descuento)"
        )
    if _normal(f.get("estado")) != "disponible":
        partes.append(f"estado: {f.get('estado', '?')}")
    if f.get("observaciones"):
        partes.append(f"({f.get('observaciones')})")
    return " · ".join(partes)


def _normal(texto) -> str:
    """Minúsculas y sin tildes, para que 'urbanizacion' encuentre 'Urbanización'."""
    texto = unicodedata.normalize("NFKD", str(texto or ""))
    return "".join(c for c in texto if not unicodedata.combining(c)).strip().lower()


def _coincide(buscado, valor) -> bool:
    """Todas las palabras buscadas aparecen en el valor, como palabras enteras.

    Palabras enteras para que "Villa Real V" no encuentre también "Villa Real IV".
    """
    palabras_valor = _normal(valor).split()
    return all(p in palabras_valor for p in _normal(buscado).split())


def _numero(valor) -> float:
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = str(valor or "").replace("$", "").replace("%", "").strip()
    # "1.050,50" (formato boliviano) o "1,050.50" (inglés)
    if "," in texto and "." in texto:
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(".", "").replace(",", ".")
        else:
            texto = texto.replace(",", "")
    elif "," in texto:
        texto = texto.replace(",", ".")
    try:
        return float(texto)
    except ValueError:
        return 0.0


def _porcentaje(valor) -> float:
    """0.3, '30%' o 30 → 0.30."""
    if valor in (None, ""):
        return 0.0
    n = _numero(valor)
    return n / 100 if n > 1 else n


def _pct(fraccion: float, decimales: int = 0) -> str:
    return f"{fraccion * 100:.{decimales}f}%".replace(".", ",")


def _formato(valor, decimales: int = 0) -> str:
    n = _numero(valor)
    texto = f"{n:,.{decimales}f}"
    # Formato boliviano: punto para miles, coma para decimales.
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")
