// Si la sesión caduca (panel con contraseña en la nube), vuelve al acceso.
const _fetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const r = await _fetch(...args);
  if (r.status === 401) location.href = '/login';
  return r;
};

const $ = (id) => document.getElementById(id);
const rejilla = $('rejilla'), visor = $('visor'), video = $('video');
const subs = $('subs'), marco = $('marco');

let clips = [], filtro = 'todos', firma = '', listaVista = [];
let actual = null, cues = [], trozos = [], estilo = 'karaoke';
let rafId = 0, cueMostrada = null, palabraMostrada = -1;

try { estilo = localStorage.getItem('estiloSubs') || 'karaoke'; } catch (e) {}

/* ---------- Utilidades ---------- */
const fFecha = (iso) => { const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString('es-ES', { dateStyle: 'medium', timeStyle: 'short' }); };
const fDur = (s) => s == null ? '' :
  `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}`;
const fTam = (b) => (b / 1048576).toFixed(1) + ' MB';
const listo = (c) => c.tiene_vtt;
const pendiente = (c) => !c.tiene_vtt && c.subtitulos !== 'error';

/* ---------- Biblioteca ---------- */
function etiqueta(c) {
  if (c.tiene_vtt) return c.subtitulos === 'sin_voz' ? 'Sin voz' : 'Subtítulos';
  return c.subtitulos === 'error' ? 'Error' : 'Procesando…';
}

function tarjeta(c) {
  const t = document.createElement('article');
  t.className = 'tarjeta'; t.tabIndex = 0; t.setAttribute('role', 'button');
  t.dataset.id = c.id;

  // Miniatura JPG ligera y diferida; el vídeo solo se crea al pasar el ratón.
  const img = document.createElement('img');
  img.className = 'mini'; img.loading = 'lazy'; img.decoding = 'async'; img.alt = '';
  img.src = `/clips/${c.id}/miniatura.jpg?v=${c.tamano}`;
  img.addEventListener('error', () => img.remove());
  let v = null;

  const velo = Object.assign(document.createElement('div'), { className: 'velo' });
  const ins = document.createElement('div'); ins.className = 'insignias';
  const a = Object.assign(document.createElement('span'), { className: 'insignia', textContent: fDur(c.duracion_s) || '—' });
  const b = Object.assign(document.createElement('span'), { className: 'insignia' + (c.tiene_vtt ? ' ok' : ''), textContent: etiqueta(c) });
  ins.append(a, b);

  const info = document.createElement('div'); info.className = 'info';
  const f = Object.assign(document.createElement('div'), { className: 'fecha', textContent: fFecha(c.fecha) + (c.parcial ? ' · parcial' : '') });
  const x = Object.assign(document.createElement('div'), { className: 'extracto', textContent: c.texto || fTam(c.tamano) });
  info.append(f, x);

  const p = document.createElement('div'); p.className = 'play';
  p.appendChild(document.createElement('b'));

  const del = document.createElement('button');
  del.className = 'borrar'; del.type = 'button';
  del.title = 'Eliminar clip'; del.setAttribute('aria-label', 'Eliminar clip');
  del.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14M10 11v6M14 11v6"/></svg>';
  del.addEventListener('click', (e) => { e.stopPropagation(); eliminar(c); });
  del.addEventListener('keydown', (e) => e.stopPropagation());

  const pill = document.createElement('span');
  pill.className = 'viral-pill';
  const barra = document.createElement('div');
  barra.className = 'viral-barra';
  const puntos = c.viral ? c.viral.score : null;

  if (puntos == null) {
    pill.classList.add('calculando');
    pill.textContent = c.tiene_vtt ? 'Puntuando…' : 'Viral —';
  } else {
    t.classList.add(nivel(puntos));
    pill.textContent = `🔥 ${puntos}`;
    pill.title = `Viral Score: ${puntos}/100`;
    const relleno = document.createElement('i');
    relleno.style.setProperty('--p', puntos + '%');
    barra.appendChild(relleno);
  }

  t.append(img, velo, ins, pill, info, p, del, barra);

  t.addEventListener('mouseenter', () => {
    if (!v) {
      v = document.createElement('video');
      v.muted = true; v.loop = true; v.playsInline = true; v.preload = 'metadata';
      v.src = `/clips/${c.id}/video`;
      t.insertBefore(v, velo);
    }
    v.play().catch(() => {});
  });
  t.addEventListener('mouseleave', () => {
    // Se descarta el vídeo: libera la conexión y el archivo (Windows).
    if (!v) return;
    v.pause(); v.removeAttribute('src'); v.load(); v.remove(); v = null;
  });
  t.addEventListener('click', () => abrir(c));
  t.addEventListener('keydown', (e) => { if (e.key === 'Enter') abrir(c); });
  return t;
}

// Pintado progresivo: la cuadrícula crece por lotes al acercarse al final,
// así abre al instante aunque haya cientos de clips.
const LOTE = 36;
let lotePendiente = null, cargados = LOTE;
const centinela = Object.assign(document.createElement('div'), { id: 'centinela' });
const obsLote = new IntersectionObserver((entradas) => {
  if (entradas.some((e) => e.isIntersecting)) siguienteLote();
}, { rootMargin: '700px' });

function siguienteLote(hasta) {
  if (!lotePendiente) return;
  const { lista } = lotePendiente;
  const fin = Math.min(hasta || lotePendiente.i + LOTE, lista.length);
  centinela.before(...lista.slice(lotePendiente.i, fin).map(tarjeta));
  lotePendiente.i = fin;
  cargados = Math.max(cargados, fin);
  if (fin >= lista.length) { lotePendiente = null; obsLote.disconnect(); centinela.remove(); }
}

const nivel = (p) => p >= 70 ? 'nivel-alto' : (p >= 45 ? 'nivel-medio' : 'nivel-bajo');
let orden = 'recientes';
try { orden = localStorage.getItem('orden') === 'viral' ? 'viral' : 'recientes'; } catch (e) {}
$('orden').value = orden;
$('orden').addEventListener('change', () => {
  orden = $('orden').value;
  try { localStorage.setItem('orden', orden); } catch (e) {}
  pintar();
});

function pintarStats(visible) {
  const caja = $('stats');
  caja.hidden = !visible || !clips.length;
  if (caja.hidden) return;
  const hechos = clips.filter(listo).length;
  const seg = clips.reduce((s, c) => s + (c.duracion_s || 0), 0);
  const con = clips.filter((c) => c.viral && typeof c.viral.score === 'number');
  const medio = con.length ? Math.round(con.reduce((s, c) => s + c.viral.score, 0) / con.length) : null;
  const mejor = con.reduce((m, c) => (!m || c.viral.score > m.viral.score ? c : m), null);
  $('st-total').textContent = clips.length;
  $('st-dur').textContent = seg ? `${Math.floor(seg / 60)} min ${Math.round(seg % 60)} s en total` : '';
  $('st-listo').textContent = hechos;
  $('st-pct').textContent = `${Math.round(100 * hechos / clips.length)} % de la biblioteca`;
  $('st-medio').textContent = medio == null ? '—' : medio;
  $('st-medio-n').textContent = con.length ? `sobre ${con.length} clip${con.length > 1 ? 's' : ''} puntuado${con.length > 1 ? 's' : ''}` : 'aún sin puntuar';
  $('st-mejor').textContent = mejor ? mejor.viral.score : '—';
  $('st-mejor-f').textContent = mejor ? fFecha(mejor.fecha) : '';
}

function pintar() {
  const verClips = filtro === 'todos' || filtro === 'listo';
  rejilla.hidden = !verClips;
  $('procesos').hidden = filtro !== 'proceso';
  $('auto').hidden = filtro !== 'auto';
  $('caja-orden').hidden = !verClips;
  $('n-todos').textContent = clips.length;
  $('n-listo').textContent = clips.filter(listo).length;
  pintarStats(verClips);

  if (!verClips) {
    $('vacio').hidden = true;
    return;
  }

  let lista = clips.filter((c) => filtro === 'todos' || listo(c));
  if (orden === 'viral') {
    lista = [...lista].sort((a, b) =>
      ((b.viral ? b.viral.score : -1) - (a.viral ? a.viral.score : -1)));
  }
  listaVista = lista;
  obsLote.disconnect();
  rejilla.replaceChildren(centinela);
  lotePendiente = { lista, i: 0 };
  siguienteLote(Math.max(LOTE, cargados));   // conserva lo ya desplegado
  if (lotePendiente) obsLote.observe(centinela);
  $('vacio').hidden = lista.length > 0;
}

/* Acordeón de la barra lateral (se recuerda en localStorage). */
const movilMQ = window.matchMedia('(max-width: 860px)');
let gruposCerrados = [];
try { gruposCerrados = JSON.parse(localStorage.getItem('gruposCerrados') || '[]'); } catch (e) {}
function aplicarGrupos() {
  document.querySelectorAll('.grupo-nav').forEach((g) => {
    const cerrado = gruposCerrados.includes(g.dataset.grupo);
    g.classList.toggle('cerrado', cerrado);
    g.querySelector('.grupo-cab').setAttribute('aria-expanded', String(!cerrado));
    g.classList.toggle('tiene-activo', !!g.querySelector('.nav-item.activo'));
    g.querySelector('.grupo-cuerpo').inert = cerrado && !movilMQ.matches;
  });
}
document.querySelectorAll('.grupo-cab').forEach((cab) => cab.addEventListener('click', () => {
  const id = cab.parentElement.dataset.grupo;
  gruposCerrados = gruposCerrados.includes(id) ? gruposCerrados.filter((x) => x !== id) : [...gruposCerrados, id];
  try { localStorage.setItem('gruposCerrados', JSON.stringify(gruposCerrados)); } catch (e) {}
  aplicarGrupos();
}));
movilMQ.addEventListener('change', aplicarGrupos);
aplicarGrupos();

document.querySelectorAll('.nav-item').forEach((b) => b.addEventListener('click', () => {
  filtro = b.dataset.filtro;
  setTimeout(aplicarGrupos, 0);
  document.querySelectorAll('.nav-item').forEach((x) => x.classList.toggle('activo', x === b));
  $('titulo').textContent = b.firstElementChild.textContent;
  pintar();
  if (filtro === 'proceso') consultarProcesos();
  if (filtro === 'auto') { cargarDestinos(); consultarPublicaciones(); }
}));

async function cargar() {
  try {
    const r = await fetch('/api/clips', { cache: 'no-cache' });   // revalida con ETag (304)
    const datos = await r.json();
    $('estado').textContent = '';
    const nueva = JSON.stringify(datos);
    if (nueva === firma) return;
    firma = nueva; clips = datos; pintar();
  } catch (e) {
    $('estado').textContent = 'Sin conexión con el servidor';
  }
}
cargar();
setInterval(() => { if (!visor.open) cargar(); }, 15000);

// Vuelta del inicio de sesión OAuth: /?oauth=ok|error&red=...&msg=...
window.addEventListener('load', () => {
  const q = new URLSearchParams(location.search);
  const res = q.get('oauth');
  if (!res) return;
  history.replaceState(null, '', location.pathname);
  document.querySelector('[data-filtro="auto"]').click();
  const red = { youtube: 'YouTube', tiktok: 'TikTok', facebook: 'Facebook' }[q.get('red')] || 'la red';
  if (res === 'ok') aviso(red + ' conectado correctamente');
  else aviso('No se pudo conectar ' + red + ': ' + (q.get('msg') || 'error desconocido').slice(0, 200), true);
});

/* ---------- Pestaña "En proceso" (actualización automática) ---------- */
let procesos = { activas: 0, tareas: [], t: 0 }, tickProc = 0, firmaProc = null;
const TIPOS = {
  captura: { icono: '📡', color: '#3ddc97', grupo: 'vivo' },
  clip: { icono: '✂️', color: '#8b5cff', grupo: 'clips' },
  ia: { icono: '🧠', color: '#4f8cff', grupo: 'clips' },
  exportacion: { icono: '🎞️', color: '#f59e0b', grupo: 'render' },
  cola: { icono: '⏳', color: '#8b93a7', grupo: 'espera' },
  pendiente: { icono: '⏸', color: '#8b93a7', grupo: 'espera' },
};
const GRUPOS = [
  ['vivo', 'En vivo'], ['clips', 'Clips y análisis'],
  ['render', 'Renders y exportaciones'], ['espera', 'En espera'],
];
const gruposProc = new Map();   // id de grupo -> { seccion, lista, cuenta }

function formatoTiempo(seg) {
  seg = Math.max(0, Math.round(seg));
  const h = Math.floor(seg / 3600), m = Math.floor((seg % 3600) / 60), s = seg % 60;
  const mm = String(m).padStart(2, '0'), ss = String(s).padStart(2, '0');
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function grupoDe(t) {
  // Un render que aún no empezó espera turno: va a «En espera».
  if (t.tipo === 'exportacion' && /cola/i.test(t.etiqueta)) return 'espera';
  return (TIPOS[t.tipo] || {}).grupo || 'clips';
}

function grupoProc(id) {
  let g = gruposProc.get(id);
  if (g) return g;
  const seccion = el('section', 'proc-grupo');
  const cab = el('h3', null, GRUPOS.find((x) => x[0] === id)[1]);
  const cuenta = el('span', 'proc-cuenta', '0');
  cab.append(cuenta);
  const lista = el('div', 'proc-lista');
  seccion.append(cab, lista);
  g = { seccion, lista, cuenta };
  gruposProc.set(id, g);
  return g;
}

const elementosTarea = new Map();

function crearTarea() {
  const e = el('div', 'tarea');
  const ico = el('div', 't-ico');
  const cuerpo = el('div', 't-cuerpo');
  const cab = el('div', 't-cab');
  cab.append(el('strong', 'et'), el('span', 'chip-estado'), el('span', 'ti'));
  const barra = el('div', 'barra-t');
  barra.append(document.createElement('i'));
  const fila = el('div', 'fila-barra');
  fila.append(barra, el('span', 'pct'));
  cuerpo.append(cab, el('div', 'det'), fila);
  const parar = el('button', 't-parar', 'Detener'); parar.type = 'button';
  parar.hidden = true;
  parar.addEventListener('click', async (ev) => {
    ev.stopPropagation();
    parar.disabled = true; parar.textContent = 'Deteniendo…';
    try { pintarCaptura(await pedir('/api/captura/detener', {})); }
    catch (err) { aviso(err.message, true); }
    consultarProcesos();
  });
  e.append(ico, cuerpo, parar);
  return e;
}

function actualizarTarea(e, t) {
  const tipo = TIPOS[t.tipo] || { icono: '•', color: '#8b93a7' };
  const enEspera = grupoDe(t) === 'espera';
  e.className = 'tarea tipo-' + t.tipo + (enEspera ? ' en-espera' : '');
  e.style.setProperty('--color', tipo.color);

  // Miniatura del clip si la tarea trabaja sobre uno conocido.
  const c = t.clip ? clips.find((x) => x.id === t.clip) : null;
  const ico = e.querySelector('.t-ico');
  if (c && ico.dataset.clip !== c.id) {
    ico.dataset.clip = c.id; ico.className = 't-ico con-mini';
    const img = el('img'); img.loading = 'lazy'; img.alt = '';
    img.src = `/clips/${c.id}/miniatura.jpg?v=${c.tamano}`;
    img.addEventListener('error', () => { img.remove(); ico.textContent = tipo.icono; });
    ico.replaceChildren(img);
  } else if (!c && ico.dataset.clip !== '') {
    ico.dataset.clip = ''; ico.className = 't-ico'; ico.textContent = tipo.icono;
  }
  e.classList.toggle('clicable', !!c);
  e.onclick = c ? () => abrir(c) : null;

  e.querySelector('.et').textContent = t.etiqueta;
  const chip = e.querySelector('.chip-estado');
  chip.textContent = enEspera ? (t.tipo === 'pendiente' ? 'Pendiente' : 'En cola') : 'En curso';
  e.querySelector('.det').textContent = t.detalle || '';

  const transcurrido = t.desde ? procesos.t - t.desde : null;
  const medida = typeof t.progreso === 'number';
  let tiempo = transcurrido != null ? formatoTiempo(transcurrido) : '';
  if (medida && transcurrido > 3 && t.progreso > 0.05 && t.progreso < 0.99) {
    tiempo += ` · quedan ~${formatoTiempo(transcurrido * (1 - t.progreso) / t.progreso)}`;
  }
  e.querySelector('.ti').textContent = tiempo;

  const parar = e.querySelector('.t-parar');
  parar.hidden = t.tipo !== 'captura';
  if (!parar.hidden && parar.disabled && hayCaptura) { parar.disabled = false; parar.textContent = 'Detener'; }

  const barra = e.querySelector('.barra-t'), rel = barra.firstChild, pct = e.querySelector('.pct');
  if (t.tipo === 'pendiente') { barra.hidden = true; pct.textContent = ''; return; }
  barra.hidden = false;
  if (!medida) {
    barra.classList.add('indet'); barra.classList.remove('medida');
    rel.style.width = ''; pct.textContent = '';
  } else {
    barra.classList.add('medida'); barra.classList.remove('indet');
    rel.style.width = Math.round(t.progreso * 100) + '%';
    pct.textContent = Math.round(t.progreso * 100) + '%';
  }
}

function pintarProcesos() {
  const contenedor = $('lista-procesos');
  const ids = new Set(procesos.tareas.map((t) => t.id));
  for (const [id, e] of elementosTarea) {
    if (!ids.has(id)) { e.remove(); elementosTarea.delete(id); }
  }

  // Cada tarea va a su grupo conservando el orden recibido.
  const porGrupo = new Map(GRUPOS.map(([id]) => [id, []]));
  for (const t of procesos.tareas) porGrupo.get(grupoDe(t)).push(t);

  let k = 0;
  for (const [id] of GRUPOS) {
    const tareas = porGrupo.get(id);
    const g = grupoProc(id);
    if (tareas.length && g.seccion.parentElement !== contenedor) contenedor.append(g.seccion);
    g.seccion.hidden = !tareas.length;
    g.cuenta.textContent = tareas.length;
    tareas.forEach((t, i) => {
      let e = elementosTarea.get(t.id);
      if (!e) { e = crearTarea(); elementosTarea.set(t.id, e); }
      actualizarTarea(e, t);
      if (g.lista.children[i] !== e) g.lista.insertBefore(e, g.lista.children[i] || null);
    });
    k += tareas.length;
  }
  // Orden fijo de los grupos aunque aparezcan en otro momento.
  GRUPOS.forEach(([id]) => { const g = gruposProc.get(id); if (g && g.seccion.parentElement) contenedor.append(g.seccion); });

  $('sin-procesos').hidden = k > 0;
  $('proc-stats').hidden = k === 0;
  const cola = porGrupo.get('espera').length;
  const cap = procesos.tareas.find((t) => t.tipo === 'captura');
  $('ps-act').textContent = k - cola;
  $('ps-cola').textContent = cola;
  $('ps-cap').textContent = cap && cap.desde ? formatoTiempo(procesos.t - cap.desde) : '—';
  $('ps-cap-s').textContent = cap ? cap.etiqueta : 'sin captura';

  $('n-proceso').textContent = procesos.activas;
  document.querySelector('.nav-item[data-filtro="proceso"]')
    .classList.toggle('vivo', procesos.activas > 0);
}

async function consultarProcesos() {
  try {
    const r = await fetch('/api/procesos', { cache: 'no-store' });
    if (!r.ok) throw new Error(r.status);
    procesos = await r.json();
    pintarProcesos();
    actualizarBarraCaptura();

    // Si empieza o termina algo, la lista de clips puede haber cambiado.
    const firma = procesos.tareas.map((t) => t.id).join('|');
    if (firmaProc !== null && firma !== firmaProc && !visor.open) cargar();
    firmaProc = firma;
  } catch (e) { /* sin conexión: se reintenta en el siguiente ciclo */ }
}
consultarProcesos();

// Cada 2 s mientras haya actividad o se mire la pestaña; si no, cada 6 s.
setInterval(() => {
  if (document.hidden) return;
  tickProc++;
  if (filtro === 'proceso' || procesos.activas > 0 || tickProc % 3 === 0) consultarProcesos();
  const pubActivas = publicaciones.some((p) => ['programado', 'exportando'].includes(p.estado));
  if (filtro === 'auto' || pubActivas || visor.open || tickProc % 3 === 0) consultarPublicaciones();
}, 2000);

/* ---------- Publicación y automatización ---------- */
let destinos = [], publicaciones = [], firmaPub = '', estadosPrev = null, etiquetasManual = false;
const ESTADOS_PUB = { programado: 'Programado', exportando: 'Exportando…', listo: 'Listo',
  error: 'Error', cancelado: 'Cancelado' };
const FECHA_PUB = (iso) => { const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleString('es-ES', { dateStyle: 'medium', timeStyle: 'short' }); };
const el = (tag, cls, texto) => { const e = document.createElement(tag);
  if (cls) e.className = cls; if (texto != null) e.textContent = texto; return e; };

async function cargarDestinos() {
  try {
    const r = await fetch('/api/destinos', { cache: 'no-store' });
    if (!r.ok) throw new Error(r.status);
    destinos = await r.json();
  } catch (e) { destinos = []; }
  await cargarOAuth();
  pintarDestinos(); rellenarDestinos();
}

const REDES = [
  { plat: 'youtube_shorts', oauth: 'youtube', nombre: 'YouTube Shorts', corto: 'YouTube', logo: '▶' },
  { plat: 'tiktok', oauth: 'tiktok', nombre: 'TikTok', corto: 'TikTok', logo: '♪' },
  { plat: 'facebook_reels', oauth: 'facebook', nombre: 'Facebook Reels', corto: 'Facebook', logo: 'f' },
];
const ETIQUETA_ESTADO = { desactivado: 'Desactivado', sin_configurar: 'Sin conectar',
  configurado: 'Sin probar', conectado: 'Conectado', error: 'Error' };
let configOAuth = {};

async function cargarOAuth() {
  try {
    const r = await fetch('/api/oauth', { cache: 'no-store' });
    configOAuth = r.ok ? await r.json() : {};
  } catch (e) { configOAuth = {}; }
}

function tarjetaRed(red, d) {
  const estado = d ? d.estado : 'sin_configurar';
  const conectado = !!(d && d.credenciales.access_token);
  const cfg = configOAuth[red.oauth];
  const c = el('div', 'red estado-' + estado);
  const cab = el('div', 'cab');
  cab.append(el('div', 'logo ' + red.plat, red.logo), el('strong', null, red.nombre),
    el('span', 'chip estado-' + estado, ETIQUETA_ESTADO[estado]));
  c.append(cab);

  if (conectado) {
    c.append(el('div', 'info bien', 'Conectado como ' + (d.credenciales.cuenta || red.corto)));
    c.append(el('div', 'info', d.credenciales.refresh_token
      ? 'La sesión se renueva sola.' : 'Esta sesión no se renueva sola: si caduca, vuelve a conectar.'));
  } else if (cfg && !cfg.configurado) {
    c.append(el('div', 'info mal', 'Faltan ' + cfg.faltan.join(' y ') + ' en el archivo .env. ' +
      'Añádelos y reinicia el panel.'));
  } else {
    c.append(el('div', 'info', 'Autoriza a Hubclips a publicar en tu cuenta de ' + red.corto + '.'));
  }
  if (cfg && !conectado) {
    const u = el('div', 'info', 'URL de retorno para registrar en la consola de ' + red.corto + ': ');
    u.append(el('code', null, cfg.url_retorno)); c.append(u);
  }
  if (d && d.prueba) {
    c.append(el('div', 'info ' + (d.prueba.ok ? 'bien' : 'mal'),
      `Comprobación (${FECHA_PUB(d.prueba.fecha)}): ${d.prueba.detalle}`));
  }

  const acc = el('div', 'acciones');
  const enlace = el('a', 'boton-conectar', conectado ? 'Reconectar' : 'Conectar con ' + red.corto);
  enlace.href = '/auth/' + red.oauth + '/login';
  if (cfg && !cfg.configurado) { enlace.setAttribute('aria-disabled', 'true'); enlace.removeAttribute('href'); }
  if (!conectado) enlace.classList.add('principal');
  acc.append(enlace);
  if (conectado) {
    const probar = el('button', null, 'Probar conexión'); probar.type = 'button';
    probar.addEventListener('click', () => probarRed(d, 'conexion', probar));
    const fuera = el('button', 'rojo', 'Desconectar'); fuera.type = 'button';
    fuera.addEventListener('click', async () => {
      if (!confirm(`¿Desconectar ${red.nombre}? Se borran los tokens guardados en este equipo.`)) return;
      fuera.disabled = true;
      try { await pedir('/api/destinos', { id: d.id, plataforma: red.plat, desvincular: true });
        aviso(red.nombre + ' desconectado'); }
      catch (e) { aviso(e.message, true); }
      await cargarDestinos();
    });
    acc.append(probar, fuera);
  }
  c.append(acc);
  if (conectado) {
    const sw = el('label', 'interruptor');
    const i = el('input'); i.type = 'checkbox'; i.checked = d.activo;
    i.addEventListener('change', async () => {
      try { await pedir('/api/destinos', { id: d.id, plataforma: red.plat, activo: i.checked }); }
      catch (e) { aviso(e.message, true); }
      await cargarDestinos();
    });
    sw.append(i, el('span', 'pista'), el('span', null, 'Publicar en ' + red.corto));
    c.append(sw);
  }
  return c;
}

async function probarRed(d, modo, boton) {
  boton.disabled = true; boton.textContent = 'Probando…';
  try {
    const r = await pedir('/api/destinos/probar', { id: d.id, modo });
    aviso(r.detalle, !r.ok);
  } catch (e) { aviso(e.message, true); }
  await cargarDestinos();
}

function pintarDestinos() {
  const redes = $('lista-redes');
  redes.replaceChildren(...REDES.map((r) =>
    tarjetaRed(r, destinos.find((x) => x.plataforma === r.plat))));
  const cont = $('lista-destinos');
  const otros = destinos.filter((d) => !REDES.some((r) => r.plat === d.plataforma));
  if (!otros.length) {
    cont.replaceChildren(el('p', 'sin-texto', 'No hay más destinos (se editan en destinos.json).'));
    return;
  }
  cont.replaceChildren(...otros.map((d) => {
    const c = el('div', 'destino');
    const t = el('div'); t.append(el('strong', null, d.nombre));
    if (d.webhook) t.append(el('span', 'chip', 'webhook'));
    c.append(t, el('div', 'ruta', d.carpeta));
    if (d.hashtags.length) c.append(el('div', 'ruta', d.hashtags.join(' ')));
    return c;
  }));
}

function rellenarDestinos() {
  const sel = $('pub-destino');
  let previo = sel.value;
  try { previo = previo || localStorage.getItem('pubDestino') || ''; } catch (e) {}
  sel.replaceChildren(...destinos.filter((d) => d.activo).map((d) => Object.assign(el('option'), { value: d.id, textContent: d.nombre })));
  if (destinos.some((d) => d.id === previo)) sel.value = previo;
  aplicarDestino();
}

function resumenPub() {
  const n = (plantillas[estilo] || {}).nombre || estilo;
  $('pub-resumen').textContent =
    `Paquete: vídeo 9:16 (${ajuste === 'recorte' ? 'recortado' : 'fondo difuminado'}) ` +
    `con subtítulos quemados, plantilla «${n}». Cámbialos más abajo antes de enviar.`;
}

function actualizarContadores() {
  const d = destinos.find((x) => x.id === $('pub-destino').value);
  const t = $('pub-titulo').value.length, de = $('pub-desc').value.length;
  const ct = $('pub-cnt-t'), cd = $('pub-cnt-d');
  ct.textContent = d ? `${t}/${d.titulo_max}` : '';
  cd.textContent = d ? `${de}/${d.descripcion_max}` : '';
  ct.classList.toggle('pasado', !!d && t > d.titulo_max);
  cd.classList.toggle('pasado', !!d && de > d.descripcion_max);
}

function aplicarDestino() {
  const d = destinos.find((x) => x.id === $('pub-destino').value);
  if (d) $('pub-titulo').maxLength = d.titulo_max;
  if (!etiquetasManual) $('pub-tags').value = d ? d.hashtags.join(' ') : '';
  actualizarContadores(); resumenPub();
}
$('pub-destino').addEventListener('change', aplicarDestino);
$('pub-tags').addEventListener('input', () => { etiquetasManual = true; });
$('pub-titulo').addEventListener('input', actualizarContadores);
$('pub-desc').addEventListener('input', actualizarContadores);

async function prepararPublicar(c) {
  if (!destinos.length) await cargarDestinos();
  if (actual !== c) return;
  const texto = (c.texto || '').trim();
  const primera = texto.split(/(?<=[.!?…])\s+/)[0] || '';
  $('pub-titulo').value = primera.slice(0, 80) || `Clip del ${fFecha(c.fecha)}`;
  $('pub-desc').value = texto.slice(0, 300);
  $('pub-fecha').value = '';
  etiquetasManual = false;
  mensajePub('');
  aplicarDestino(); pintarPubClip();
}

function mensajePub(texto, error = false) {
  const p = $('pub-aviso'); p.textContent = texto; p.classList.toggle('error', error);
}

async function publicar(modo) {
  if (!actual) return;
  if (hayCambios()) return mensajePub('Guarda o descarta las correcciones de subtítulos antes de publicar.', true);
  const cuerpo = {
    clip: actual.id, destino: $('pub-destino').value, titulo: $('pub-titulo').value,
    descripcion: $('pub-desc').value, hashtags: $('pub-tags').value, modo, estilo, ajuste,
  };
  if (modo === 'programar') {
    if (!$('pub-fecha').value) return mensajePub('Elige la fecha y la hora para programar la subida.', true);
    cuerpo.programado_para = $('pub-fecha').value;
  }
  const botones = [$('pub-borrador'), $('pub-programar')];
  botones.forEach((b) => { b.disabled = true; });
  try {
    await pedir('/api/publicaciones', cuerpo);
    try { localStorage.setItem('pubDestino', cuerpo.destino); } catch (e) {}
    mensajePub(modo === 'borrador'
      ? 'Enviado a borrador: se está exportando el vídeo 9:16 con subtítulos. Mira el progreso en «En proceso».'
      : `Subida programada para ${FECHA_PUB(cuerpo.programado_para)}. El panel debe seguir abierto en esa hora.`);
    await consultarPublicaciones();
  } catch (err) {
    mensajePub(err.message, true);
  } finally {
    botones.forEach((b) => { b.disabled = false; });
  }
}
$('pub-borrador').addEventListener('click', () => publicar('borrador'));
$('pub-programar').addEventListener('click', () => publicar('programar'));
$('pub-sec').addEventListener('toggle', resumenPub);

function pintarPubClip() {
  const cont = $('pub-lista');
  if (!actual) { cont.replaceChildren(); return; }
  const propias = publicaciones.filter((p) => p.clip === actual.id);
  cont.replaceChildren(...propias.slice(0, 6).map((p) => {
    const f = el('div', 'mini');
    const izq = el('span', null, `${p.destino_nombre} · ${p.programado_para && p.estado === 'programado' ? FECHA_PUB(p.programado_para) : FECHA_PUB(p.creado)}`);
    f.append(izq, el('span', 'chip estado-' + p.estado, ESTADOS_PUB[p.estado] || p.estado));
    return f;
  }));
}

async function accionPub(ruta, id, confirmacion) {
  if (confirmacion && !confirm(confirmacion)) return;
  try { await pedir(ruta, { id }); } catch (err) { aviso(err.message, true); }
  await consultarPublicaciones();
}

function pintarAuto() {
  const cont = $('lista-pub');
  $('sin-pub').hidden = publicaciones.length > 0;
  cont.replaceChildren(...publicaciones.map((p) => {
    const f = el('div', 'pub-fila');
    const l1 = el('div', 'l1');
    l1.append(el('strong', null, p.titulo), el('span', 'chip estado-' + p.estado, ESTADOS_PUB[p.estado] || p.estado),
      el('span', 'chip', p.destino_nombre));
    f.appendChild(l1);

    const cuando = p.estado === 'programado' ? `Programado para ${FECHA_PUB(p.programado_para)}`
      : p.terminado ? `${p.estado === 'listo' ? 'Entregado' : 'Terminó'} el ${FECHA_PUB(p.terminado)}`
      : `Creado el ${FECHA_PUB(p.creado)}`;
    f.appendChild(el('div', 'meta', `${cuando} · ${p.clip} · plantilla ${p.estilo}`));
    if (p.archivo) f.appendChild(el('div', 'ruta', p.archivo));
    if (p.webhook) f.appendChild(el('div', 'meta', p.webhook.ok ? 'Webhook avisado correctamente.' : `El webhook falló: ${p.webhook.error || p.webhook.estado}`));
    if (p.subida) {
      const s = el('div', 'meta', `Subido a ${p.destino_nombre}: ${p.subida.detalle || 'ok'}`);
      if (p.subida.url && /^https:\/\//.test(p.subida.url)) {
        const a = el('a', null, ' · Ver'); a.href = p.subida.url; a.target = '_blank'; a.rel = 'noopener noreferrer'; s.append(a);
      }
      f.appendChild(s);
    }
    if (p.aviso) f.appendChild(el('div', 'meta', p.aviso));
    if (p.error) f.appendChild(el('div', 'err', p.error));

    const ac = el('div', 'acciones');
    if (p.estado === 'listo') {
      const a = el('a', null, 'Descargar paquete'); a.href = `/publicaciones/${p.id}/archivo`; ac.append(a);
    }
    if (p.estado === 'programado') {
      const b = el('button', null, 'Cancelar'); b.type = 'button';
      b.addEventListener('click', () => accionPub('/api/publicaciones/cancelar', p.id)); ac.append(b);
    }
    if (p.estado === 'error' || p.estado === 'cancelado') {
      const b = el('button', null, 'Reintentar'); b.type = 'button';
      b.addEventListener('click', () => accionPub('/api/publicaciones/reintentar', p.id)); ac.append(b);
    }
    if (p.estado !== 'exportando') {
      const b = el('button', 'rojo', 'Quitar del registro'); b.type = 'button';
      b.addEventListener('click', () => accionPub('/api/publicaciones/eliminar', p.id,
        'Se quitará del registro. Los archivos ya entregados en la carpeta de destino no se borran.'));
      ac.append(b);
    }
    f.appendChild(ac);
    return f;
  }));
}

async function consultarPublicaciones() {
  try {
    const r = await fetch('/api/publicaciones', { cache: 'no-store' });
    if (!r.ok) throw new Error(r.status);
    const lista = await r.json();

    if (estadosPrev) {
      for (const p of lista) {
        const antes = estadosPrev[p.id];
        if (!antes || antes === p.estado) continue;
        if (p.estado === 'listo') aviso(`Publicación lista en «${p.destino_nombre}»: ${p.titulo}`);
        else if (p.estado === 'error') aviso(`Falló la publicación «${p.titulo}»: ${p.error}`, true);
      }
    }
    estadosPrev = Object.fromEntries(lista.map((p) => [p.id, p.estado]));
    publicaciones = lista;
    $('n-auto').textContent = lista.filter((p) => ['programado', 'exportando'].includes(p.estado)).length;

    const firma = JSON.stringify(lista);
    if (firma !== firmaPub) { firmaPub = firma; pintarAuto(); pintarPubClip(); }
  } catch (e) { /* se reintenta en el siguiente ciclo */ }
}
consultarPublicaciones();

/* ---------- Subtítulos ---------- */
function segundos(t) {
  const m = t.match(/(?:(\d+):)?(\d+):(\d+)[.,](\d+)/);
  return m ? (+(m[1] || 0)) * 3600 + (+m[2]) * 60 + (+m[3]) + (+m[4].padEnd(3, '0')) / 1000 : 0;
}

function parsearVTT(texto) {
  const salida = [];
  for (const bloque of texto.replace(/\r/g, '').split(/\n\n+/)) {
    const l = bloque.split('\n');
    const i = l.findIndex((x) => x.includes('-->'));
    if (i < 0) continue;
    const [a, b] = l[i].split('-->');
    const txt = l.slice(i + 1).join(' ').trim();
    if (txt) salida.push({ ini: segundos(a), fin: segundos(b), texto: txt });
  }
  return salida;
}

// Divide frases largas en trozos cortos con tiempos proporcionales a las letras.
function trocear(lista, maxPalabras) {
  const res = [];
  for (const c of lista) {
    const pal = c.texto.split(/\s+/);
    const n = Math.ceil(pal.length / maxPalabras);
    const tam = Math.ceil(pal.length / n);
    const total = pal.reduce((s, p) => s + p.length + 1, 0);
    let t = c.ini;
    for (let k = 0; k < pal.length; k += tam) {
      const grupo = pal.slice(k, k + tam);
      const peso = grupo.reduce((s, p) => s + p.length + 1, 0);
      const dur = (c.fin - c.ini) * peso / total;
      // Tiempos de cada palabra para el karaoke.
      let u = t;
      const tiempos = grupo.map((p) => { const ini = u; u += dur * (p.length + 1) / peso; return ini; });
      res.push({ ini: t, fin: t + dur, palabras: grupo, tiempos });
      t += dur;
    }
  }
  return res;
}

function recalcularTrozos() {
  trozos = trocear(cues, pl().palabras);
  cueMostrada = null; palabraMostrada = -1;
}

const ctxFondo = $('fondo').getContext('2d');

// Fondo difuminado: el fotograma actual recortado a 9:16 y muy pequeño.
function pintarFondo() {
  const vw = video.videoWidth, vh = video.videoHeight;
  if (video.readyState < 2 || !vw) return;
  const cw = 48, ch = 86, k = Math.max(cw / vw, ch / vh);
  const sw = cw / k, sh = ch / k;
  ctxFondo.drawImage(video, (vw - sw) / 2, (vh - sh) / 2, sw, sh, 0, 0, cw, ch);
}

function dibujar() {
  if (!marco.classList.contains('recorte')) pintarFondo();
  const t = video.currentTime;
  const c = trozos.find((x) => t >= x.ini && t < x.fin) || null;

  if (c !== cueMostrada) {
    cueMostrada = c; palabraMostrada = -1;
    subs.replaceChildren();
    if (c) {
      const cont = document.createElement('span');
      if (pl().karaoke) {
        c.palabras.forEach((p) => {
          const w = document.createElement('span');
          w.className = 'w'; w.textContent = p; cont.appendChild(w);
        });
      } else {
        cont.textContent = c.palabras.join(' ');
      }
      subs.appendChild(cont);
    }
  }

  if (c && pl().karaoke) {
    let idx = 0;
    c.tiempos.forEach((ti, i) => { if (t >= ti) idx = i; });
    if (idx !== palabraMostrada) {
      palabraMostrada = idx;
      [...subs.firstChild.children].forEach((w, i) => {
        w.className = 'w' + (i === idx ? ' on' : (i < idx ? ' hecha' : ''));
      });
    }
  }

  // Resalta la línea activa de la transcripción.
  const activa = cues.findIndex((x) => t >= x.ini && t < x.fin);
  document.querySelectorAll('.linea').forEach((l, i) => l.classList.toggle('activa', i === activa));
}

function bucle() { dibujar(); rafId = requestAnimationFrame(bucle); }

/* ---------- Plantillas de estilo (vienen del backend) ---------- */
// Plantilla mínima por si el servidor no sirve /api/estilos.
const PLANTILLA_BASE = {
  nombre: 'Básico', descripcion: '', fuente: 'Arial', fuente_alt: 'sans-serif', tam: 92,
  negrita: true, mayusculas: true, color: '#FFFFFF', resalte: '#FFE14D',
  contorno: '#000000', grosor: 7, sombra: 3, sombra_color: '#00000080',
  fondo: null, glow: null, glow_radio: 0, palabras: 4, karaoke: false,
};
let plantillas = {};
const pl = () => plantillas[estilo] || PLANTILLA_BASE;

// Convierte una plantilla en variables CSS (px de un vídeo de 1080 px -> cqw).
function aplicarVars(el, p, escala = 1) {
  const cq = (px) => (px * (100 / 1080) * escala).toFixed(3) + 'cqw';
  const sombras = [];
  if (p.sombra) sombras.push(`0 ${cq(p.sombra)} 0 ${p.sombra_color}`);
  if (p.glow) {
    sombras.push(`0 0 ${cq(p.glow_radio * 0.6)} ${p.glow}`, `0 0 ${cq(p.glow_radio * 1.4)} ${p.glow}`);
  }
  const v = {
    '--fam': `"${p.fuente}", ${p.fuente_alt || 'sans-serif'}, sans-serif`,
    '--fs': cq(p.tam),
    '--fw': p.negrita ? '800' : '400',
    '--tt': p.mayusculas ? 'uppercase' : 'none',
    '--col': p.color,
    '--res': p.resalte || p.color,
    // El contorno de libass se dibuja hacia fuera; el trazo CSS va centrado.
    '--stroke': p.contorno && p.grosor ? `${cq(2 * p.grosor)} ${p.contorno}` : '0 transparent',
    '--ts': sombras.length ? sombras.join(', ') : 'none',
    '--bg': p.fondo || 'transparent',
    '--pad': p.fondo ? `${cq(12)} ${cq(30)}` : '0',
    '--rad': p.fondo ? cq(16) : '0',
  };
  for (const [k, val] of Object.entries(v)) el.style.setProperty(k, val);
}

async function cargarPlantillas() {
  const cont = $('estilos');
  try {
    const r = await fetch('/api/estilos', { cache: 'no-cache' });
    if (!r.ok) throw new Error(r.status);
    const lista = await r.json();
    plantillas = Object.fromEntries(lista.map((p) => [p.id, p]));
    if (!plantillas[estilo]) estilo = lista.length ? lista[0].id : 'karaoke';

    cont.replaceChildren(...lista.map((p) => {
      const b = document.createElement('button');
      b.className = 'estilo'; b.type = 'button'; b.dataset.estilo = p.id;

      const nombre = document.createElement('span');
      nombre.className = 'nombre'; nombre.textContent = p.nombre;

      const lienzo = document.createElement('span'); lienzo.className = 'lienzo';
      const m = document.createElement('span'); m.className = 'muestra';
      aplicarVars(m, p, 0.6);
      m.append('Así se ');
      const em = document.createElement('em'); em.textContent = 'verá';
      m.appendChild(em);
      lienzo.appendChild(m);

      const d = document.createElement('small'); d.textContent = p.descripcion;
      b.append(nombre, lienzo, d);
      b.addEventListener('click', () => aplicarEstilo(p.id, true));
      return b;
    }));
  } catch (e) {
    cont.replaceChildren(Object.assign(document.createElement('p'), {
      className: 'sin-texto',
      textContent: 'No se pudieron cargar las plantillas. Reinicia el servidor con «python panel_web.py».',
    }));
  }
  aplicarEstilo(estilo, false);
}
const plantillasListas = cargarPlantillas();

// guardar = true cuando el usuario lo elige (se recuerda); false al abrir un clip.
function aplicarEstilo(nuevo, guardar = false) {
  estilo = plantillas[nuevo] ? nuevo : (plantillas.karaoke ? 'karaoke' : estilo);
  if (guardar) {
    try { localStorage.setItem('estiloSubs', estilo); } catch (e) {}
    guardarPreferencias();
  }
  aplicarVars(subs, pl());
  document.querySelectorAll('.estilo').forEach((b) => b.classList.toggle('activo', b.dataset.estilo === estilo));
  recalcularTrozos(); dibujar();
  resumenPub();
}

// El backend recibe estilo y ajuste de cada clip para exportar con ellos.
let temporizadorPref = 0;
function guardarPreferencias() {
  if (!actual) return;
  const clip = actual, cuerpo = { id: clip.id, estilo, ajuste };
  clearTimeout(temporizadorPref);
  temporizadorPref = setTimeout(async () => {
    try {
      await pedir('/api/clips/preferencias', cuerpo);
      clip.preferencias = { estilo: cuerpo.estilo, ajuste: cuerpo.ajuste };
    } catch (e) { /* la descarga envía el estilo elegido de todos modos */ }
  }, 400);
}

/* ---------- Captura de un directo (captura_buffer.py) ---------- */
const cajaCaptura = $('captura'), botonGenerar = $('generar');
let hayCaptura = false, mensajeError = '';

/* La captura se muestra como una tarjeta con barra de progreso: nada de
   líneas de FFmpeg ni de [INFO]. Los datos salen de /api/procesos (estado
   real de la captura) y se actualizan en el sitio, sin repintar. */
let ultimaCaptura = null, tarjetaCap = null;

function estadoBarra() {
  const t = procesos.tareas || [];
  const cap = t.find((x) => x.tipo === 'captura');
  const clip = t.find((x) => x.tipo === 'clip');
  const ia = t.find((x) => x.tipo === 'ia');
  const resumen = cap && cap.detalle && /clips guardados/.test(cap.detalle) ? cap.detalle : '';

  if (!cap) return { titulo: 'Preparando la captura…', sub: 'Cargando modelos y comprobando el enlace', pct: null };
  if (/Preparando|Conectando|Reconectando/.test(cap.etiqueta)) {
    return { titulo: cap.etiqueta + '…', sub: cap.detalle || '', pct: null };
  }
  if (clip && clip.etiqueta === 'Reuniendo clip') {
    return { titulo: 'Momento detectado: preparando el clip', sub: resumen, pct: clip.progreso };
  }
  if (clip) return { titulo: 'Renderizando el clip…', sub: resumen, pct: null };
  return {
    titulo: 'Capturando y procesando en vivo…',
    sub: [resumen, ia ? ia.etiqueta : ''].filter(Boolean).join(' · '),
    pct: null,
  };
}

function actualizarBarraCaptura() {
  if (!tarjetaCap || !ultimaCaptura || !ultimaCaptura.activo) return;
  const s = estadoBarra();
  const q = (c) => tarjetaCap.querySelector(c);
  q('.cap-titulo').textContent = s.titulo;
  q('.cap-sub').textContent = s.sub;
  const determinada = typeof s.pct === 'number';
  const barra = q('.cap-barra');
  barra.classList.toggle('indet', !determinada);
  barra.setAttribute('aria-valuenow', determinada ? String(Math.round(s.pct * 100)) : '');
  q('.cap-barra i').style.width = determinada ? Math.round(s.pct * 100) + '%' : '';
  q('.cap-pct').textContent = determinada ? Math.round(s.pct * 100) + ' %' : '';
}

function pintarCaptura(e) {
  ultimaCaptura = e;
  hayCaptura = !!(e && e.activo);
  botonGenerar.disabled = hayCaptura;
  cajaCaptura.className = '';

  if (mensajeError) {
    tarjetaCap = null;
    cajaCaptura.replaceChildren(document.createTextNode(mensajeError));
    cajaCaptura.className = 'error';
    cajaCaptura.hidden = false;
    return;
  }
  if (!e || (!e.activo && e.codigo == null)) { tarjetaCap = null; cajaCaptura.hidden = true; return; }
  cajaCaptura.hidden = false;

  if (e.activo) {
    // La tarjeta se crea una vez; después solo se actualizan sus textos.
    if (!tarjetaCap || !cajaCaptura.contains(tarjetaCap)) {
      const mk = (tag, cls, txt) => Object.assign(document.createElement(tag), { className: cls, textContent: txt || '' });
      tarjetaCap = mk('div', 'cap-card');
      const cab = mk('div', 'cap-cab');
      const textos = mk('div', 'cap-txt');
      textos.append(mk('strong', 'cap-titulo'), mk('span', 'cap-sub'));
      const b = mk('button', 'cap-parar', 'Detener'); b.type = 'button';
      b.addEventListener('click', async () => {
        b.disabled = true; b.textContent = 'Deteniendo…';
        try { pintarCaptura(await pedir('/api/captura/detener', {})); }
        catch (err) { mensajeError = err.message; pintarCaptura(null); }
      });
      cab.append(mk('span', 'punto'), textos, mk('span', 'cap-pct'), b);
      const barra = mk('div', 'cap-barra indet');
      barra.setAttribute('role', 'progressbar');
      barra.setAttribute('aria-label', 'Estado de la captura');
      barra.setAttribute('aria-valuemin', '0'); barra.setAttribute('aria-valuemax', '100');
      barra.append(document.createElement('i'));
      tarjetaCap.append(cab, barra);
      cajaCaptura.replaceChildren(tarjetaCap);
    }
    tarjetaCap.title = e.url || '';
    actualizarBarraCaptura();
    return;
  }

  tarjetaCap = null;
  const ok = e.codigo === 0;
  cajaCaptura.className = ok ? 'fin' : 'error';
  const msg = document.createElement('span');
  msg.textContent = ok ? '✓ Captura finalizada.'
    : 'La captura se detuvo por un problema. Puedes volver a intentarlo.';
  cajaCaptura.replaceChildren(msg);

  // Los detalles técnicos solo existen si algo falló, y van plegados.
  if (!ok && e.log && e.log.length) {
    const d = document.createElement('details'); d.className = 'cap-detalles';
    d.append(Object.assign(document.createElement('summary'), { textContent: 'Detalles técnicos' }),
      Object.assign(document.createElement('pre'), { textContent: e.log.join('\n') }));
    cajaCaptura.append(d);
  }
}

async function pedir(ruta, cuerpo) {
  const r = await fetch(ruta, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cuerpo),
  });
  const datos = await r.json().catch(() => ({}));
  if ((r.status === 501 || r.status === 404) && !datos.error) {
    throw new Error('El servidor no reconoce esta operación (' + ruta + '). Cierra el servidor ' +
      'actual y arráncalo con «python panel_web.py» (no con http.server ni una versión antigua).');
  }
  if (!r.ok) throw new Error(datos.error || `Error ${r.status}`);
  return datos;
}

async function consultarCaptura() {
  try {
    const r = await fetch('/api/captura', { cache: 'no-store' });
    pintarCaptura(await r.json());
  } catch (e) { /* el aviso de conexión ya lo gestiona la lista */ }
}

$('form-captura').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  mensajeError = '';
  botonGenerar.disabled = true;
  try {
    const duracion = Number($('duracion').value);
    try { localStorage.setItem('duracionMax', String(duracion)); } catch (e) {}
    pintarCaptura(await pedir('/api/captura', { url: $('url').value, duracion_max: duracion }));
  } catch (err) {
    mensajeError = err.message; pintarCaptura(null);
    botonGenerar.disabled = false;
  }
});
$('url').addEventListener('input', () => { if (mensajeError) { mensajeError = ''; consultarCaptura(); } });

consultarCaptura();
setInterval(() => { consultarCaptura(); if (hayCaptura && !visor.open) cargar(); }, 4000);

try {
  const guardada = localStorage.getItem('duracionMax');
  if (['30', '60', '90'].includes(guardada)) $('duracion').value = guardada;
} catch (e) {}

/* ---------- Eliminar clips ---------- */
async function eliminar(c) {
  if (!confirm('¿Eliminar este clip y sus subtítulos del disco?\nEsta acción no se puede deshacer.')) return;

  // Suelta las conexiones de vídeo abiertas para que el servidor pueda borrar el archivo.
  if (actual && actual.id === c.id) cerrar();
  document.querySelectorAll(`.tarjeta[data-id="${c.id}"] video`).forEach((v) => {
    v.pause(); v.removeAttribute('src'); v.load();
  });

  try {
    await pedir('/api/clips/eliminar', { id: c.id });
    aviso('Clip eliminado.');
  } catch (err) {
    aviso('No se pudo eliminar el clip: ' + err.message, true);
  }
  // Siempre se recarga la cuadrícula, para reflejar lo que hay en disco.
  firma = '';
  await cargar();
}

let temporizadorAviso = 0;
function aviso(texto, error = false) {
  const caja = $('aviso');
  // Un <dialog> modal tapa todo lo demás: el aviso se muestra dentro de él.
  const sitio = visor.open ? visor : document.body;
  if (caja.parentElement !== sitio) sitio.appendChild(caja);
  caja.textContent = texto;
  caja.className = error ? 'error' : '';
  caja.hidden = false;
  clearTimeout(temporizadorAviso);
  temporizadorAviso = setTimeout(() => { caja.hidden = true; }, error ? 9000 : 2500);
}
$('eliminar').addEventListener('click', () => { if (actual) eliminar(actual); });

/* ---------- Descarga procesada (FFmpeg en el servidor) ---------- */
const textoAviso = '9:16 con el formato y los subtítulos elegidos, quemados en el vídeo.';

$('descargar').addEventListener('click', async () => {
  if (!actual) return;
  if (hayCambios()) {
    aviso('Guarda o descarta las correcciones de subtítulos antes de descargar.', true);
    return;
  }
  const clip = actual, boton = $('descargar'), avisoDesc = $('aviso-desc');
  boton.disabled = true; boton.textContent = 'Procesando con FFmpeg…';
  avisoDesc.textContent = 'Puede tardar unos segundos; no cierres el visor.';
  try {
    const url = `/clips/${clip.id}/exportar?ajuste=${ajuste}&estilo=${estilo}`;
    const r = await fetch(url);
    if (!r.ok) throw new Error(await r.text());
    const blob = await r.blob();
    const enlace = document.createElement('a');
    enlace.href = URL.createObjectURL(blob);
    enlace.download = `${clip.id}_9x16_${estilo}.mp4`;
    document.body.appendChild(enlace); enlace.click(); enlace.remove();
    setTimeout(() => URL.revokeObjectURL(enlace.href), 10000);
    avisoDesc.textContent = 'Descarga lista.';
  } catch (e) {
    avisoDesc.textContent = 'No se pudo exportar: ' + e.message;
  } finally {
    boton.disabled = false; boton.textContent = '⬇ Descargar clip';
  }
});

/* ---------- Formato 9:16 ---------- */
let ajuste = 'adaptar';
try { ajuste = localStorage.getItem('ajuste916') || 'adaptar'; } catch (e) {}

function aplicarAjuste(nuevo, guardar = false) {
  ajuste = nuevo === 'recorte' ? 'recorte' : 'adaptar';
  if (guardar) {
    try { localStorage.setItem('ajuste916', ajuste); } catch (e) {}
    guardarPreferencias();
  }
  marco.classList.toggle('recorte', ajuste === 'recorte');
  document.querySelectorAll('#ajustes button').forEach((b) =>
    b.classList.toggle('activo', b.dataset.ajuste === ajuste));
  resumenPub();
}
document.querySelectorAll('#ajustes button').forEach((b) =>
  b.addEventListener('click', () => aplicarAjuste(b.dataset.ajuste, true)));
aplicarAjuste(ajuste);

/* ---------- Viral Score en el visor ---------- */
function pintarViral(c) {
  const panel = $('viral-panel');
  panel.replaceChildren();
  panel.className = '';
  const h = document.createElement('h4'); h.textContent = 'Viral Score';
  panel.appendChild(h);
  const v = c.viral;

  if (!v) {
    const p = document.createElement('p');
    p.className = 'sin-texto';
    p.textContent = c.tiene_vtt ? 'Calculando puntuación…' : 'Disponible tras generar los subtítulos.';
    panel.appendChild(p);
    return;
  }
  panel.classList.add(nivel(v.score));

  const grande = document.createElement('div'); grande.className = 'grande';
  const n = document.createElement('span'); n.className = 'numero'; n.textContent = v.score;
  const de = document.createElement('span'); de.className = 'de'; de.textContent = '/ 100';
  grande.append(n, de);

  const barra = document.createElement('div'); barra.className = 'barra';
  const rel = document.createElement('i'); rel.style.setProperty('--p', v.score + '%');
  barra.appendChild(rel);
  panel.append(grande, barra);

  for (const [clave, etiqueta] of [['gancho', 'Gancho'], ['retencion', 'Retención'], ['claridad', 'Claridad']]) {
    const f = document.createElement('div'); f.className = 'sub-barra';
    const a = document.createElement('span'); a.textContent = etiqueta;
    const b = document.createElement('div'); b.className = 'b';
    const i = document.createElement('i'); i.style.setProperty('--p', (v[clave] || 0) + '%');
    b.appendChild(i);
    const z = document.createElement('span'); z.textContent = v[clave] ?? '—';
    f.append(a, b, z); panel.appendChild(f);
  }

  const extra = [];
  if (v.tema) extra.push(['Tema', v.tema]);
  if (v.motivo) extra.push(['Por qué', v.motivo]);
  extra.push(['Origen', (c.disparo === 'contenido' ? 'detectado por contenido' :
    c.disparo === 'audio' ? 'detectado por audio' : 'clip manual') +
    (v.metodo === 'heuristico+llm' ? ' · análisis IA' : ' · estimación local')]);
  for (const [k, val] of extra) {
    const d = document.createElement('div'); d.className = 'tema';
    const b = document.createElement('b'); b.textContent = k + ': ';
    d.append(b, document.createTextNode(val)); panel.appendChild(d);
  }
}

/* ---------- Visor ---------- */
async function abrir(c) {
  await plantillasListas;
  actual = c; cues = []; trozos = [];
  // Cada clip recuerda su plantilla y su ajuste 9:16; si no tiene, los últimos usados.
  const pref = c.preferencias || {};
  let estiloGlobal = 'karaoke', ajusteGlobal = 'adaptar';
  try {
    estiloGlobal = localStorage.getItem('estiloSubs') || estiloGlobal;
    ajusteGlobal = localStorage.getItem('ajuste916') || ajusteGlobal;
  } catch (e) {}
  aplicarAjuste(pref.ajuste || ajusteGlobal);
  estilo = pref.estilo || estiloGlobal;
  pintarViral(c);
  prepararPublicar(c);
  $('original').href = `/clips/${c.id}/video?descargar=1`;
  $('original').setAttribute('download', c.id + '.mkv');
  $('aviso-desc').textContent = textoAviso;
  $('v-titulo').textContent = fFecha(c.fecha);
  $('v-sub').textContent = [fDur(c.duracion_s), fTam(c.tamano), c.parcial ? 'parcial' : ''].filter(Boolean).join(' · ');
  pintarCabeceraVisor(c);
  if (!visor.open) activarTab('resumen');
  $('lineas').replaceChildren(Object.assign(document.createElement('p'),
    { className: 'sin-texto', textContent: c.tiene_vtt ? 'Cargando…' : 'Subtítulos aún no disponibles.' }));
  subs.replaceChildren();
  video.src = `/clips/${c.id}/video`;
  aplicarEstilo(estilo);
  if (!visor.open) visor.showModal();
  video.play().catch(() => {});
  cancelAnimationFrame(rafId); bucle();

  if (!c.tiene_vtt) return;
  try {
    const r = await fetch(`/clips/${c.id}/subtitulos.vtt`);
    const lista = parsearVTT(await r.text());
    if (actual !== c) return;
    cues = lista; recalcularTrozos();
    cues.forEach((q) => { q.orig = q.texto; });
    pintarLineas();
  } catch (e) {
    $('lineas').replaceChildren(Object.assign(document.createElement('p'),
      { className: 'sin-texto', textContent: 'No se pudieron cargar los subtítulos.' }));
  }
}

/* ---------- Transcripción editable ---------- */
const limpiarLinea = (t) => t.replace(/\s+/g, ' ').trim();
const cambiadas = () => cues.filter((q) => limpiarLinea(q.texto) !== q.orig);
const hayCambios = () => cambiadas().length > 0;

function pintarLineas() {
  const cont = $('lineas');
  $('ayuda-edicion').hidden = !cues.length;
  if (!cues.length) {
    cont.replaceChildren(Object.assign(document.createElement('p'),
      { className: 'sin-texto', textContent: 'No se detectó voz en este clip.' }));
    actualizarBarra();
    return;
  }
  cont.replaceChildren(...cues.map((q) => {
    const fila = document.createElement('div'); fila.className = 'linea';

    const t = document.createElement('button');
    t.className = 't'; t.type = 'button'; t.textContent = fDur(q.ini);
    t.title = 'Ir a este momento';
    t.addEventListener('click', () => { video.currentTime = q.ini + 0.01; video.play().catch(() => {}); });

    const x = document.createElement('span');
    x.className = 'x'; x.textContent = q.texto; x.spellcheck = true; x.lang = 'es';
    x.contentEditable = 'plaintext-only';
    if (x.contentEditable !== 'plaintext-only') x.contentEditable = 'true';
    x.title = 'Clic para editar';

    const marcar = () => {
      const nuevo = limpiarLinea(q.texto);
      fila.classList.toggle('cambiada', nuevo !== q.orig);
      fila.classList.toggle('vacia', nuevo === '');
      actualizarBarra();
    };

    x.addEventListener('focus', () => video.pause());
    x.addEventListener('input', () => { q.texto = x.textContent; marcar(); });
    x.addEventListener('paste', (e) => {
      // Solo texto plano, sin saltos de línea.
      e.preventDefault();
      const txt = (e.clipboardData.getData('text/plain') || '').replace(/\s+/g, ' ');
      document.execCommand('insertText', false, txt);
    });
    x.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); x.blur(); }
      else if (e.key === 'Escape') {
        e.preventDefault(); e.stopPropagation();   // no cierra el modal
        q.texto = q.orig; x.textContent = q.orig; marcar(); x.blur();
      }
    });
    x.addEventListener('blur', () => { recalcularTrozos(); dibujar(); });

    fila.append(t, x);
    marcar();
    return fila;
  }));
  actualizarBarra();
}

function actualizarBarra() {
  const n = cambiadas().length;
  $('barra-subs').hidden = n === 0;
  $('nota-subs').textContent = n
    ? `${n} línea${n > 1 ? 's' : ''} modificada${n > 1 ? 's' : ''}. ` +
      'Al guardar se actualizan los subtítulos y las próximas descargas.'
    : '';
}

async function guardarSubtitulos() {
  if (!actual || !hayCambios()) return;
  const clip = actual, boton = $('guardar-subs');
  const cambios = {};
  cues.forEach((q, i) => { if (limpiarLinea(q.texto) !== q.orig) cambios[i] = limpiarLinea(q.texto); });

  boton.disabled = true; boton.textContent = 'Guardando…';
  try {
    const r = await pedir('/api/clips/subtitulos', { id: clip.id, total: cues.length, cambios });
    if (actual !== clip) return;
    // Lo guardado pasa a ser la referencia; las líneas vacías desaparecen.
    cues = cues.filter((q) => limpiarLinea(q.texto) !== '');
    cues.forEach((q) => { q.texto = limpiarLinea(q.texto); q.orig = q.texto; });
    recalcularTrozos(); pintarLineas(); dibujar();
    clip.texto = r.texto;
    aviso('Subtítulos guardados. Las descargas usarán el texto corregido.');
    firma = '';           // refresca el extracto de la tarjeta
    cargar();
  } catch (err) {
    aviso('No se pudieron guardar los subtítulos: ' + err.message, true);
  } finally {
    boton.disabled = false; boton.textContent = 'Guardar cambios';
  }
}
$('guardar-subs').addEventListener('click', guardarSubtitulos);
$('descartar-subs').addEventListener('click', () => {
  cues.forEach((q) => { q.texto = q.orig; });
  recalcularTrozos(); pintarLineas(); dibujar();
});

/* ---------- Pestañas, chips y navegación del visor ---------- */
function activarTab(nombre) {
  document.querySelector('.lateral').dataset.tab = nombre;
  document.querySelectorAll('.tabs button').forEach((b) =>
    b.setAttribute('aria-selected', String(b.dataset.tab === nombre)));
  document.querySelector('.lateral-cuerpo').scrollTop = 0;
}
document.querySelectorAll('.tabs button').forEach((b) =>
  b.addEventListener('click', () => activarTab(b.dataset.tab)));

function pintarCabeceraVisor(c) {
  const chips = $('v-chips'), nodos = [];
  const chip = (texto, cls) => { const s = el('span', 'chip-v ' + (cls || ''), texto); nodos.push(s); return s; };
  if (c.viral && typeof c.viral.score === 'number') {
    const n = c.viral.score;
    chip(`🔥 Viral ${n}`, 'viral ' + (n >= 70 ? 'nivel-alto' : n >= 45 ? 'nivel-medio' : 'nivel-bajo'));
  }
  chip(c.tiene_vtt ? 'Subtitulado' : 'Sin subtítulos', c.tiene_vtt ? 'ok' : 'aviso');
  if (c.parcial) chip('Parcial', 'aviso');
  chips.replaceChildren(...nodos);

  const i = listaVista.findIndex((x) => x.id === c.id);
  $('posicion').textContent = i < 0 ? '' : `${i + 1} / ${listaVista.length}`;
  const hayAnt = i > 0, haySig = i >= 0 && i < listaVista.length - 1;
  for (const id of ['prev', 'prev2']) $(id).disabled = !hayAnt;
  for (const id of ['sig', 'sig2']) $(id).disabled = !haySig;
}

function navegar(delta) {
  if (!actual) return;
  const i = listaVista.findIndex((x) => x.id === actual.id);
  const destino = i < 0 ? null : listaVista[i + delta];
  if (!destino) return;
  if (hayCambios() && !confirm('Tienes correcciones de subtítulos sin guardar. ¿Cambiar de clip y perderlas?')) return;
  abrir(destino);
}
for (const id of ['prev', 'prev2']) $(id).addEventListener('click', () => navegar(-1));
for (const id of ['sig', 'sig2']) $(id).addEventListener('click', () => navegar(1));
visor.addEventListener('keydown', (e) => {
  if (e.altKey || e.ctrlKey || e.metaKey || (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight')) return;
  const t = e.target;
  // Las flechas siguen sirviendo para mover el cursor o el vídeo.
  if (t.closest && t.closest('input, textarea, select, video, [contenteditable]')) return;
  e.preventDefault();
  navegar(e.key === 'ArrowLeft' ? -1 : 1);
});

function cerrar() {
  cancelAnimationFrame(rafId);
  video.pause(); video.removeAttribute('src'); video.load();
  actual = null; cues = []; trozos = [];
  if (visor.open) visor.close();
}
// Cierre iniciado por el usuario: avisa si hay correcciones sin guardar.
function intentarCerrar() {
  if (hayCambios() && !confirm('Tienes correcciones de subtítulos sin guardar. ¿Cerrar y perderlas?')) return;
  cerrar();
}
$('cerrar').addEventListener('click', intentarCerrar);
visor.addEventListener('close', cerrar);
visor.addEventListener('cancel', (e) => {            // tecla Esc
  if (hayCambios() && !confirm('Tienes correcciones de subtítulos sin guardar. ¿Cerrar y perderlas?')) e.preventDefault();
});
visor.addEventListener('click', (e) => { if (e.target === visor) intentarCerrar(); });
$('pantalla').addEventListener('click', () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.querySelector('.escena').requestFullscreen().catch(() => {});
});

// Con contraseña (nube) aparece «Cerrar sesión» en el pie de la barra lateral.
fetch('/api/sesion').then((r) => r.json()).then((s) => {
  if (!s.auth) return;
  const f = document.createElement('form');
  f.method = 'post'; f.action = '/logout'; f.className = 'salir';
  const b = document.createElement('button'); b.type = 'submit'; b.textContent = 'Cerrar sesión';
  f.append(b);
  document.querySelector('.pie').append(f);
}).catch(() => {});

// «Pegar una URL» en el estado vacío de «En proceso»: vuelve a la biblioteca.
$('ir-url').addEventListener('click', () => {
  document.querySelector('.nav-item[data-filtro="todos"]').click();
  $('url').focus();
});
