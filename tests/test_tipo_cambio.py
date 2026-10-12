from agente import tipo_cambio as tc

PAGINA = """
<p>Cotización Oficial del Boliviano respecto al Dólar (BCB)</p>
<table><tr><th>País / Concepto</th><th>Moneda</th><th>Código</th><th>Tipo de Cambio Oficial (TCO) (Bs/USD)</th></tr>
<tr><td>ESTADOS UNIDOS</td><td>D&Oacute;LAR</td><td>USD</td><td align="right">11.73</td></tr></table>
<table><tr><td>ARGENTINA</td><td>PESO</td><td>ARS</td><td>0.00774</td></tr></table>
"""


def _limpiar(monkeypatch):
    tc._cache.update(momento=0.0, valor=None, fecha="")
    monkeypatch.delenv("TIPO_DE_CAMBIO_BOB", raising=False)


def test_extrae_el_tco_de_la_tabla_del_bcb():
    assert tc.extraer_tco(PAGINA) == 11.73


def test_sin_la_fila_no_inventa():
    assert tc.extraer_tco("<p>mantenimiento</p>") is None


def test_convierte_con_el_valor_del_bcb(monkeypatch):
    _limpiar(monkeypatch)
    monkeypatch.setattr(tc, "leer_bcb", lambda: 11.73)
    texto = tc.convertir_a_bolivianos.invoke({"montos_usd": [16500, 11550]})
    assert "Bs 11,73" in texto
    assert "USD 11.550 = Bs 135.482" in texto
    assert "DÓLARES" in texto


def test_si_el_bcb_falla_usa_el_respaldo(monkeypatch):
    _limpiar(monkeypatch)

    def falla():
        raise OSError("sin red")

    monkeypatch.setattr(tc, "leer_bcb", falla)
    monkeypatch.setenv("TIPO_DE_CAMBIO_BOB", "11.73")
    texto = tc.convertir_a_bolivianos.invoke({"montos_usd": [1000]})
    assert "Bs 11.730" in texto
    assert "referencia configurado" in texto


def test_sin_bcb_ni_respaldo_no_convierte(monkeypatch):
    _limpiar(monkeypatch)
    monkeypatch.setattr(tc, "leer_bcb", lambda: None)
    texto = tc.convertir_a_bolivianos.invoke({"montos_usd": [1000]})
    assert "No conviertas" in texto
