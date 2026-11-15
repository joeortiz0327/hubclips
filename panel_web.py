"""Panel web local: galería de clips con streaming y subtítulos.

Solo usa la biblioteca estándar. Uso:

    python panel_web.py [--puerto 8000] [--clips RUTA]

Y abre http://127.0.0.1:8000
"""
import argparse
import gzip
import hashlib
import json
import logging
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.parse
from urllib.parse import parse_qs, unquote, urlsplit

import acceso as ACCESO_MOD
import oauth

from analisis_local import guardar_metadatos
from estado_proceso import FASES_ACTIVAS, leer_estado
from recursos import BAJA_PRIORIDAD, DATOS, ColaLlena, limpiar_antiguos
from exportar_clip import (
    AJUSTES,
    ESTILO_POR_DEFECTO,
    ESTILOS,
    escribir_vtt,
    exportaciones_activas,
    exportar,
    leer_vtt,
    lista_plantillas,
)
from lanzador_captura import Lanzador
from publicacion import (
    Publicador, destinos_publicos, guardar_destino, guardar_tokens,
    probar_destino,
)

LANZADOR = Lanzador()

# Acceso con contraseña (obligatorio fuera de localhost, ver acceso.py).
ACCESO = ACCESO_MOD.Acceso()
HOSTS_PERMITIDOS = {
    h.strip().lower()
    for h in (
        os.environ.get("HUBCLIPS_ALLOWED_HOSTS", "") + ","
        + os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")
    ).split(",")
    if h.strip()
}

BASE = Path(__file__).resolve().parent
WEB = BASE / "web"

ID_CLIP = r"clip_[0-9A-Za-z_]+"
RUTA_CLIP = re.compile(
    rf"/clips/({ID_CLIP})/(video|subtitulos\.vtt|exportar|miniatura\.jpg)"
)

# Recursos estáticos que sirve /static/ (lista cerrada: nada más).
ESTATICOS = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "favicon.svg": "image/svg+xml",
}

TROZO = 256 * 1024


# --------------------------------------------------------------------------
# Caché de la lista de clips y miniaturas
# --------------------------------------------------------------------------

_CACHE_CLIPS = {}
_CERROJO_CACHE = threading.Lock()
# Como mucho 2 FFmpeg de miniaturas a la vez, con prioridad baja: abrir una
# galería grande no debe saturar el procesador.
_SEM_MINIATURAS = threading.BoundedSemaphore(2)


def _mtime(ruta):
    try:
        return ruta.stat().st_mtime_ns
    except OSError:
        return None


def describir_cacheado(mkv):
    """describir_clip() sin releer el .json mientras no cambie nada."""
    firma = (
        _mtime(mkv), _mtime(mkv.with_suffix(".json")),
        _mtime(mkv.with_suffix(".vtt")),
    )

    if firma[0] is None:
        raise FileNotFoundError(mkv)

    clave = str(mkv)

    with _CERROJO_CACHE:
        acierto = _CACHE_CLIPS.get(clave)

    if acierto is not None and acierto[0] == firma:
        return acierto[1]

    datos = describir_clip(mkv)

    with _CERROJO_CACHE:
        _CACHE_CLIPS[clave] = (firma, datos)

    return datos


def generar_miniatura(carpeta, identificador):
    """JPG de un fotograma del clip (caché en _miniaturas/). None si falla."""
    mkv = carpeta / f"{identificador}.mkv"
    destino = carpeta / "_miniaturas" / f"{identificador}.jpg"
    reciente = lambda: (  # noqa: E731 - la miniatura es posterior al clip
        destino.is_file() and (_mtime(destino) or 0) >= (_mtime(mkv) or 0)
    )

    if reciente():
        return destino

    with _SEM_MINIATURAS:
        if reciente():  # otra petición ya la generó mientras esperábamos
            return destino

        try:
            destino.parent.mkdir(exist_ok=True)
        except OSError:
            return None

        temporal = destino.with_name(f"{identificador}.tmp.jpg")

        for instante in ("0.6", "0"):
            try:
                resultado = subprocess.run(
                    [
                        "ffmpeg", "-v", "error", "-y", "-threads", "1",
                        "-ss", instante,
                        "-i", str(mkv), "-frames:v", "1",
                        "-vf", "scale=360:-2", "-q:v", "5", str(temporal),
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=30,
                    creationflags=BAJA_PRIORIDAD,
                )
            except (OSError, subprocess.TimeoutExpired):
                break

            if (
                resultado.returncode == 0 and temporal.is_file()
                and temporal.stat().st_size > 0
            ):
                temporal.replace(destino)
                return destino

        try:
            temporal.unlink()
        except OSError:
            pass

    return None


def leer_meta(ruta_mkv):
    try:
        datos = json.loads(
            ruta_mkv.with_suffix(".json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return {}

    return datos if isinstance(datos, dict) else {}


def describir_clip(ruta_mkv):
    meta = leer_meta(ruta_mkv)
    estado = ruta_mkv.stat()

    fecha = meta.get("fecha")

    if not fecha:
        coincidencia = re.match(
            r"clip_(\d{8})_(\d{6})", ruta_mkv.stem
        )

        try:
            fecha = datetime.strptime(
                "".join(coincidencia.groups()), "%Y%m%d%H%M%S"
            ).isoformat(timespec="seconds")
        except (AttributeError, ValueError):
            fecha = datetime.fromtimestamp(
                estado.st_mtime
            ).isoformat(timespec="seconds")

    tiene_vtt = ruta_mkv.with_suffix(".vtt").is_file()

    return {
        "id": ruta_mkv.stem,
        "fecha": fecha,
        "duracion_s": meta.get("duracion_s"),
        "tamano": estado.st_size,
        "parcial": bool(meta.get("parcial")),
        "subtitulos": meta.get("subtitulos", "pendiente"),
        "tiene_vtt": tiene_vtt,
        "texto": meta.get("transcripcion", ""),
        "disparo": meta.get("disparo"),
        "viral": meta.get("viral"),
        "preferencias": meta.get("preferencias"),
    }


ETIQUETAS_CAPTURA = {
    "iniciando": "Preparando la captura",
    "resolviendo": "Conectando con el directo",
    "grabando": "Descargando directo",
    "reintentando": "Reconectando con el directo",
}


def _epoch(iso):
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


def construir_procesos(carpeta, estado_lanzador, estado=None):
    """Todo lo que se está descargando, transcribiendo o renderizando.

    Une el estado de la captura (otro proceso, vía estado_proceso.json),
    la tarea actual de la IA y las exportaciones con FFmpeg de este
    proceso. `progreso` es 0-1, o None si no se puede medir (barra animada).
    """
    estado = leer_estado() if estado is None else estado
    en_marcha = bool(estado_lanzador.get("activo"))
    captura = estado if estado and estado.get("fase") in FASES_ACTIVAS else None
    tareas = []

    if captura is None and en_marcha:
        # El proceso existe pero aún no ha escrito su primer latido.
        tareas.append({
            "id": "captura", "tipo": "captura",
            "etiqueta": "Preparando la captura",
            "detalle": "Cargando modelos y comprobando el enlace…",
            "progreso": None, "desde": None,
        })
    elif captura is not None:
        ia = captura.get("ia") or {}
        partes = [
            f"{captura.get('segmentos', 0)} segmentos analizados",
            f"{captura.get('clips_guardados', 0)} clips guardados",
        ]

        if ia.get("continuo") and ia.get("cola_segmentos"):
            partes.append(f"análisis IA en vivo ({ia['cola_segmentos']} en cola)")

        fase = captura.get("fase")
        detalle = (
            " · ".join(partes) if fase == "grabando"
            else captura.get("detalle", "")
        )

        tareas.append({
            "id": "captura", "tipo": "captura",
            "etiqueta": ETIQUETAS_CAPTURA.get(fase, "Capturando"),
            "detalle": detalle, "progreso": None,
            "desde": _epoch(captura.get("inicio")),
            "url": captura.get("url"),
        })

        clip = captura.get("clip")

        if isinstance(clip, dict):
            origen = (
                "contenido" if clip.get("motivo") == "contenido" else "audio"
            )

            if clip.get("etapa") == "ensamblando":
                tareas.append({
                    "id": f"clip:{clip.get('id')}", "tipo": "clip",
                    "etiqueta": "Renderizando clip",
                    "detalle": f"Ensamblando los segmentos "
                               f"({clip.get('total', 0):.0f} s) · detectado por {origen}",
                    "progreso": None, "desde": None,
                })
            else:
                objetivo = clip.get("objetivo") or 1
                tareas.append({
                    "id": f"clip:{clip.get('id')}", "tipo": "clip",
                    "etiqueta": "Reuniendo clip",
                    "detalle": f"Detectado por {origen} · "
                               f"{clip.get('segundos', 0):.0f} de {objetivo} s "
                               "de contexto posterior",
                    "progreso": max(0.0, min(1.0, clip.get("segundos", 0) / objetivo)),
                    "desde": None,
                })

        ia = captura.get("ia") or {}
        etiquetas_ia = {
            "subtitulando": "Generando subtítulos con IA",
            "puntuando": "Calculando Viral Score",
        }

        if ia.get("tarea") in etiquetas_ia:
            tareas.append({
                "id": f"ia:{ia.get('clip')}", "tipo": "ia",
                "etiqueta": etiquetas_ia[ia["tarea"]],
                "detalle": f"Clip {ia.get('clip')}",
                "clip": ia.get("clip"),
                "progreso": None, "desde": ia.get("desde"),
            })

        if ia.get("cola_subtitulos"):
            n = ia["cola_subtitulos"]
            tareas.append({
                "id": "cola-subtitulos", "tipo": "cola",
                "etiqueta": "En cola para subtitular",
                "detalle": f"{n} clip{'s' if n > 1 else ''} esperando a la IA",
                "progreso": 0.0, "desde": None,
            })

    for exp in exportaciones_activas():
        esperando = exp["fase"] == "esperando"
        tareas.append({
            "id": f"exp:{exp['clave']}", "tipo": "exportacion",
            "etiqueta": "En cola para renderizar" if esperando else "Renderizando clip",
            "detalle": f"Vertical 9:16 ({exp['ajuste']}) · plantilla {exp['estilo']} · {exp['clip']}",
            "clip": exp["clip"],
            "progreso": None if esperando else exp["progreso"],
            "desde": exp["inicio"],
        })

    activas = len(tareas)

    # Clips ya guardados que esperan subtítulos pero nadie los procesa.
    if captura is None:
        esperando = 0

        # Se consulta cada pocos segundos: describir_cacheado evita releer
        # el .json de cada clip mientras no cambie.
        for mkv in carpeta.glob("clip_*.mkv"):
            try:
                clip = describir_cacheado(mkv)
            except OSError:
                continue

            if not clip["tiene_vtt"] and clip["subtitulos"] == "pendiente":
                esperando += 1

        if esperando:
            tareas.append({
                "id": "pendientes", "tipo": "pendiente",
                "etiqueta": "Subtítulos pendientes",
                "detalle": f"{esperando} clip{'s' if esperando > 1 else ''} "
                           "sin subtitular. Inicia una captura para procesarlos.",
                "progreso": None, "desde": None,
            })

    return {"t": time.time(), "activas": activas, "tareas": tareas}


_CERROJO_SUBS = threading.Lock()
MAX_LINEAS_EDITADAS = 500
MAX_CARACTERES_LINEA = 400


def limpiar_texto(valor):
    """Una línea de subtítulo segura: sin saltos, '-->' ni etiquetas."""
    if not isinstance(valor, str):
        raise ValueError("Texto no válido.")

    valor = valor.replace("-->", "→").replace("<", "").replace(">", "")
    return " ".join(valor.split())[:MAX_CARACTERES_LINEA]


def guardar_subtitulos(carpeta, identificador, total, cambios):
    """Aplica correcciones de texto a los subtítulos de un clip.

    `cambios` es {indice_de_linea: texto}; una línea vacía se elimina.
    Los tiempos no se tocan. Actualiza el .vtt (que usa la exportación
    con FFmpeg), la transcripción del .json y descarta las exportaciones
    antiguas. Devuelve la lista final [(ini, fin, texto)].
    """
    mkv = carpeta / f"{identificador}.mkv"
    vtt = mkv.with_suffix(".vtt")

    if not mkv.is_file() or not vtt.is_file():
        raise FileNotFoundError(identificador)

    if (
        type(total) is not int
        or not isinstance(cambios, dict)
        or len(cambios) > MAX_LINEAS_EDITADAS
    ):
        raise ValueError("Datos no válidos.")

    with _CERROJO_SUBS:
        cues = leer_vtt(vtt)

        if total != len(cues):
            raise RuntimeError(
                "Los subtítulos cambiaron en el servidor. "
                "Vuelve a abrir el clip."
            )

        textos = [c[2] for c in cues]

        for clave, valor in cambios.items():
            try:
                indice = int(clave)
            except (TypeError, ValueError):
                raise ValueError("Línea no válida.")

            if not 0 <= indice < len(textos):
                raise ValueError("Línea fuera de rango.")

            textos[indice] = limpiar_texto(valor)

        nuevos = [
            (ini, fin, texto)
            for (ini, fin, _), texto in zip(cues, textos)
            if texto
        ]

        escribir_vtt(vtt, nuevos)

        guardar_metadatos(
            mkv,
            transcripcion=" ".join(t for _, _, t in nuevos),
            subtitulos="listo" if nuevos else "sin_voz",
            editado=True,
            editado_en=datetime.now().isoformat(timespec="seconds"),
        )

        # Las exportaciones ya hechas llevan el texto antiguo.
        for exportado in (carpeta / "_exportados").glob(
            f"{identificador}_*.mp4"
        ):
            try:
                exportado.unlink()
            except OSError:
                pass

    return nuevos


def normalizar_id(valor):
    """Acepta 'clip_X', 'clip_X.mkv' o '/clips/clip_X/...'; None si no vale."""
    if not isinstance(valor, str):
        return None

    valor = valor.strip()

    if valor.lower().endswith(".mkv"):
        valor = valor[:-4]

    return valor if re.fullmatch(ID_CLIP, valor) else None


def eliminar_clip(carpeta, identificador):
    """Borra el vídeo, sus subtítulos, metadatos y exportaciones.

    Devuelve False si el clip no existe. Lanza OSError si el vídeo
    sigue en uso por otro proceso (p. ej. mientras se transcribe).
    """
    carpeta = carpeta.resolve()
    mkv = (carpeta / f"{identificador}.mkv").resolve()

    # Defensa extra: el archivo debe estar directamente en la carpeta.
    if mkv.parent != carpeta or not mkv.is_file():
        return False

    exportados = sorted(
        (carpeta / "_exportados").glob(f"{identificador}_*.mp4")
    )

    # El vídeo primero: si falla, no queda un clip sin subtítulos.
    for intento in range(10):
        try:
            mkv.unlink()
            break
        except FileNotFoundError:
            break
        except OSError:
            if intento == 9:
                raise

            time.sleep(0.3)

    asociados = (
        mkv.with_suffix(".vtt"),
        mkv.with_suffix(".json"),
        mkv.with_suffix(".vtt.tmp"),
        mkv.with_suffix(".json.tmp"),
        carpeta / "_miniaturas" / f"{identificador}.jpg",
        *exportados,
    )

    for ruta in asociados:
        try:
            ruta.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # El vídeo ya no está; un resto suelto no debe dar error.
            logging.warning("No se pudo borrar %s", ruta.name)

    return True


def crear_manejador(carpeta_clips, publicador):
    class Manejador(BaseHTTPRequestHandler):
        server_version = "PanelClips"
        # Conexiones persistentes: la galería hace muchas peticiones
        # pequeñas (miniaturas, API) y así no abre un socket por cada una.
        # Todas las respuestas llevan Content-Length.
        protocol_version = "HTTP/1.1"
        timeout = 30  # una conexión inactiva libera su hilo

        def log_message(self, formato, *args):
            pass

        def _enviar(self, estado, datos, tipo, cache="no-store",
                    etag=None, comprimir=False):
            """Envía bytes con ETag/304 y gzip opcionales."""
            if (
                etag and estado == 200
                and self.headers.get("If-None-Match") == etag
            ):
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", cache)
                self.end_headers()
                return

            comprimido = False

            if (
                comprimir and len(datos) > 1024
                and "gzip" in (self.headers.get("Accept-Encoding") or "")
            ):
                datos = gzip.compress(datos, 5)
                comprimido = True

            self.send_response(estado)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(datos)))
            self.send_header("Cache-Control", cache)

            if comprimir:
                self.send_header("Vary", "Accept-Encoding")

            if comprimido:
                self.send_header("Content-Encoding", "gzip")

            if etag:
                self.send_header("ETag", etag)

            self.end_headers()

            if self.command != "HEAD":
                self.wfile.write(datos)

        def _texto(self, estado, cuerpo, tipo="text/plain", etag=False):
            datos = cuerpo.encode("utf-8")
            self._enviar(
                estado, datos, f"{tipo}; charset=utf-8",
                # Con ETag el navegador revalida (304) en vez de bajar todo.
                cache="no-cache" if etag else "no-store",
                etag=(
                    '"' + hashlib.md5(datos).hexdigest()[:16] + '"'
                    if etag else None
                ),
                comprimir=True,
            )

        def _estatico(self, ruta, tipo):
            """HTML/CSS/JS/iconos: ETag por fecha y tamaño, revalidación 304."""
            try:
                estado = ruta.stat()
                datos = ruta.read_bytes()
            except OSError:
                return self._error(404)

            self._enviar(
                200, datos, tipo, cache="no-cache",
                etag=f'"{estado.st_mtime_ns:x}-{estado.st_size:x}"',
                comprimir=not tipo.startswith("image/") or "svg" in tipo,
            )

        def _error(self, estado):
            self._texto(estado, HTTPStatus(estado).phrase)

        def _json(self, estado, objeto, etag=False):
            self._texto(
                estado,
                json.dumps(objeto, ensure_ascii=False),
                "application/json",
                etag=etag,
            )

        def _peticion_local(self):
            """Evita que otras webs abiertas en el navegador lancen capturas."""
            if not self.headers.get(
                "Content-Type", ""
            ).startswith("application/json"):
                return False

            origen = self.headers.get("Origin")

            return origen is None or (
                urlsplit(origen).netloc == self.headers.get("Host")
            )

        def _eliminar(self, valor):
            identificador = normalizar_id(valor)

            if identificador is None:
                return self._json(400, {"error": "Identificador no válido."})

            try:
                if not eliminar_clip(carpeta_clips, identificador):
                    return self._json(
                        404,
                        {
                            "error": "El clip no existe.",
                            "id": identificador,
                        },
                    )
            except OSError as error:
                logging.warning(
                    "No se pudo eliminar %s: %s", identificador, error
                )
                return self._json(
                    409,
                    {
                        "error": "El clip lo está usando otro proceso "
                        "(por ejemplo, se está transcribiendo). "
                        "Inténtalo de nuevo en unos segundos.",
                        "id": identificador,
                    },
                )

            self._json(200, {"ok": True, "eliminado": identificador})

        def _publicacion(self, ruta, cuerpo):
            """Crear, cancelar, reintentar o eliminar una publicación."""
            try:
                if ruta == "/api/publicaciones":
                    pub = publicador.crear(
                        cuerpo.get("clip"),
                        cuerpo.get("destino"),
                        cuerpo.get("titulo"),
                        cuerpo.get("descripcion"),
                        cuerpo.get("hashtags"),
                        cuerpo.get("modo"),
                        cuerpo.get("programado_para"),
                        cuerpo.get("estilo"),
                        cuerpo.get("ajuste"),
                    )
                    return self._json(200, {"ok": True, "publicacion": pub})

                ident = cuerpo.get("id")

                if not isinstance(ident, str) or not re.fullmatch(
                    r"[0-9a-f]{8}", ident
                ):
                    return self._json(400, {"error": "Identificador no válido."})

                if ruta == "/api/publicaciones/cancelar":
                    pub = publicador.cancelar(ident)
                elif ruta == "/api/publicaciones/reintentar":
                    pub = publicador.reintentar(ident)
                else:
                    publicador.eliminar(ident)
                    pub = None

                return self._json(200, {"ok": True, "publicacion": pub})
            except LookupError as error:
                return self._json(404, {"error": str(error.args[0])})
            except ValueError as error:
                return self._json(400, {"error": str(error)})
            except RuntimeError as error:
                return self._json(409, {"error": str(error)})
            except OSError as error:
                return self._json(
                    500, {"error": f"Error de disco: {error.strerror or error}"}
                )

        def _host_local(self):
            """True si la petición puede usar todo el panel.

            En local: solo por 127.0.0.1/localhost. Un túnel (cloudflared,
            ngrok) reenvía con su propio Host: por ahí solo se permite el
            retorno OAuth. En la nube (HUBCLIPS_PASSWORD definida) se acepta
            el host público declarado y la contraseña protege el resto.
            """
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()

            if host in ("127.0.0.1", "localhost", "[::1]"):
                return True

            return ACCESO.activo and (
                not HOSTS_PERMITIDOS or host in HOSTS_PERMITIDOS
            )

        def _ip_cliente(self):
            reenviada = (self.headers.get("X-Forwarded-For") or "").split(",")[0]
            return reenviada.strip() or self.client_address[0]

        def _autenticado(self, ruta):
            """True si puede continuar; si no, ya se envió la respuesta."""
            if not ACCESO.activo:
                return True

            if (
                ruta in ("/login", "/logout", "/healthz", "/favicon.ico",
                         "/static/favicon.svg")
                or re.fullmatch(
                    r"/auth/(youtube|tiktok|facebook)/callback", ruta
                )
            ):
                return True

            if ACCESO_MOD.cookie_de(self.headers.get("Cookie")) and (
                ACCESO.sesion_valida(
                    ACCESO_MOD.cookie_de(self.headers.get("Cookie"))
                )
            ):
                return True

            if self.command in ("GET", "HEAD") and not ruta.startswith(
                ("/api/", "/clips/", "/publicaciones/")
            ):
                self.send_response(303)
                self.send_header("Location", "/login")
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
            else:
                self._json(401, {"error": "Sesión caducada. Inicia sesión de nuevo."})

            return False

        def _cookie_sesion(self, valor, max_edad):
            seguro = (
                "; Secure"
                if (self.headers.get("X-Forwarded-Proto") or "") == "https"
                else ""
            )
            return (
                f"{ACCESO_MOD.COOKIE}={valor}; Path=/; Max-Age={max_edad}; "
                f"HttpOnly; SameSite=Lax{seguro}"
            )

        def _login(self):
            """POST /login (formulario con la contraseña)."""
            origen = self.headers.get("Origin")

            if origen is not None and (
                urlsplit(origen).netloc != self.headers.get("Host")
            ):
                return self._error(403)

            try:
                largo = min(int(self.headers.get("Content-Length") or 0), 2048)
                datos = parse_qs(self.rfile.read(largo).decode("utf-8", "replace"))
            except (ValueError, OSError):
                return self._error(400)

            ip = self._ip_cliente()

            if ACCESO.bloqueado(ip):
                return self._enviar(
                    429,
                    ACCESO_MOD.pagina_login(
                        "Demasiados intentos. Espera unos minutos."
                    ).encode("utf-8"),
                    "text/html; charset=utf-8",
                )

            if not ACCESO.comprobar((datos.get("password") or [""])[0], ip):
                time.sleep(1)  # frena la fuerza bruta
                return self._enviar(
                    401,
                    ACCESO_MOD.pagina_login("Contraseña incorrecta.").encode("utf-8"),
                    "text/html; charset=utf-8",
                )

            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                self._cookie_sesion(
                    ACCESO.nueva_sesion(), ACCESO_MOD.DURACION_SESION
                ),
            )
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def _logout(self):
            self.send_response(303)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", self._cookie_sesion("", 0))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _redirigir(self, destino):
            if destino.startswith("/") and not self._host_local():
                # Llegó por el túnel: seguir en el panel local del usuario.
                destino = (
                    f"http://127.0.0.1:{self.server.server_address[1]}"
                    + destino
                )

            self.send_response(302)
            self.send_header("Location", destino)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _oauth(self, red, paso, consulta):
            """/auth/<red>/login y /auth/<red>/callback (OAuth 2.0)."""
            puerto = self.server.server_address[1]

            try:
                if paso == "login":
                    return self._redirigir(oauth.iniciar(red, puerto))

                if consulta.get("error"):
                    raise oauth.ErrorOAuth(
                        "La plataforma canceló la conexión: "
                        f"{consulta['error'][0][:100]}"
                    )

                tokens = oauth.completar(
                    red,
                    (consulta.get("code") or [None])[0],
                    (consulta.get("state") or [None])[0],
                    puerto,
                )
                guardar_tokens(red, tokens)
                return self._redirigir(f"/?oauth=ok&red={red}")
            except (oauth.ErrorOAuth, ValueError, OSError) as error:
                logging.warning("OAuth %s: %s", red, error)
                mensaje = urllib.parse.quote(str(error)[:200])
                return self._redirigir(
                    f"/?oauth=error&red={red}&msg={mensaje}"
                )

        def _destino(self, ruta, cuerpo):
            """Guardar un destino o probarlo (credenciales solo en disco)."""
            try:
                if ruta == "/api/destinos":
                    return self._json(
                        200, {"ok": True, "destino": guardar_destino(cuerpo)}
                    )

                return self._json(
                    200,
                    {"ok": True, **probar_destino(
                        cuerpo.get("id"), cuerpo.get("modo")
                    )},
                )
            except LookupError as error:
                return self._json(404, {"error": str(error.args[0])})
            except ValueError as error:
                return self._json(400, {"error": str(error)})
            except OSError as error:
                return self._json(
                    500, {"error": f"Error de disco: {error.strerror or error}"}
                )

        def _guardar_preferencias(self, cuerpo):
            """Recuerda el estilo y el ajuste elegidos para un clip."""
            identificador = normalizar_id(cuerpo.get("id"))
            estilo = cuerpo.get("estilo")
            ajuste = cuerpo.get("ajuste")

            if identificador is None:
                return self._json(400, {"error": "Identificador no válido."})

            if estilo not in ESTILOS or ajuste not in AJUSTES:
                return self._json(
                    400, {"error": "Estilo o ajuste no válido."}
                )

            mkv = carpeta_clips / f"{identificador}.mkv"

            if not mkv.is_file():
                return self._json(404, {"error": "El clip no existe."})

            try:
                guardar_metadatos(
                    mkv, preferencias={"estilo": estilo, "ajuste": ajuste}
                )
            except OSError as error:
                return self._json(
                    500,
                    {"error": "No se pudo guardar: "
                     f"{error.strerror or error}"},
                )

            self._json(200, {"ok": True, "estilo": estilo, "ajuste": ajuste})

        def _guardar_subtitulos(self, cuerpo):
            identificador = normalizar_id(cuerpo.get("id"))

            if identificador is None:
                return self._json(400, {"error": "Identificador no válido."})

            try:
                nuevos = guardar_subtitulos(
                    carpeta_clips,
                    identificador,
                    cuerpo.get("total"),
                    cuerpo.get("cambios"),
                )
            except FileNotFoundError:
                return self._json(
                    404,
                    {"error": "El clip o sus subtítulos no existen."},
                )
            except ValueError as error:
                return self._json(400, {"error": str(error)})
            except RuntimeError as error:
                return self._json(409, {"error": str(error)})
            except OSError as error:
                logging.warning(
                    "No se pudieron guardar los subtítulos de %s: %s",
                    identificador,
                    error,
                )
                return self._json(
                    500,
                    {"error": "No se pudo escribir en disco: "
                     f"{error.strerror or error}"},
                )

            self._json(
                200,
                {
                    "ok": True,
                    "lineas": len(nuevos),
                    "texto": " ".join(t for _, _, t in nuevos),
                },
            )

        def do_DELETE(self):
            """DELETE /api/clips/<id> (equivale a POST /api/clips/eliminar)."""
            self.close_connection = True
            coincidencia = re.fullmatch(
                r"/api/clips/([^/]+)", urlsplit(self.path).path
            )

            if coincidencia is None:
                return self._error(404)

            if not self._host_local():
                return self._error(403)

            if not self._autenticado(urlsplit(self.path).path):
                return

            origen = self.headers.get("Origin")

            if origen is not None and (
                urlsplit(origen).netloc != self.headers.get("Host")
            ):
                return self._error(403)

            self._eliminar(unquote(coincidencia.group(1)))

        def do_POST(self):
            # El cuerpo puede quedar sin leer (rechazos, límites): cada POST
            # cierra la conexión para no mezclarlo con la siguiente petición.
            self.close_connection = True
            ruta = urlsplit(self.path).path

            if not self._host_local():
                return self._error(403)

            if ACCESO.activo and ruta == "/login":
                return self._login()

            if ACCESO.activo and ruta == "/logout":
                return self._logout()

            if not self._autenticado(ruta):
                return

            if ruta not in (
                "/api/captura",
                "/api/captura/detener",
                "/api/clips/eliminar",
                "/api/clips/subtitulos",
                "/api/clips/preferencias",
                "/api/publicaciones",
                "/api/publicaciones/cancelar",
                "/api/publicaciones/reintentar",
                "/api/publicaciones/eliminar",
                "/api/destinos",
                "/api/destinos/probar",
            ):
                return self._error(404)

            if not self._peticion_local():
                return self._error(403)

            try:
                largo = int(self.headers.get("Content-Length") or 0)

                # Las correcciones de subtítulos pueden ser largas.
                limite = {
                    "/api/clips/subtitulos": 262144,
                    "/api/destinos": 16384,
                }.get(ruta, 4096)
                cuerpo = json.loads(
                    self.rfile.read(min(largo, limite)) or b"{}"
                )
            except (ValueError, OSError):
                return self._json(400, {"error": "JSON no válido."})

            if not isinstance(cuerpo, dict):
                return self._json(400, {"error": "JSON no válido."})

            if ruta == "/api/captura/detener":
                LANZADOR.detener()
                return self._json(200, LANZADOR.estado())

            if ruta == "/api/clips/eliminar":
                return self._eliminar(cuerpo.get("id"))

            if ruta == "/api/clips/subtitulos":
                return self._guardar_subtitulos(cuerpo)

            if ruta == "/api/clips/preferencias":
                return self._guardar_preferencias(cuerpo)

            if ruta.startswith("/api/publicaciones"):
                return self._publicacion(ruta, cuerpo)

            if ruta.startswith("/api/destinos"):
                return self._destino(ruta, cuerpo)

            try:
                LANZADOR.iniciar(
                    cuerpo.get("url"), cuerpo.get("duracion_max")
                )
            except ValueError as error:
                return self._json(400, {"error": str(error)})
            except RuntimeError as error:
                return self._json(409, {"error": str(error)})
            except OSError as error:
                return self._json(
                    500, {"error": f"No se pudo iniciar: {error}"}
                )

            self._json(200, LANZADOR.estado())

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            ruta = urlsplit(self.path).path

            # Comprobación de salud de la plataforma (Render): sin sesión.
            if ruta == "/healthz":
                return self._texto(200, "ok")

            if not self._host_local() and not re.fullmatch(
                r"/auth/(youtube|tiktok|facebook)/callback", ruta
            ):
                return self._error(404)

            if not self._autenticado(ruta):
                return

            if ruta == "/login":
                if ACCESO.activo:
                    return self._enviar(
                        200, ACCESO_MOD.pagina_login().encode("utf-8"),
                        "text/html; charset=utf-8",
                    )

                return self._redirigir("/")

            if ruta == "/":
                return self._estatico(
                    WEB / "index.html", "text/html; charset=utf-8"
                )

            if ruta in ("/favicon.ico", "/favicon.svg"):
                return self._estatico(
                    WEB / "favicon.svg", ESTATICOS["favicon.svg"]
                )

            estatico = re.fullmatch(r"/static/([\w.-]+)", ruta)

            if estatico:
                if estatico.group(1) not in ESTATICOS:
                    return self._error(404)

                return self._estatico(
                    WEB / estatico.group(1), ESTATICOS[estatico.group(1)]
                )

            if ruta == "/api/sesion":
                return self._json(200, {"auth": ACCESO.activo})

            if ruta == "/api/clips":
                return self._listar()

            if ruta == "/api/captura":
                return self._json(200, LANZADOR.estado())

            if ruta == "/api/estilos":
                return self._json(200, lista_plantillas(), etag=True)

            if ruta == "/api/destinos":
                return self._json(
                    200, destinos_publicos(publicador.destinos())
                )

            if ruta == "/api/oauth":
                return self._json(
                    200, oauth.estado_config(self.server.server_address[1])
                )

            ruta_oauth = re.fullmatch(
                r"/auth/(youtube|tiktok|facebook)/(login|callback)", ruta
            )

            if ruta_oauth:
                return self._oauth(
                    ruta_oauth.group(1), ruta_oauth.group(2),
                    parse_qs(urlsplit(self.path).query),
                )

            if ruta == "/api/publicaciones":
                return self._json(200, publicador.registro.listar())

            archivo = re.fullmatch(
                r"/publicaciones/([0-9a-f]{8})/archivo", ruta
            )

            if archivo:
                paquete = publicador.ruta_archivo(archivo.group(1))

                if paquete is None:
                    return self._error(404)

                return self._video(paquete, "video/mp4", paquete.name)

            if ruta == "/api/procesos":
                return self._json(
                    200,
                    construir_procesos(carpeta_clips, LANZADOR.estado()),
                )

            coincidencia = RUTA_CLIP.fullmatch(ruta)

            if coincidencia is None:
                return self._error(404)

            identificador, recurso = coincidencia.groups()
            mkv = carpeta_clips / f"{identificador}.mkv"

            if recurso == "video":
                return self._video(mkv)

            if recurso == "exportar":
                return self._exportar(mkv)

            if recurso == "miniatura.jpg":
                return self._miniatura(identificador)

            return self._archivo(
                mkv.with_suffix(".vtt"), "text/vtt"
            )

        def _listar(self):
            clips = []

            vistos = set()

            for mkv in carpeta_clips.glob("clip_*.mkv"):
                try:
                    clips.append(describir_cacheado(mkv))
                    vistos.add(str(mkv))
                except OSError:
                    continue

            with _CERROJO_CACHE:  # olvida los clips ya eliminados
                for clave in [k for k in _CACHE_CLIPS if k not in vistos]:
                    del _CACHE_CLIPS[clave]

            clips.sort(key=lambda clip: clip["id"], reverse=True)

            self._json(200, clips, etag=True)

        def _miniatura(self, identificador):
            ruta = generar_miniatura(carpeta_clips, identificador)

            if ruta is None:
                return self._error(404)

            try:
                datos = ruta.read_bytes()
            except OSError:
                return self._error(404)

            # La URL lleva ?v=<tamaño del clip>: si el clip cambia, cambia
            # la URL, así que la miniatura puede cachearse un año.
            self._enviar(
                200, datos, "image/jpeg",
                cache="public, max-age=31536000, immutable",
            )

        def _archivo(self, ruta, tipo):
            try:
                datos = ruta.read_bytes()
            except OSError:
                return self._error(404)

            self.send_response(200)
            self.send_header("Content-Type", f"{tipo}; charset=utf-8")
            self.send_header("Content-Length", str(len(datos)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

            if self.command != "HEAD":
                self.wfile.write(datos)

        def _exportar(self, mkv):
            """Vídeo 9:16 con subtítulos quemados, generado con FFmpeg."""
            if not mkv.is_file():
                return self._error(404)

            consulta = parse_qs(urlsplit(self.path).query)

            # Sin parámetros se usa la preferencia guardada del clip.
            pref = leer_meta(mkv).get("preferencias")
            pref = pref if isinstance(pref, dict) else {}
            ajuste = consulta.get(
                "ajuste", [pref.get("ajuste", "adaptar")]
            )[0]
            estilo = consulta.get(
                "estilo", [pref.get("estilo", ESTILO_POR_DEFECTO)]
            )[0]

            if ajuste not in AJUSTES or estilo not in ESTILOS:
                return self._error(400)

            try:
                mp4 = exportar(mkv, ajuste, estilo)
            except ColaLlena as error:
                return self._texto(429, str(error))
            except Exception as error:
                print(f"Error al exportar {mkv.name}: {error}")
                return self._texto(
                    500, "No se pudo exportar el clip."
                )

            nombre = f"{mkv.stem}_9x16_{estilo}.mp4"
            return self._video(mp4, "video/mp4", nombre)

        def _video(self, mkv, tipo="video/x-matroska", adjunto=None):
            try:
                total = mkv.stat().st_size
            except OSError:
                return self._error(404)

            inicio, fin, estado = 0, total - 1, 200
            rango = self.headers.get("Range")

            if rango:
                m = re.fullmatch(r"bytes=(\d*)-(\d*)", rango.strip())

                if m is None or not (m[1] or m[2]):
                    return self._rango_invalido(total)

                if m[1]:
                    inicio = int(m[1])

                    if m[2]:
                        fin = min(int(m[2]), total - 1)
                else:
                    inicio = max(0, total - int(m[2]))

                if inicio >= total or inicio > fin:
                    return self._rango_invalido(total)

                estado = 206

            self.send_response(estado)
            self.send_header("Content-Type", tipo)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(fin - inicio + 1))

            if adjunto is None and "descargar" in parse_qs(
                urlsplit(self.path).query
            ):
                adjunto = mkv.name

            if adjunto:
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{adjunto}"',
                )

            if estado == 206:
                self.send_header(
                    "Content-Range", f"bytes {inicio}-{fin}/{total}"
                )

            self.end_headers()

            if self.command == "HEAD":
                return

            posicion = inicio
            restante = fin - inicio + 1

            try:
                while restante > 0:
                    # El archivo se abre solo para leer cada trozo. Si se
                    # mantuviera abierto mientras esperamos al navegador
                    # (un <video> pausado deja de leer), Windows no
                    # permitiría eliminar el clip.
                    with mkv.open("rb") as archivo:
                        archivo.seek(posicion)
                        datos = archivo.read(min(TROZO, restante))

                    if not datos:
                        break

                    self.wfile.write(datos)
                    posicion += len(datos)
                    restante -= len(datos)
            except (ConnectionError, OSError):
                # El navegador cancela peticiones al saltar en el vídeo
                # o el clip se ha eliminado mientras se enviaba. La
                # respuesta quedó a medias: la conexión no se reutiliza.
                self.close_connection = True

        def _rango_invalido(self, total):
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{total}")
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Manejador


class ServidorPanel(ThreadingHTTPServer):
    """Un hilo por conexión (daemon); ignora las desconexiones del navegador."""

    request_queue_size = 64  # ráfagas de miniaturas
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Pestañas cerradas, vídeos cancelados al saltar... no son errores.
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return

        super().handle_error(request, client_address)


def main():
    # En la nube (Render, etc.) la plataforma fija PORT y hay que escuchar en
    # 0.0.0.0. En local se mantiene 127.0.0.1:8000, que no expone el panel.
    en_nube = bool(os.environ.get("PORT"))

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        default=os.environ.get("HUBCLIPS_HOST")
        or ("0.0.0.0" if en_nube else "127.0.0.1"),
    )
    parser.add_argument(
        "--puerto", type=int, default=int(os.environ.get("PORT", 8000))
    )
    parser.add_argument(
        "--clips",
        type=Path,
        default=DATOS / "clips",
        help="Carpeta con los clips (por defecto datos/clips).",
    )
    args = parser.parse_args()

    # Fuera de localhost cualquiera con la URL llegaría al panel: se exige
    # contraseña (o un permiso explícito para no tenerla).
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        if not ACCESO.activo and os.environ.get("HUBCLIPS_ALLOW_NO_AUTH") != "1":
            raise SystemExit(
                "El panel escucha en una dirección pública (%s) y no tiene "
                "contraseña. Define HUBCLIPS_PASSWORD (mín. %s caracteres)."
                % (args.host, ACCESO_MOD.MIN_PASSWORD)
            )

        if ACCESO.activo and len(ACCESO.password) < ACCESO_MOD.MIN_PASSWORD:
            raise SystemExit(
                "HUBCLIPS_PASSWORD es demasiado corta (mín. %s caracteres)."
                % ACCESO_MOD.MIN_PASSWORD
            )

    carpeta = args.clips.resolve()
    carpeta.mkdir(parents=True, exist_ok=True)

    # Caché regenerable: exportaciones de más de 2 semanas, miniaturas de
    # más de 30 días y restos de miniaturas a medias.
    limpiar_antiguos(carpeta / "_exportados", r"clip_\w+\.mp4", 14 * 86400)
    limpiar_antiguos(carpeta / "_miniaturas", r"clip_\w+\.jpg", 30 * 86400)
    limpiar_antiguos(carpeta / "_miniaturas", r"clip_\w+\.tmp\.jpg", 3600)

    # Programador de publicaciones: exporta y entrega las programadas
    # mientras el panel esté abierto.
    publicador = Publicador(carpeta)
    publicador.iniciar()

    servidor = ServidorPanel(
        (args.host, args.puerto), crear_manejador(carpeta, publicador)
    )

    print(
        f"Panel en http://{args.host}:{args.puerto}  |  clips: {carpeta}  |  "
        f"acceso con contraseña: {'sí' if ACCESO.activo else 'no'}",
        flush=True,
    )

    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Si el panel se cierra, no dejamos una captura huérfana.
        publicador.detener()
        LANZADOR.detener()
        servidor.server_close()


if __name__ == "__main__":
    main()
