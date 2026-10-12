"""Prueba que el agente puede leer el tipo de cambio del Banco Central.

En el servidor (Coolify → la app del agente → Terminal):

    python probar_tipo_de_cambio.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from agente.tipo_cambio import leer_bcb, tipo_de_cambio  # noqa: E402


def main() -> int:
    print("1) Leyendo la página del Banco Central…")
    try:
        valor = leer_bcb()
    except Exception as e:  # noqa: BLE001
        print(f"   ✗ No respondió: {type(e).__name__}: {e}")
        valor = None
    if valor:
        print(f"   ✓ Tipo de cambio oficial: Bs {valor} por dólar")
    else:
        print("   ✗ No encontré la fila «ESTADOS UNIDOS · DÓLAR · USD».")
    tasa, origen = tipo_de_cambio()
    print(f"2) Lo que usará el agente: {tasa or 'nada (no convertirá)'} ({origen or '-'})")
    if tasa:
        print(f"   Ejemplo: USD 11.550 = Bs {11550 * tasa:,.0f}".replace(",", "."))
    return 0 if valor else 1


if __name__ == "__main__":
    raise SystemExit(main())
