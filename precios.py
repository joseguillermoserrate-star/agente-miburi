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
MAXIMO_DE_RESULTADOS = 15

_cache: dict = {"momento": 0.0, "filas": None}


@tool
def consultar_lotes(
    proyecto: str = "",
    presupuesto_contado_maximo_usd: float = 0,
    cuota_mensual_maxima_usd: float = 0,
    incluir_no_disponibles: bool = False,
) -> str:
    """Consulta la lista de precios ACTUALIZADA de los lotes y su disponibilidad.

    Usala SIEMPRE que pregunten por precios, precio por metro cuadrado,
    descuentos, pago al contado, financiamiento, cuota inicial, cuota mensual,
    plazos, superficie o qué lotes hay. Nunca inventes un precio ni
    un descuento: si no está en el resultado de esta herramienta, no lo sabés.

    Todos los filtros son opcionales; dejalos vacíos o en 0 para no filtrar.

    Args:
        proyecto: Nombre (o parte) del proyecto, por ejemplo "Villa Real V".
        presupuesto_contado_maximo_usd: Lo máximo que la persona puede pagar
            al contado, en dólares. Se compara con el precio al contado con el
            descuento inicial. 0 = sin tope.
        cuota_mensual_maxima_usd: La cuota mensual máxima que puede pagar, en
            dólares. 0 = sin tope.
        incluir_no_disponibles: True solo si preguntan específicamente por
            lotes reservados o vendidos. Por defecto, solo los disponibles.
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

    encontradas = []
    for fila in filas:
        if not incluir_no_disponibles and _normal(fila.get("estado")) != "disponible":
            continue
        if proyecto and not _coincide(proyecto, fila.get("proyecto")):
            continue
        if presupuesto_contado_maximo_usd:
            contado, _ = _precios_contado(fila)
            if contado > presupuesto_contado_maximo_usd:
                continue
        if cuota_mensual_maxima_usd and _numero(fila.get("cuota_mensual_usd")) > cuota_mensual_maxima_usd:
            continue
        encontradas.append(fila)

    if not encontradas:
        proyectos = sorted({str(f.get("proyecto", "")).strip() for f in filas if f.get("proyecto")})
        return (
            "No hay lotes que cumplan esos filtros. "
            f"Proyectos que existen en la lista: {', '.join(proyectos) or 'ninguno'}."
        )

    encontradas.sort(key=lambda f: (str(f.get("proyecto", "")), _numero(f.get("precio_lista_usd"))))
    unicas = list(dict.fromkeys(_redactar(f) for f in encontradas))
    lineas = [
        f"Opciones encontradas: {len(unicas)}.",
        "REGLA DE DESCUENTO: ofrecé siempre el precio con el descuento inicial. "
        "El descuento máximo es solo para cuando la persona pide un mejor precio "
        "para pagar al contado; nunca lo ofrezcas de entrada. Los descuentos son "
        "solo para pago al contado, no para el plan a plazos.",
    ]
    lineas.extend(unicas[:MAXIMO_DE_RESULTADOS])
    if len(unicas) > MAXIMO_DE_RESULTADOS:
        lineas.append(
            "(Hay más opciones. Pedile a la persona que acote por proyecto "
            "o presupuesto.)"
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


def _redactar(f: dict) -> str:
    """Una línea por opción, en texto: menos chances de que el modelo se confunda."""
    lista = _numero(f.get("precio_lista_usd"))
    inicial = _porcentaje(f.get("descuento_contado_inicial"))
    maximo = _porcentaje(f.get("descuento_contado_maximo")) or inicial
    contado_inicial, contado_maximo = _precios_contado(f)

    partes = [f"- {f.get('proyecto', '?')}"]
    if f.get("ubicacion"):
        partes.append(str(f.get("ubicacion")))
    partes.append(f"{_formato(f.get('superficie_m2'))} m²")
    if f.get("precio_m2_lista_usd"):
        partes.append(f"USD {_formato(f.get('precio_m2_lista_usd'), 2)}/m² de lista")
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
    if f.get("plazo_anios"):
        partes.append(
            f"A PLAZO {_formato(f.get('plazo_anios'))} años: cuota inicial USD "
            f"{_formato(f.get('cuota_inicial_usd'))} y cuota mensual USD "
            f"{_formato(f.get('cuota_mensual_usd'), 2)}"
        )
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
