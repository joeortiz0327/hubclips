import argparse
import logging
import re
import shutil
import signal
import subprocess
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import yt_dlp
from analisis_local import AnalizadorLocal, guardar_metadatos
from estado_proceso import EstadoProceso
from recursos import BAJA_PRIORIDAD, DATOS, limpiar_antiguos

# Desactivar las subidas automáticas durante la prueba local.
SUBIR_CLIPS = False

URL ="https://www.youtube.com/watch?v=Q7vkfFh7VVg"

BASE = Path(__file__).resolve().parent

BUFFER = DATOS / "buffer"
LOGS = DATOS / "logs"
CLIPS = DATOS / "clips"
TEMPORALES = CLIPS / "_temporales"

SEGUNDOS_POR_SEGMENTO = 6

# Aproximadamente cuatro minutos.
# La duración exacta depende de los fotogramas clave.
SEGMENTOS_A_CONSERVAR = 40

ESPACIO_MINIMO = 2 * 1024**3  # 2 GiB libres

# Configuración del detector.
UMBRAL_MEDIA = -22
UMBRAL_PICO = -5

# Configuración de los clips.
SEGUNDOS_ANTES = 20
SEGUNDOS_DESPUES = 20

# None = sin tope adicional. Lo fija configurar_duracion().
DURACION_MAXIMA = None
PAUSA_ENTRE_DETECCIONES = 60


class NoEsDirecto(RuntimeError):
    """El enlace no corresponde a un directo activo."""


def resolver_directo():
    opciones = {
        "format": "bestvideo+bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "remote_components": {"ejs:github"},
    }

    with yt_dlp.YoutubeDL(opciones) as ydl:
        info = ydl.extract_info(URL, download=False)

    if info.get("is_live") is not True:
        raise NoEsDirecto(
            "El enlace no es un directo activo. "
            f"Estado: {info.get('live_status', 'desconocido')}"
        )

    def obtener_formatos(datos):
        separados = datos.get("requested_formats")

        if separados:
            return separados

        descargas = datos.get("requested_downloads")

        if descargas:
            resultado = []

            for descarga in descargas:
                resultado.extend(obtener_formatos(descarga))

            return resultado

        return [datos]

    formatos = obtener_formatos(info)

    video = next(
        (
            formato
            for formato in formatos
            if formato.get("url")
            and formato.get("vcodec") not in (None, "none")
        ),
        None,
    )

    audio = next(
        (
            formato
            for formato in formatos
            if formato.get("url")
            and (
                formato.get("acodec") not in (None, "none")
                or (
                    formato.get("vcodec") == "none"
                    and formato.get("acodec") is None
                )
            )
        ),
        None,
    )

    if video is None or audio is None:
        resumen = [
            {
                "id": formato.get("format_id"),
                "video": formato.get("vcodec"),
                "audio": formato.get("acodec"),
                "tiene_url": bool(formato.get("url")),
            }
            for formato in formatos
        ]

        raise RuntimeError(
            "No se pudieron identificar ambas pistas. "
            f"Formatos seleccionados: {resumen}"
        )

    logging.info(
        "Vídeo seleccionado: formato %s | %sx%s | %s",
        video.get("format_id"),
        video.get("width"),
        video.get("height"),
        video.get("vcodec"),
    )

    logging.info(
        "Audio seleccionado: formato %s | %s",
        audio.get("format_id"),
        audio.get("acodec") or "desconocido",
    )

    fuentes = [video]
    indice_audio = 0

    if audio["url"] != video["url"]:
        fuentes.append(audio)
        indice_audio = 1

    return info, fuentes, indice_audio


def construir_comando(carpeta):
    info, fuentes, indice_audio = resolver_directo()

    comando = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-nostdin",
        "-y",
    ]

    for fuente in fuentes:
        cabeceras = dict(info.get("http_headers") or {})
        cabeceras.update(fuente.get("http_headers") or {})

        if cabeceras:
            texto = "".join(
                f"{clave}: {valor}\r\n"
                for clave, valor in cabeceras.items()
            )

            comando.extend(["-headers", texto])

        comando.extend([
            "-rw_timeout", "20000000",
            "-i", fuente["url"],
        ])

    comando.extend([
        "-map", "0:v:0",
        "-map", f"{indice_audio}:a:0",
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(SEGUNDOS_POR_SEGMENTO),
        "-segment_format", "matroska",
        "-reset_timestamps", "1",
        str(carpeta / "segmento_%09d.mkv"),
    ])

    return comando


def limpiar_buffer():
    archivos = sorted(
        BUFFER.glob("*/segmento_*.mkv"),
        key=lambda archivo: (
            archivo.parent.name,
            archivo.name,
        ),
    )

    # El último segmento puede seguir abierto.
    sobrantes = len(archivos) - (SEGMENTOS_A_CONSERVAR + 1)

    for archivo in archivos[:max(0, sobrantes)]:
        try:
            archivo.unlink()
        except OSError:
            logging.warning(
                "No se pudo eliminar %s",
                archivo.name,
            )


def analizar_audio(archivo):
    comando = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel", "info",
        "-threads", "1",
        "-i", str(archivo),
        "-map", "0:a:0",
        "-vn",
        "-af", "volumedetect",
        "-f", "null",
        "-",
    ]

    try:
        resultado = subprocess.run(
            comando,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            # Medición auxiliar: prioridad baja, un solo hilo.
            creationflags=BAJA_PRIORIDAD,
        )
    except subprocess.TimeoutExpired:
        logging.warning(
            "El análisis de audio tardó demasiado: %s",
            archivo.name,
        )
        return False

    if resultado.returncode != 0:
        logging.warning(
            "No se pudo analizar el audio de %s: %s",
            archivo.name,
            resultado.stderr[-500:],
        )
        return False

    patron = r"(-?\d+(?:\.\d+)?|-inf)"

    media = re.search(
        rf"mean_volume:\s*{patron}\s*dB",
        resultado.stderr,
    )

    pico = re.search(
        rf"max_volume:\s*{patron}\s*dB",
        resultado.stderr,
    )

    if media is None or pico is None:
        logging.warning(
            "No se encontraron mediciones en %s",
            archivo.name,
        )
        return False

    volumen_medio = float(media.group(1))
    volumen_pico = float(pico.group(1))

    logging.info(
        "Audio %s | Media: %.1f dBFS | Pico: %.1f dBFS",
        archivo.name,
        volumen_medio,
        volumen_pico,
    )

    return (
        volumen_medio >= UMBRAL_MEDIA
        and volumen_pico >= UMBRAL_PICO
    )


def obtener_duracion(archivo):
    comando = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(archivo),
    ]

    try:
        resultado = subprocess.run(
            comando,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=True,
            creationflags=BAJA_PRIORIDAD,
        )

        duracion = float(resultado.stdout.strip())

        if not 0 < duracion < float("inf"):
            raise ValueError("Duración no válida")

        return duracion

    except (
        subprocess.SubprocessError,
        ValueError,
    ):
        logging.warning(
            "No se pudo medir la duración de %s. "
            "Se estiman %s segundos.",
            archivo.name,
            SEGUNDOS_POR_SEGMENTO,
        )

        return float(SEGUNDOS_POR_SEGMENTO)


class GestorClips:
    def __init__(self, analizador, estado=None):
        self.analizador = analizador
        self.estado = estado
        self.guardados = 0
        self.historial = deque()
        self.duracion_historial = 0.0
        self.pendiente = None

        # Se conserva también entre reconexiones del directo.
        self.ultima_deteccion = float("-inf")

    def _publicar_estado(self, pendiente, etapa):
        """Informa del clip en preparación a la pestaña "En proceso"."""
        if self.estado is None:
            return

        if pendiente is None:
            self.estado.actualizar(clip=None)
            return

        self.estado.actualizar(clip={
            "id": pendiente["id"],
            "motivo": pendiente["motivo"],
            "etapa": etapa,
            "segundos": round(pendiente["segundos_posteriores"], 1),
            "objetivo": SEGUNDOS_DESPUES,
            "total": round(pendiente["duracion_total"], 1),
        })

    def iniciar_sesion(self):
        self.historial.clear()
        self.duracion_historial = 0.0

    def copiar_segmento(self, archivo, duracion):
        pendiente = self.pendiente

        indice = len(pendiente["segmentos"])
        destino = pendiente["carpeta"] / f"parte_{indice:05d}.mkv"

        shutil.copy2(archivo, destino)

        pendiente["segmentos"].append(destino)
        pendiente["duracion_total"] += duracion

    def iniciar_clip(self, archivo, duracion, motivo="audio"):
        identificador = datetime.now().strftime(
            "%Y%m%d_%H%M%S_%f"
        )

        carpeta = TEMPORALES / identificador
        carpeta.mkdir(parents=True, exist_ok=False)

        self.pendiente = {
            "id": identificador,
            "carpeta": carpeta,
            "segmentos": [],
            "duracion_total": 0.0,
            "segundos_posteriores": 0.0,
            "motivo": motivo,
        }

        # Elegimos segmentos anteriores desde el más reciente
        # hasta reunir al menos SEGUNDOS_ANTES.
        anteriores = []
        segundos_anteriores = 0.0

        for ruta, segundos in reversed(self.historial):
            if segundos_anteriores >= SEGUNDOS_ANTES:
                break

            anteriores.append((ruta, segundos))
            segundos_anteriores += segundos

        for ruta, segundos in reversed(anteriores):
            self.copiar_segmento(ruta, segundos)

        # Añadimos el segmento que activó la detección.
        self.copiar_segmento(archivo, duracion)

        self.ultima_deteccion = time.monotonic()

        logging.info(
            "MOMENTO %s: %s | "
            "Clip iniciado con %.1f s anteriores "
            "+ %.1f s del segmento detectado.",
            "INTERESANTE (contenido)"
            if motivo == "contenido"
            else "INTENSO (audio)",
            archivo.name,
            segundos_anteriores,
            duracion,
        )

        logging.info(
            "Esperando al menos %s segundos posteriores...",
            SEGUNDOS_DESPUES,
        )

        self._publicar_estado(self.pendiente, "reuniendo")

    def procesar(self, archivo, motivo):
        """motivo: "audio", "contenido" (IA) o None si no hay disparo."""
        intenso = motivo is not None
        duracion = obtener_duracion(archivo)

        # Si hay un clip pendiente, este segmento pertenece
        # al periodo posterior a su detección.
        if self.pendiente is not None:
            self.copiar_segmento(archivo, duracion)
            self.pendiente["segundos_posteriores"] += duracion
            self._publicar_estado(self.pendiente, "reuniendo")

            if (
                self.pendiente["segundos_posteriores"]
                >= SEGUNDOS_DESPUES
            ):
                self.finalizar_clip()

        if intenso:
            transcurrido = (
                time.monotonic() - self.ultima_deteccion
            )

            if self.pendiente is not None:
                logging.info(
                    "Detección incluida en el clip "
                    "que ya se está preparando."
                )

            elif transcurrido < PAUSA_ENTRE_DETECCIONES:
                logging.info(
                    "Detección omitida por pausa: "
                    "faltan %.0f segundos.",
                    PAUSA_ENTRE_DETECCIONES - transcurrido,
                )

            else:
                self.iniciar_clip(archivo, duracion, motivo)

        # Guardamos referencias al contexto reciente.
        self.historial.append((archivo, duracion))
        self.duracion_historial += duracion

        # Conservamos suficientes segmentos completos
        # para cubrir el contexto anterior.
        while (
            len(self.historial) > 1
            and (
                self.duracion_historial
                - self.historial[0][1]
            ) >= SEGUNDOS_ANTES
        ):
            _, segundos = self.historial.popleft()
            self.duracion_historial -= segundos

    def finalizar_clip(self, parcial=False):
        if self.pendiente is None:
            return

        pendiente = self.pendiente
        self.pendiente = None
        self._publicar_estado(pendiente, "ensamblando")

        carpeta = pendiente["carpeta"]
        lista = carpeta / "lista.txt"

        sufijo = "_parcial" if parcial else ""
        nombre = f"clip_{pendiente['id']}{sufijo}"

        destino = CLIPS / f"{nombre}.mkv"
        temporal_salida = carpeta / "salida.mkv"

        try:
            # Nombres relativos y controlados, sin problemas
            # con espacios o barras de las rutas de Windows.
            with lista.open(
                "w",
                encoding="utf-8",
                newline="\n",
            ) as salida:
                for segmento in pendiente["segmentos"]:
                    salida.write(
                        f"file '{segmento.name}'\n"
                    )

            comando = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "warning",
                "-nostdin",
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(lista),
                "-map", "0:v:0",
                "-map", "0:a:0",
                "-c", "copy",
                # Tope duro por si los segmentos dan más de lo previsto.
                *(
                    ["-t", str(DURACION_MAXIMA)]
                    if DURACION_MAXIMA
                    else []
                ),
                str(temporal_salida),
            ]

            resultado = subprocess.run(
                comando,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                creationflags=BAJA_PRIORIDAD,
            )

            if resultado.returncode != 0:
                raise RuntimeError(
                    resultado.stderr[-2000:]
                )

            if (
                not temporal_salida.exists()
                or temporal_salida.stat().st_size == 0
            ):
                raise RuntimeError(
                    "FFmpeg no generó un clip válido."
                )

            temporal_salida.replace(destino)

            logging.info(
                "CLIP GUARDADO%s: %s | "
                "Duración aproximada: %.1f segundos",
                " (parcial)" if parcial else "",
                destino,
                pendiente["duracion_total"],
            )

        except Exception:
            logging.exception(
                "No se pudo generar el clip. "
                "Los segmentos se conservan en: %s",
                carpeta,
            )

        else:
            shutil.rmtree(carpeta, ignore_errors=True)

            # Metadatos junto al clip y subtítulos en segundo plano.
            try:
                guardar_metadatos(
                    destino,
                    id=destino.stem,
                    archivo=destino.name,
                    fecha=datetime.now().isoformat(
                        timespec="seconds"
                    ),
                    duracion_s=round(
                        pendiente["duracion_total"], 1
                    ),
                    parcial=parcial,
                    disparo=pendiente["motivo"],
                    subtitulos="pendiente",
                )
                self.analizador.subtitular(destino)
            except Exception:
                logging.exception(
                    "No se pudieron guardar los metadatos de %s",
                    destino.name,
                )

            self.guardados += 1

        finally:
            if self.estado is not None:
                self.estado.actualizar(
                    clip=None, clips_guardados=self.guardados
                )


def detener(proceso):
    if proceso is None or proceso.poll() is not None:
        return

    proceso.terminate()

    try:
        proceso.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proceso.kill()
        proceso.wait()


def capturar(gestor, analizador, estado=None):
    def fase(nombre, detalle=""):
        if estado is not None:
            estado.actualizar(fase=nombre, detalle=detalle)

    sesion = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    carpeta = BUFFER / sesion
    carpeta.mkdir(parents=True)

    ruta_log = LOGS / f"ffmpeg_{sesion}.log"

    gestor.iniciar_sesion()

    logging.info("Resolviendo el directo...")
    fase("resolviendo", "Resolviendo el directo con yt-dlp…")
    comando = construir_comando(carpeta)

    proceso = None

    with ruta_log.open("w", encoding="utf-8") as salida:
        try:
            proceso = subprocess.Popen(
                comando,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=salida,
            )

            logging.info("Grabando en: %s", carpeta)
            logging.info("Registro de FFmpeg: %s", ruta_log)
            fase("grabando", "Grabando el directo en segmentos")

            ultimo_nombre = None
            ultimo_analizado = ""
            ultima_actividad = time.monotonic()

            while proceso.poll() is None:
                if shutil.disk_usage(DATOS).free < ESPACIO_MINIMO:
                    raise RuntimeError(
                        "Quedan menos de 2 GiB libres."
                    )

                archivos = sorted(
                    carpeta.glob("segmento_*.mkv")
                )

                if archivos:
                    nombre = archivos[-1].name

                    if nombre != ultimo_nombre:
                        ultimo_nombre = nombre
                        ultima_actividad = time.monotonic()

                        logging.info(
                            "Nuevo segmento: %s",
                            nombre,
                        )

                if time.monotonic() - ultima_actividad > 120:
                    raise RuntimeError(
                        "No aparecen nuevos segmentos "
                        "desde hace 120 segundos."
                    )

                # El último archivo puede seguir abierto.
                for archivo in archivos[:-1]:
                    if archivo.name <= ultimo_analizado:
                        continue

                    intenso = analizar_audio(archivo)

                    # Análisis semántico en segundo plano: puede pedir
                    # un clip aunque el volumen no haya subido.
                    analizador.agregar_segmento(archivo)
                    semantico = analizador.consumir_disparo()

                    gestor.procesar(
                        archivo,
                        "audio" if intenso
                        else ("contenido" if semantico else None),
                    )

                    ultimo_analizado = archivo.name

                    if estado is not None:
                        estado.incrementar("segmentos")

                limpiar_buffer()
                time.sleep(2)

            raise RuntimeError(
                f"FFmpeg terminó con código "
                f"{proceso.returncode}. Revisa {ruta_log}"
            )

        finally:
            detener(proceso)

            # Si se interrumpe la captura, guardamos lo
            # reunido hasta ahora, aunque falte contexto posterior.
            if gestor.pendiente is not None:
                logging.warning(
                    "Captura interrumpida con un clip pendiente. "
                    "Se guardará como parcial."
                )

                gestor.finalizar_clip(parcial=True)


def configurar_duracion(maximo):
    """Limita la duración total de cada clip a `maximo` segundos.

    Reparte el margen a partes iguales antes y después de la detección.
    Se reservan 3 segmentos (el detectado y el redondeo hacia arriba de
    cada lado) para que el resultado no supere el máximo.
    """
    global SEGUNDOS_ANTES, SEGUNDOS_DESPUES, DURACION_MAXIMA

    contexto = max(3, (maximo - 3 * SEGUNDOS_POR_SEGMENTO) // 2)

    SEGUNDOS_ANTES = contexto
    SEGUNDOS_DESPUES = contexto
    DURACION_MAXIMA = maximo


def main():
    global URL

    parser = argparse.ArgumentParser(
        description="Monitoriza un directo de YouTube y guarda clips."
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=URL,
        help="URL del directo (por defecto, la definida en URL).",
    )
    parser.add_argument(
        "--duracion-max",
        type=int,
        metavar="SEGUNDOS",
        help="Duración máxima de cada clip, entre 24 y 600 segundos "
        "(por defecto, 20 s antes + 20 s después).",
    )
    args = parser.parse_args()
    URL = args.url

    if args.duracion_max is not None:
        if not 24 <= args.duracion_max <= 600:
            parser.error("--duracion-max debe estar entre 24 y 600.")

        configurar_duracion(args.duracion_max)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    # Permite que el panel web detenga la captura de forma limpia
    # (guardando el clip pendiente) con CTRL_BREAK en Windows.
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)

    logging.info("Directo: %s", URL)

    for programa in ("ffmpeg", "ffprobe"):
        if shutil.which(programa) is None:
            raise SystemExit(
                f"No se encontró {programa} en el PATH."
            )

    for carpeta in (BUFFER, LOGS, CLIPS, TEMPORALES):
        carpeta.mkdir(parents=True, exist_ok=True)

    # Restos de ejecuciones anteriores (cierres bruscos, clips que fallaron):
    # sesiones de buffer y carpetas temporales con más de 1-2 días y logs
    # de más de 2 semanas. Solo hijos directos con el nombre esperado.
    sesion = r"\d{8}_\d{6}_\d+"
    limpiar_antiguos(BUFFER, sesion, 24 * 3600, directorios=True)
    limpiar_antiguos(TEMPORALES, sesion, 48 * 3600, directorios=True)
    limpiar_antiguos(LOGS, r"(ffmpeg|captura)_.*\.log", 14 * 24 * 3600)

    detener_subidas = None

    if SUBIR_CLIPS:
        # Importación diferida: supabase/dotenv solo hacen falta si se activa.
        from subidas_supabase import iniciar_subidas

        detener_subidas = iniciar_subidas()

    analizador = AnalizadorLocal(BASE)

    # Estado en vivo para la pestaña "En proceso" del panel web.
    estado = EstadoProceso(proveedor_ia=analizador.estado)
    estado.actualizar(url=URL, duracion_max=DURACION_MAXIMA)
    estado.iniciar()

    gestor = GestorClips(analizador, estado)

    # Subtitula clips anteriores que no llegaron a procesarse.
    analizador.subtitular_pendientes()

    logging.info(
        "Subidas automáticas: %s",
        "ACTIVADAS" if SUBIR_CLIPS else "DESACTIVADAS",
    )



    logging.info("Carpeta de clips: %s", CLIPS)

    logging.info(
        "Configuración: %s s antes, %s s después, "
        "%s s de pausa entre detecciones y duración máxima %s.",
        SEGUNDOS_ANTES,
        SEGUNDOS_DESPUES,
        PAUSA_ENTRE_DETECCIONES,
        f"{DURACION_MAXIMA} s" if DURACION_MAXIMA else "sin tope",
    )

    codigo = 0

    try:
        while True:
            try:
                capturar(gestor, analizador, estado)

            except NoEsDirecto as error:
                # Sin directo activo no tiene sentido reintentar.
                logging.error("%s", error)
                estado.actualizar(fase="error", detalle=str(error))
                codigo = 2
                break

            except Exception as error:
                logging.error("%s", error)
                estado.actualizar(
                    fase="reintentando",
                    detalle=f"{error} — reintentando en 10 s",
                )

                logging.info(
                    "Reintentando dentro de 10 segundos..."
                )

                time.sleep(10)

    except KeyboardInterrupt:
        logging.info("Captura detenida por el usuario.")

    finally:
        if detener_subidas is not None:
            detener_subidas.set()

        analizador.cerrar()
        estado.cerrar("error" if codigo else "detenido")

    raise SystemExit(codigo)


if __name__ == "__main__":
    main()
