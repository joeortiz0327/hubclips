"""Exporta un clip en vertical 9:16 con subtítulos quemados (FFmpeg + libass).

exportar(mkv, ajuste, estilo) -> Path del .mp4 resultante (con caché).
"""
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from recursos import (  # noqa: F401 - BAJA_PRIORIDAD se reexporta
    BAJA_PRIORIDAD,
    EXPORTS_PARALELAS,
    HILOS_FFMPEG,
    MAX_EN_COLA,
    ColaLlena,
    args_hilos,
    codec_video,
    limpiar_antiguos,
    liberar_memoria,
)

AJUSTES = ("adaptar", "recorte")

ANCHO, ALTO = 1080, 1920

# Cola de renders: como mucho EXPORTS_PARALELAS FFmpeg a la vez (1 por
# defecto) y MAX_EN_COLA esperando turno; más allá se rechaza con ColaLlena.
_TURNO = threading.BoundedSemaphore(EXPORTS_PARALELAS)
_CERROJO_COLA = threading.Lock()
_EN_ESPERA = 0

# Plantillas de subtítulos. Única fuente de verdad: la web las pide en
# /api/estilos para la vista previa y FFmpeg (libass) las usa al quemar,
# así que lo que se ve en el modal es lo que se exporta.
#
# Medidas en píxeles sobre un vídeo de 1080 px de ancho (9:16).
#   fuente / fuente_alt  familia instalada en el sistema y alternativas web
#   tam                  tamaño de letra
#   negrita              negrita explícita (las fuentes "Black" ya son gruesas)
#   mayusculas           texto en mayúsculas
#   color / resalte      color del texto y de la palabra activa (karaoke)
#   contorno / grosor    contorno exterior (0 = sin contorno)
#   sombra / sombra_color  desplazamiento y color (#RRGGBB o #RRGGBBAA)
#   fondo                caja detrás del texto (#RRGGBBAA) o None
#   glow / glow_radio    resplandor de neón o None
#   palabras             máximo de palabras por línea mostrada
#   karaoke              resalta la palabra que se está diciendo
#   margen_v             distancia al borde inferior
PLANTILLAS = {
    "hormozi": {
        "nombre": "Hormozi",
        "descripcion": "Amarillo brillante con contorno negro grueso",
        "fuente": "Arial Black", "fuente_alt": "Impact, Arial",
        "tam": 96, "negrita": False, "mayusculas": True,
        "color": "#FFE600", "resalte": "#FFFFFF",
        "contorno": "#000000", "grosor": 10,
        "sombra": 4, "sombra_color": "#00000099",
        "fondo": None, "glow": None, "glow_radio": 0,
        "palabras": 3, "karaoke": True, "margen_v": 380,
    },
    "neon": {
        "nombre": "Neón verde",
        "descripcion": "Letras claras con resplandor verde fosforito",
        "fuente": "Segoe UI Black", "fuente_alt": "Arial Black, Arial",
        "tam": 88, "negrita": False, "mayusculas": True,
        "color": "#E8FFE3", "resalte": "#39FF14",
        "contorno": "#39FF14", "grosor": 3,
        "sombra": 0, "sombra_color": "#00000000",
        "fondo": None, "glow": "#39FF14", "glow_radio": 14,
        "palabras": 3, "karaoke": True, "margen_v": 380,
    },
    "blanco": {
        "nombre": "Minimalista blanco",
        "descripcion": "Texto blanco limpio con sombra suave",
        "fuente": "Segoe UI Semibold", "fuente_alt": "Segoe UI, Arial",
        "tam": 58, "negrita": False, "mayusculas": False,
        "color": "#FFFFFF", "resalte": "#FFFFFF",
        "contorno": "#000000", "grosor": 2,
        "sombra": 4, "sombra_color": "#000000AA",
        "fondo": None, "glow": None, "glow_radio": 0,
        "palabras": 8, "karaoke": False, "margen_v": 360,
    },
    "karaoke": {
        "nombre": "Karaoke",
        "descripcion": "Blanco con la palabra activa en amarillo",
        "fuente": "Arial", "fuente_alt": "Helvetica",
        "tam": 92, "negrita": True, "mayusculas": True,
        "color": "#FFFFFF", "resalte": "#FFE14D",
        "contorno": "#000000", "grosor": 7,
        "sombra": 3, "sombra_color": "#00000080",
        "fondo": None, "glow": None, "glow_radio": 0,
        "palabras": 4, "karaoke": True, "margen_v": 380,
    },
    "borde": {
        "nombre": "Grande con borde",
        "descripcion": "Letras enormes con contorno negro",
        "fuente": "Arial", "fuente_alt": "Helvetica",
        "tam": 124, "negrita": True, "mayusculas": True,
        "color": "#FFFFFF", "resalte": "#FFFFFF",
        "contorno": "#000000", "grosor": 16,
        "sombra": 5, "sombra_color": "#00000080",
        "fondo": None, "glow": None, "glow_radio": 0,
        "palabras": 4, "karaoke": False, "margen_v": 380,
    },
    "minimal": {
        "nombre": "Caja oscura",
        "descripcion": "Texto discreto sobre fondo translúcido",
        "fuente": "Arial", "fuente_alt": "Helvetica",
        "tam": 46, "negrita": False, "mayusculas": False,
        "color": "#FFFFFF", "resalte": "#FFFFFF",
        "contorno": None, "grosor": 0,
        "sombra": 0, "sombra_color": "#00000000",
        "fondo": "#00000099", "glow": None, "glow_radio": 0,
        "palabras": 12, "karaoke": False, "margen_v": 380,
    },
}

ESTILOS = tuple(PLANTILLAS)
ESTILO_POR_DEFECTO = "karaoke"


def lista_plantillas():
    """Plantillas en orden, con su id, para la API de la web."""
    return [{"id": clave, **datos} for clave, datos in PLANTILLAS.items()]


def _color_ass(hex_color, alfa_extra=None):
    """'#RRGGBB' o '#RRGGBBAA' -> '&HAABBGGRR' (en ASS 00 = opaco)."""
    h = hex_color.lstrip("#")
    rr, gg, bb = h[0:2], h[2:4], h[4:6]
    opacidad = int(h[6:8], 16) if len(h) == 8 else 255

    if alfa_extra is not None:
        opacidad = alfa_extra

    return f"&H{255 - opacidad:02X}{bb}{gg}{rr}".upper()


def _tag_color(hex_color):
    return "{\\c" + _color_ass(hex_color) + "&}"


def _segundos(texto):
    m = re.match(r"(?:(\d+):)?(\d+):(\d+)[.,](\d+)", texto.strip())

    if m is None:
        return 0.0

    horas, minutos, segs, frac = m.groups()
    return (
        int(horas or 0) * 3600
        + int(minutos) * 60
        + int(segs)
        + int(frac.ljust(3, "0")[:3]) / 1000
    )


def leer_vtt(ruta):
    """Devuelve [(inicio, fin, texto)] desde un archivo WebVTT."""
    try:
        contenido = Path(ruta).read_text(encoding="utf-8")
    except OSError:
        return []

    cues = []

    for bloque in re.split(r"\n\n+", contenido.replace("\r", "")):
        lineas = bloque.split("\n")

        for i, linea in enumerate(lineas):
            if "-->" not in linea:
                continue

            ini, fin = linea.split("-->")[:2]
            texto = " ".join(lineas[i + 1:]).strip()

            if texto:
                cues.append((
                    _segundos(ini),
                    _segundos(fin.split()[0]),
                    texto,
                ))

            break

    return cues


def _marca_vtt(segundos):
    ms = int(round(max(0.0, segundos) * 1000))
    horas, ms = divmod(ms, 3_600_000)
    minutos, ms = divmod(ms, 60_000)
    segs, ms = divmod(ms, 1000)
    return f"{horas:02d}:{minutos:02d}:{segs:02d}.{ms:03d}"


def escribir_vtt(ruta, cues):
    """Escribe [(inicio, fin, texto)] como WebVTT de forma atómica."""
    ruta = Path(ruta)
    lineas = ["WEBVTT", ""]

    for ini, fin, texto in cues:
        lineas.extend([
            f"{_marca_vtt(ini)} --> {_marca_vtt(fin)}", texto, "",
        ])

    temporal = ruta.with_suffix(".vtt.tmp")
    temporal.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    temporal.replace(ruta)


def _trocear(cues, max_palabras):
    """Divide frases largas con tiempos proporcionales a las letras."""
    trozos = []

    for ini, fin, texto in cues:
        palabras = texto.split()

        if not palabras:
            continue

        n = -(-len(palabras) // max_palabras)
        tam = -(-len(palabras) // n)
        total = sum(len(p) + 1 for p in palabras)
        t = ini

        for k in range(0, len(palabras), tam):
            grupo = palabras[k:k + tam]
            peso = sum(len(p) + 1 for p in grupo)
            dur = (fin - ini) * peso / total

            tiempos, u = [], t

            for p in grupo:
                tiempos.append(u)
                u += dur * (len(p) + 1) / peso

            trozos.append((t, t + dur, grupo, tiempos))
            t += dur

    return trozos


def _marca_ass(segundos):
    cs = int(round(max(0.0, segundos) * 100))
    horas, cs = divmod(cs, 360000)
    minutos, cs = divmod(cs, 6000)
    segs, cs = divmod(cs, 100)
    return f"{horas}:{minutos:02d}:{segs:02d}.{cs:02d}"


def _limpiar(palabra, mayusculas):
    palabra = palabra.replace("{", "(").replace("}", ")")
    palabra = palabra.replace("\\", "")
    return palabra.upper() if mayusculas else palabra


def generar_ass(cues, estilo):
    p = PLANTILLAS[estilo]

    if p["fondo"]:
        # Caja opaca/translúcida detrás del texto (BorderStyle 3).
        borde, grosor = 3, 12
        c_contorno = c_atras = _color_ass(p["fondo"])
        sombra = 0
    else:
        borde, grosor = 1, p["grosor"]
        c_contorno = _color_ass(p["contorno"] or "#000000")
        c_atras = _color_ass(p["sombra_color"])
        sombra = p["sombra"]

    fuente, tam, margen_v = p["fuente"], p["tam"], p["margen_v"]
    negrita = -1 if p["negrita"] else 0
    c_texto = _color_ass(p["color"])
    mayus, max_pal = p["mayusculas"], p["palabras"]

    cabecera = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {ANCHO}
PlayResY: {ALTO}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Estilo,{fuente},{tam},{c_texto},{c_texto},{c_contorno},{c_atras},{negrita},0,0,0,100,100,0,0,{borde},{grosor},{sombra},2,70,70,{margen_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    base = _tag_color(p["color"])
    resalte = _tag_color(p["resalte"])
    eventos = []   # (inicio, fin, texto, texto_sin_resalte)

    for ini, fin, palabras, tiempos in _trocear(cues, max_pal):
        limpias = [_limpiar(w, mayus) for w in palabras]
        plano = " ".join(limpias)

        if p["karaoke"]:
            # Un evento por palabra, con la activa resaltada.
            for i, ti in enumerate(tiempos):
                fin_i = tiempos[i + 1] if i + 1 < len(tiempos) else fin
                partes = [
                    f"{resalte}{w}{base}" if j == i else w
                    for j, w in enumerate(limpias)
                ]
                eventos.append((ti, fin_i, " ".join(partes), plano))
        else:
            eventos.append((ini, fin, plano, plano))

    lineas = []

    for a, b, texto, plano in eventos:
        if p["glow"]:
            # Capa 0: solo el contorno, ancho y desenfocado (resplandor).
            r = p["glow_radio"]
            halo = (
                f"{{\\blur{r}\\bord{r}\\1a&HFF&"
                f"\\3c{_color_ass(p['glow'])}&"
                f"\\3a&H30&}}"
            )
            lineas.append(
                f"Dialogue: 0,{_marca_ass(a)},{_marca_ass(b)},"
                f"Estilo,,0,0,0,,{halo}{plano}"
            )

        # Capa 1: el texto nítido.
        lineas.append(
            f"Dialogue: 1,{_marca_ass(a)},{_marca_ass(b)},"
            f"Estilo,,0,0,0,,{texto}"
        )

    return cabecera + "\n".join(lineas) + "\n"


def _filtro(ajuste, hay_subs):
    ass = ",ass=subs.ass" if hay_subs else ""

    if ajuste == "recorte":
        return (
            f"[0:v]scale={ANCHO}:{ALTO}:force_original_aspect_ratio=increase,"
            f"crop={ANCHO}:{ALTO},setsar=1{ass}[v]"
        )

    # Fondo: el mismo vídeo recortado, pequeño, desenfocado y ampliado.
    return (
        "[0:v]split=2[a][b];"
        "[a]scale=216:384:force_original_aspect_ratio=increase,"
        "crop=216:384,gblur=sigma=6,"
        f"scale={ANCHO}:{ALTO},setsar=1,eq=brightness=-0.15[fondo];"
        f"[b]scale={ANCHO}:{ALTO}:force_original_aspect_ratio=decrease,"
        "setsar=1[frente];"
        f"[fondo][frente]overlay=(W-w)/2:(H-h)/2{ass}[v]"
    )


# Exportaciones en curso, para la pestaña "En proceso" del panel.
_PROGRESO = {}
_CERROJO_PROGRESO = threading.Lock()


def _registrar_progreso(clave, clip, ajuste, estilo):
    with _CERROJO_PROGRESO:
        actual = _PROGRESO.get(clave)

        if actual is not None:
            # Misma exportación pedida dos veces: se comparte la entrada.
            actual["usuarios"] += 1
            return

        _PROGRESO[clave] = {
            "clave": clave, "clip": clip, "ajuste": ajuste,
            "estilo": estilo, "fase": "esperando", "progreso": None,
            "inicio": time.time(), "usuarios": 1,
        }


def _actualizar_progreso(clave, **campos):
    with _CERROJO_PROGRESO:
        if clave in _PROGRESO:
            _PROGRESO[clave].update(campos)


def _quitar_progreso(clave):
    with _CERROJO_PROGRESO:
        actual = _PROGRESO.get(clave)

        if actual is None:
            return

        actual["usuarios"] -= 1

        if actual["usuarios"] <= 0:
            del _PROGRESO[clave]


def exportaciones_activas():
    """Copia de las exportaciones en cola o renderizándose."""
    with _CERROJO_PROGRESO:
        return [
            {k: v for k, v in e.items() if k != "usuarios"}
            for e in _PROGRESO.values()
        ]


def _duracion(mkv):
    """Duración del clip en segundos (0 si no se puede medir)."""
    try:
        salida = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration", "-of",
                "default=noprint_wrappers=1:nokey=1", str(mkv),
            ],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=15, check=True, creationflags=BAJA_PRIORIDAD,
        ).stdout.strip()
        return max(0.0, float(salida))
    except (subprocess.SubprocessError, ValueError, OSError):
        return 0.0


def exportar(mkv, ajuste, estilo):
    if ajuste not in AJUSTES or estilo not in ESTILOS:
        raise ValueError("Ajuste o estilo no válido.")

    mkv = Path(mkv)
    vtt = mkv.with_suffix(".vtt")
    carpeta = mkv.parent / "_exportados"
    destino = carpeta / f"{mkv.stem}_{ajuste}_{estilo}.mp4"

    def vigente():
        if not destino.is_file() or destino.stat().st_size == 0:
            return False

        fuentes = [mkv] + ([vtt] if vtt.exists() else [])
        return all(
            destino.stat().st_mtime >= f.stat().st_mtime
            for f in fuentes
        )

    if vigente():
        return destino

    clave = f"{mkv.stem}|{ajuste}|{estilo}"
    _registrar_progreso(clave, mkv.stem, ajuste, estilo)

    try:
        return _exportar_bloqueado(
            mkv, vtt, carpeta, destino, ajuste, estilo, clave, vigente
        )
    finally:
        _quitar_progreso(clave)


@contextmanager
def _turno():
    """Espera el turno de render (cola FIFO aproximada y acotada)."""
    global _EN_ESPERA

    with _CERROJO_COLA:
        if _EN_ESPERA >= MAX_EN_COLA:
            raise ColaLlena(
                "Hay demasiadas exportaciones en cola. "
                "Inténtalo de nuevo en un momento."
            )

        _EN_ESPERA += 1

    try:
        _TURNO.acquire()
    finally:
        with _CERROJO_COLA:
            _EN_ESPERA -= 1

    try:
        yield
    finally:
        _TURNO.release()


def _exportar_bloqueado(
    mkv, vtt, carpeta, destino, ajuste, estilo, clave, vigente
):
    with _turno():
        if vigente():
            return destino

        carpeta.mkdir(exist_ok=True)

        # Restos de un render interrumpido (proceso cerrado a la fuerza).
        limpiar_antiguos(carpeta, r"tmp\w+", 3600, directorios=True)
        cues = leer_vtt(vtt) if vtt.exists() else []

        # El trabajo se hace en una carpeta temporal: FFmpeg se ejecuta
        # allí para usar nombres relativos en el filtro (evita los
        # problemas de "C:\" dentro de filtros en Windows).
        with tempfile.TemporaryDirectory(dir=carpeta) as tmp:
            tmp = Path(tmp)

            if cues:
                (tmp / "subs.ass").write_text(
                    generar_ass(cues, estilo), encoding="utf-8"
                )

            salida = tmp / "salida.mp4"

            comando = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-nostdin", "-y",
                "-progress", "pipe:1", "-nostats",
                # Hilos acotados: el render no acapara todos los núcleos.
                *args_hilos(),
                "-i", str(mkv.resolve()),
                "-filter_complex", _filtro(ajuste, bool(cues)),
                "-map", "[v]", "-map", "0:a:0?",
                *codec_video(),
                "-threads", str(HILOS_FFMPEG),   # hilos del codificador
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart",
                "salida.mp4",
            ]

            _actualizar_progreso(clave, fase="renderizando", progreso=0.0)
            duracion = _duracion(mkv)

            proceso = subprocess.Popen(
                comando,
                # Prioridad baja: el render no debe quitar CPU a la captura
                # ni al panel.
                creationflags=BAJA_PRIORIDAD,
                cwd=tmp,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            # Límite de tiempo: si FFmpeg se cuelga, se corta.
            vigilante = threading.Timer(900, proceso.kill)
            vigilante.start()
            ultimas = deque(maxlen=30)

            try:
                # Con -progress, FFmpeg escribe pares clave=valor; el
                # tiempo ya renderizado da el porcentaje real.
                for linea in proceso.stdout:
                    linea = linea.strip()

                    if linea.startswith("out_time_us=") or linea.startswith(
                        "out_time_ms="
                    ):
                        try:
                            hecho = int(linea.split("=", 1)[1]) / 1_000_000
                        except ValueError:
                            continue

                        if duracion > 0:
                            _actualizar_progreso(
                                clave,
                                progreso=max(0.0, min(0.99, hecho / duracion)),
                            )
                    elif "=" not in linea:
                        ultimas.append(linea)

                proceso.wait()
            finally:
                vigilante.cancel()

                if proceso.poll() is None:
                    proceso.kill()

            if proceso.returncode != 0 or not salida.is_file():
                logging.error("FFmpeg: %s", " | ".join(ultimas))
                raise RuntimeError("FFmpeg no pudo exportar el clip.")

            salida.replace(destino)

        return destino
