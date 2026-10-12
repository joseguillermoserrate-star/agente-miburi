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
 *  - Si en tu calendario tenés OTRO evento (que no sea una visita) en ese
 *    horario, la hora está ocupada. Los eventos de todo el día no bloquean.
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
var HORA_INICIO = 8;               // primera visita: 08:00
var HORA_FIN = 18;                 // última visita empieza a las 17:00
var VISITAS_POR_HORA = 2;
var DURACION_MINUTOS = 60;
var ANTICIPACION_MINUTOS = 120;    // no agendar con menos de 2 h de aviso
var PREFIJO = 'Visita lote:';      // así se reconocen las visitas en el calendario
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

/** Cuántas visitas más entran en [inicio, fin): 0 si está ocupado. */
function lugaresLibres(eventos, inicio, fin) {
  var visitas = 0;
  for (var i = 0; i < eventos.length; i++) {
    var ev = eventos[i];
    if (ev.isAllDayEvent()) continue;
    if (ev.getStartTime() >= fin || ev.getEndTime() <= inicio) continue; // no se pisan
    if (ev.getTitle().indexOf(PREFIJO) === 0) {
      visitas++;
    } else {
      return 0; // otro compromiso tuyo en ese horario
    }
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
      '',
      'Agendada por el agente de WhatsApp (conversación ' + limpio(datos.conversacion, 20) + ').'
    ].join('\n');

    var evento = calendario.createEvent(
      PREFIJO + ' ' + nombre + (proyecto ? ' – ' + proyecto : ''),
      inicio,
      fin,
      { description: descripcion, location: proyecto || '' }
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
      hora,
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
      hora_fin: Utilities.formatDate(fin, ZONA, 'HH:mm')
    };
  } finally {
    candado.releaseLock();
  }
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
