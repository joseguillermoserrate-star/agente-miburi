// Lista de precios -> JSON para el agente (Extensiones > Apps Script de la hoja de precios)
// La PRIMERA pestaña es la lista de precios. La pestaña «Tipo de cambio» tiene en B1
// los bolivianos por dólar (ej. 11,73), que el agente usa solo si le piden precios en Bs.
const TOKEN = 'PEGA_AQUI_TU_TOKEN';

function doGet(e) {
  const token = (e && e.parameter && e.parameter.token) || '';
  if (token !== TOKEN) return responder({ ok: false, error: 'no autorizado' });

  const libro = SpreadsheetApp.getActiveSpreadsheet();
  const hoja = libro.getSheets()[0];
  const valores = hoja.getDataRange().getValues();
  const encabezados = valores.shift().map(function (h) { return String(h).trim(); });
  const filas = valores
    .filter(function (fila) { return fila.some(function (c) { return c !== '' && c !== null; }); })
    .map(function (fila) {
      const obj = {};
      encabezados.forEach(function (h, i) { if (h) obj[h] = fila[i]; });
      return obj;
    });

  const hojaTc = libro.getSheetByName('Tipo de cambio');
  const tipoCambio = hojaTc ? hojaTc.getRange('B1').getValue() : '';

  return responder({
    ok: true,
    actualizado: new Date().toISOString(),
    filas: filas,
    tipo_cambio: tipoCambio
  });
}

function responder(datos) {
  return ContentService.createTextOutput(JSON.stringify(datos))
    .setMimeType(ContentService.MimeType.JSON);
}
