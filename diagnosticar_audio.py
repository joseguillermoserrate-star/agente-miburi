"""Prueba, paso a paso, por qué el agente no entiende un audio.

En el servidor (Coolify → la app del agente → Terminal):

    python diagnosticar_audio.py 1

donde 1 es el número de la conversación en Chatwoot (el de la URL:
/app/accounts/1/conversations/1). Busca el último audio de esa conversación,
lo baja de Chatwoot y se lo pasa a quien transcribe (Gemini u OpenAI),
mostrando qué pasa en cada paso. No le contesta nada a nadie.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from agente import Config  # noqa: E402
from agente.canales.adjuntos import (  # noqa: E402
    TranscriptorGemini,
    TranscriptorOpenAI,
    _tipo_de_audio_para_gemini,
    adjuntos_de,
)
from agente.canales.chatwoot import Chatwoot  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("Uso: python diagnosticar_audio.py <número de conversación>")
        return 1
    conversacion = sys.argv[1]

    config = Config.desde_entorno()
    print(f"1) Configuración: proveedor={config.proveedor} modelo={config.modelo}")
    print(f"   TRANSCRIBIR_AUDIOS={config.transcribir_audios} "
          f"clave OpenAI={'sí' if config.clave_openai else 'no'} "
          f"clave Google={'sí' if config.clave_gemini else 'no'}")
    if config.transcribir_audios and config.clave_openai:
        transcribir = TranscriptorOpenAI(config.clave_openai, config.modelo_transcripcion)
    elif config.transcribir_audios and config.clave_gemini:
        transcribir = TranscriptorGemini(config.clave_gemini, config.modelo_transcripcion_gemini)
    else:
        print("   ✗ No hay quién transcriba: falta TRANSCRIBIR_AUDIOS=1 o la clave.")
        return 1
    print(f"   Transcribe: {transcribir!r}")

    canal = Chatwoot(config.chatwoot_url, config.chatwoot_token, config.chatwoot_cuenta_id)
    print(f"\n2) Buscando el último audio de la conversación {conversacion}…")
    mensajes = canal._api("GET", f"conversations/{conversacion}/messages").get("payload") or []
    audio = None
    for m in reversed(mensajes):
        for a in adjuntos_de(m):
            if str(a.get("file_type") or "").lower() == "audio":
                audio = a
                break
        if audio:
            break
    if not audio:
        tipos = sorted({str(a.get("file_type")) for m in mensajes for a in adjuntos_de(m)})
        print(f"   ✗ No encontré audios. Tipos de adjuntos que sí hay: {tipos or 'ninguno'}")
        return 1
    print(f"   ✓ Audio id={audio.get('id')} data_url={str(audio.get('data_url'))[:90]}…")

    print("\n3) Bajándolo de Chatwoot…")
    try:
        datos, tipo = canal.bajar(str(audio.get("data_url") or ""), 25 * 1024 * 1024)
    except Exception as e:  # noqa: BLE001
        print(f"   ✗ No se pudo bajar: {type(e).__name__}: {e}")
        return 1
    print(f"   ✓ {len(datos)} bytes · tipo declarado: {tipo!r} · primeros bytes: {datos[:8]!r}")
    print(f"   Tipo que se le manda a Gemini: {_tipo_de_audio_para_gemini(datos, tipo, 'audio')}")

    print("\n4) Transcribiendo…")
    try:
        texto = transcribir(datos, tipo, "audio.ogg")
    except Exception as e:  # noqa: BLE001
        print(f"   ✗ Falló: {type(e).__name__}: {e}")
        return 1
    print(f"   ✓ Texto: {texto!r}" if texto else "   ✗ La transcripción vino vacía.")
    return 0 if texto else 1


if __name__ == "__main__":
    raise SystemExit(main())
