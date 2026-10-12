/**
 * Agenda de visitas para el agente de WhatsApp (Villa Real).
 *
 * Va pegado en el Apps Script de la hoja "Leads y visitas" (Extensiones →
 * Apps Script). Usa TU Google Calendar principal y anota cada lead en la
 * primera pestaña de la hoja.
 *
 * Reglas:
 *  - Cada visita dura 1 hora (ir y volver).
 *  - Hasta 2 visitas en la misma hora; la tercera tiene que ir a otra hora.
 *  - Solo cuentan las visitas: tus otros eventos del calendario NO bloquean.
 *  - Un cliente puede reagendar su visita a otro horario libre.
 *  - Solo dentro del horario de visitas (abajo) y con anticipación mínima.
 *
 * Después de pegarlo: Implementar → Nueva implementación → Aplicación web
 * (Ejecutar como: Yo · Acceso: Cualquier usuario) y copiar la URL /exec.
 * Si cambiás el código: Administrar implementaciones → ✏️ → Nueva versión
 * (así la URL no cambia).
 */

// ---- Ajustes ---------------------------------------------------------------
var TOKEN = 'PEGA_AQUI_TU_TOKEN';
var ZONA = 'America/La_Paz';
var DESFASE = '-04:00';            // Bolivia no tiene horario de verano
var DIAS_DE_VISITA = [1, 2, 3, 4, 5, 6]; // 0=domingo … 6=sábado
var HORA_INICIO = 9;               // primera visita: 09:00
var HORA_FIN = 16;                 // última visita empieza a las 15:00 (termina 16:00)
var VISITAS_POR_HORA = 2;
var DURACION_MINUTOS = 60;
var ANTICIPACION_MINUTOS = 120;    // no agendar con menos de 2 h de aviso
var PREFIJO = 'Visita lote:';      // así se reconocen las visitas en el calendario
var UBICACION = 'https://maps.app.goo.gl/yn5Qig4LykQ5A45E7'; // punto de encuentro de las visitas
// ----------------------------------------------------------------------------

function doPost(e) {
  var datos = {};
  try {
    datos = JSON.parse((e && e.postData && e.postData.contents) || '{}');
  } catch (err) {
    return responder({ ok: false, error: 'pedido inválido' });
  }
  if (datos.token !== TOKEN) {
    return responder({ ok: false, error: 'no autorizado' });
  }
  try {
    if (datos.accion === 'disponibilidad') return responder(disponibilidad(datos));
    if (datos.accion === 'agendar') return responder(agendar(datos));
    if (datos.accion === 'reagendar') return responder(reagendar(datos));
    return responder({ ok: false, error: 'acción desconocida' });
  } catch (err) {
    return responder({ ok: false, error: String(err) });
  }
}

// Para probar desde el navegador: .../exec?token=...&fecha=2026-10-15
function doGet(e) {
  var p = (e && e.parameter) || {};
  if (p.token !== TOKEN) return responder({ ok: false, error: 'no autorizado' });
  return responder(disponibilidad({ fecha: p.fecha, dias: p.dias }));
}

// ---- Disponibilidad ----------------------------------------------------------

function disponibilidad(datos) {
  var dias = Math.max(1, Math.min(Number(datos.dias) || 1, 7));
  var fecha = datos.fecha || Utilities.formatDate(new Date(), ZONA, 'yyyy-MM-dd');
  var resultado = [];
  for (var d = 0; d < dias; d++) {
    var dia = sumarDias(fecha, d);
    var libres = horariosLibres(dia);
    resultado.push({
      fecha: dia,
      dia_semana: nombreDelDia(dia),
      atiende: libres !== null,
      horarios_libres: libres || []
    });
  }
  return { ok: true, dias: resultado };
}

function horariosLibres(fecha) {
  var inicioDelDia = aFecha(fecha, '00:00');
  var diaSemana = Number(Utilities.formatDate(inicioDelDia, ZONA, 'u')) % 7; // 7=domingo → 0
  if (DIAS_DE_VISITA.indexOf(diaSemana) === -1) return null;

  var desde = aFecha(fecha, dosDigitos(HORA_INICIO) + ':00');
  var hasta = aFecha(fecha, dosDigitos(HORA_FIN) + ':00');
  var eventos = CalendarApp.getDefaultCalendar().getEvents(desde, hasta);
  var minimo = new Date(Date.now() + ANTICIPACION_MINUTOS * 60000);

  var libres = [];
  for (var h = HORA_INICIO; h < HORA_FIN; h++) {
    var hora = dosDigitos(h) + ':00';
    var inicio = aFecha(fecha, hora);
    if (inicio < minimo) continue;
    var fin = new Date(inicio.getTime() + DURACION_MINUTOS * 60000);
    var lugares = lugaresLibres(eventos, inicio, fin);
    if (lugares > 0) libres.push({ hora: hora, lugares: lugares });
  }
  return libres;
}

/**
 * Cuántas visitas más entran en [inicio, fin). Solo cuentan las visitas: los
 * demás eventos de tu calendario no bloquean. `excluirId` deja afuera una
 * visita (la que se está moviendo al reagendar).
 */
function lugaresLibres(eventos, inicio, fin, excluirId) {
  var visitas = 0;
  for (var i = 0; i < eventos.length; i++) {
    var ev = eventos[i];
    if (ev.isAllDayEvent()) continue;
    if (ev.getTitle().indexOf(PREFIJO) !== 0) continue; // no es una visita
    if (excluirId && ev.getId() === excluirId) continue;
    if (ev.getStartTime() >= fin || ev.getEndTime() <= inicio) continue; // no se pisan
    visitas++;
  }
  return Math.max(0, VISITAS_POR_HORA - visitas);
}

// ---- Agendar -----------------------------------------------------------------

function agendar(datos) {
  var nombre = limpio(datos.nombre, 80);
  var telefono = String(datos.telefono || '').replace(/[^\d+]/g, '').slice(0, 20);
  var fecha = limpio(datos.fecha, 10);
  var hora = limpio(datos.hora, 5);
  if (!nombre || !fecha || !/^\d{2}:\d{2}$/.test(hora)) {
    return { ok: false, error: 'faltan datos: nombre, fecha (AAAA-MM-DD) y hora (HH:MM)' };
  }

  var inicio = aFecha(fecha, hora);
  if (isNaN(inicio.getTime())) return { ok: false, error: 'fecha u hora inválida' };
  var fin = new Date(inicio.getTime() + DURACION_MINUTOS * 60000);

  var diaSemana = Number(Utilities.formatDate(inicio, ZONA, 'u')) % 7;
  var h = Number(hora.split(':')[0]);
  if (DIAS_DE_VISITA.indexOf(diaSemana) === -1 || h < HORA_INICIO || h >= HORA_FIN) {
    return { ok: false, error: 'fuera del horario de visitas', sugerencias: proximosLibres(fecha) };
  }
  if (inicio < new Date(Date.now() + ANTICIPACION_MINUTOS * 60000)) {
    return { ok: false, error: 'muy pronto o en el pasado', sugerencias: proximosLibres(fecha) };
  }

  // Un candado: si dos clientes piden la misma hora al mismo tiempo, que no se pasen de 2.
  var candado = LockService.getScriptLock();
  candado.waitLock(20000);
  try {
    var calendario = CalendarApp.getDefaultCalendar();
    var lugares = lugaresLibres(calendario.getEvents(inicio, fin), inicio, fin);
    if (lugares <= 0) {
      return { ok: false, error: 'ese horario ya está ocupado', sugerencias: proximosLibres(fecha) };
    }

    var proyecto = limpio(datos.proyecto, 60);
    var descripcion = [
      'Cliente: ' + nombre,
      'Teléfono: ' + (telefono || '(no disponible)'),
      'Proyecto: ' + (proyecto || '-'),
      'Forma de pago: ' + (limpio(datos.forma_de_pago, 30) || '-'),
      'Presupuesto: ' + (limpio(datos.presupuesto, 60) || '-'),
      'Notas: ' + (limpio(datos.notas, 300) || '-'),
      'Ubicación: ' + UBICACION,
      '',
      'Agendada por el agente de WhatsApp (conversación ' + limpio(datos.conversacion, 20) + ').'
    ].join('\n');

    var evento = calendario.createEvent(
      PREFIJO + ' ' + nombre + (proyecto ? ' – ' + proyecto : ''),
      inicio,
      fin,
      { description: descripcion, location: UBICACION }
    );
    evento.addPopupReminder(60);

    SpreadsheetApp.getActiveSpreadsheet().getSheets()[0].appendRow([
      Utilities.formatDate(new Date(), ZONA, 'yyyy-MM-dd HH:mm'),
      nombre,
      telefono ? "'" + telefono : '',   // el apóstrofo evita que Sheets lo convierta en número
      proyecto,
      limpio(datos.forma_de_pago, 30),
      limpio(datos.presupuesto, 60),
      fecha,
      "'" + hora,
      'Agendada',
      limpio(datos.notas, 300),
      limpio(datos.conversacion, 20),
      evento.getId()
    ]);

    return {
      ok: true,
      fecha: fecha,
      dia_semana: nombreDelDia(fecha),
      hora: hora,
      hora_fin: Utilities.formatDate(fin, ZONA, 'HH:mm'),
      ubicacion: UBICACION
    };
  } finally {
    candado.releaseLock();
  }
}

// ---- Reagendar ---------------------------------------------------------------

// Columnas de la hoja (1 = A): fecha_visita, hora_visita, estado, id_evento…
var COL = { telefono: 3, fecha: 7, hora: 8, estado: 9, conversacion: 11, evento: 12 };

function reagendar(datos) {
  var fecha = limpio(datos.fecha, 10);
  var hora = limpio(datos.hora, 5);
  if (!fecha || !/^\d{2}:\d{2}$/.test(hora)) {
    return { ok: false, error: 'faltan datos: nueva fecha (AAAA-MM-DD) y hora (HH:MM)' };
  }
  var inicio = aFecha(fecha, hora);
  if (isNaN(inicio.getTime())) return { ok: false, error: 'fecha u hora inválida' };
  var fin = new Date(inicio.getTime() + DURACION_MINUTOS * 60000);

  var diaSemana = Number(Utilities.formatDate(inicio, ZONA, 'u')) % 7;
  var h = Number(hora.split(':')[0]);
  if (DIAS_DE_VISITA.indexOf(diaSemana) === -1 || h < HORA_INICIO || h >= HORA_FIN) {
    return { ok: false, error: 'fuera del horario de visitas', sugerencias: proximosLibres(fecha) };
  }
  if (inicio < new Date(Date.now() + ANTICIPACION_MINUTOS * 60000)) {
    return { ok: false, error: 'muy pronto o en el pasado', sugerencias: proximosLibres(fecha) };
  }

  var candado = LockService.getScriptLock();
  candado.waitLock(20000);
  try {
    var hoja = SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];
    var fila = buscarVisita(hoja, String(datos.conversacion || ''), String(datos.telefono || ''));
    if (!fila) return { ok: false, error: 'no encontré una visita agendada de esta persona' };

    var calendario = CalendarApp.getDefaultCalendar();
    var evento = fila.idEvento ? calendario.getEventById(fila.idEvento) : null;
    if (!evento) return { ok: false, error: 'no encontré la visita en el calendario' };

    var lugares = lugaresLibres(calendario.getEvents(inicio, fin), inicio, fin, evento.getId());
    if (lugares <= 0) {
      return { ok: false, error: 'ese horario ya está ocupado', sugerencias: proximosLibres(fecha) };
    }

    evento.setTime(inicio, fin);
    hoja.getRange(fila.numero, COL.fecha).setValue(fecha);
    hoja.getRange(fila.numero, COL.hora).setValue("'" + hora);
    hoja.getRange(fila.numero, COL.estado).setValue('Reagendada');

    return {
      ok: true,
      antes: fila.fecha + ' ' + fila.hora,
      fecha: fecha,
      dia_semana: nombreDelDia(fecha),
      hora: hora,
      hora_fin: Utilities.formatDate(fin, ZONA, 'HH:mm'),
      ubicacion: UBICACION
    };
  } finally {
    candado.releaseLock();
  }
}

/** La última visita vigente (futura) de esa conversación, o de ese teléfono. */
function buscarVisita(hoja, conversacion, telefono) {
  var valores = hoja.getDataRange().getValues();
  var tel = telefono.replace(/[^\d]/g, '');
  var ahora = new Date();
  for (var i = valores.length - 1; i >= 1; i--) {
    var v = valores[i];
    var estado = String(v[COL.estado - 1]);
    if (estado !== 'Agendada' && estado !== 'Reagendada') continue;
    var mismaCharla = conversacion && String(v[COL.conversacion - 1]) === conversacion;
    var mismoTel = tel && String(v[COL.telefono - 1]).replace(/[^\d]/g, '') === tel;
    if (!mismaCharla && !mismoTel) continue;
    var f = textoDeFecha(v[COL.fecha - 1]);
    var hh = textoDeHora(v[COL.hora - 1]);
    if (aFecha(f, hh) < ahora) continue; // ya pasó
    return { numero: i + 1, fecha: f, hora: hh, idEvento: String(v[COL.evento - 1] || '') };
  }
  return null;
}

// Sheets puede convertir "2026-10-13" en fecha y "10:00" en hora: se normalizan.
function textoDeFecha(valor) {
  return valor instanceof Date ? Utilities.formatDate(valor, ZONA, 'yyyy-MM-dd') : String(valor).slice(0, 10);
}
function textoDeHora(valor) {
  return valor instanceof Date ? Utilities.formatDate(valor, ZONA, 'HH:mm') : String(valor).replace("'", '').slice(0, 5);
}

function proximosLibres(fecha) {
  var sugerencias = [];
  for (var d = 0; d < 7 && sugerencias.length < 4; d++) {
    var dia = sumarDias(fecha, d);
    var libres = horariosLibres(dia) || [];
    for (var i = 0; i < libres.length && sugerencias.length < 4; i++) {
      sugerencias.push(nombreDelDia(dia) + ' ' + dia + ' ' + libres[i].hora);
    }
  }
  return sugerencias;
}

// ---- Ayudantes ---------------------------------------------------------------

function aFecha(fecha, hora) {
  return new Date(fecha + 'T' + hora + ':00' + DESFASE);
}

function sumarDias(fecha, n) {
  var base = aFecha(fecha, '12:00');
  return Utilities.formatDate(new Date(base.getTime() + n * 86400000), ZONA, 'yyyy-MM-dd');
}

function nombreDelDia(fecha) {
  var nombres = ['domingo', 'lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado'];
  return nombres[Number(Utilities.formatDate(aFecha(fecha, '12:00'), ZONA, 'u')) % 7];
}

function dosDigitos(n) {
  return (n < 10 ? '0' : '') + n;
}

function limpio(valor, largo) {
  // Quita saltos de línea y fórmulas: lo que escribe un cliente no debe
  // convertirse en una fórmula de la hoja.
  var texto = String(valor == null ? '' : valor).replace(/[\r\n\t]+/g, ' ').trim();
  if (/^[=+\-@]/.test(texto)) texto = "'" + texto;
  return texto.slice(0, largo);
}

function responder(datos) {
  return ContentService
    .createTextOutput(JSON.stringify(datos))
    .setMimeType(ContentService.MimeType.JSON);
}
