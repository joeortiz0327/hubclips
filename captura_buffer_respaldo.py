import logging
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import yt_dlp


URL = "https://www.youtube.com/watch?v=Q7vkfFh7VVg"

BASE = Path(__file__).resolve().parent
BUFFER = BASE / "datos" / "buffer"
LOGS = BASE / "datos" / "logs"

SEGUNDOS_POR_SEGMENTO = 6

# Aproximadamente cuatro minutos.
# La duración exacta depende de los fotogramas clave.
SEGMENTOS_A_CONSERVAR = 40

ESPACIO_MINIMO = 2 * 1024**3  # 2 GiB libres


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
        raise RuntimeError(
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

    # Conservamos un segmento adicional porque el último
    # puede estar todavía abierto y siendo escrito.
    sobrantes = len(archivos) - (SEGMENTOS_A_CONSERVAR + 1)

    for archivo in archivos[:max(0, sobrantes)]:
        try:
            archivo.unlink()
        except OSError:
            logging.warning(
                "No se pudo eliminar %s",
                archivo.name,
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


def capturar():
    sesion = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    carpeta = BUFFER / sesion
    carpeta.mkdir(parents=True)

    ruta_log = LOGS / f"ffmpeg_{sesion}.log"

    logging.info("Resolviendo el directo...")
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

            ultimo_nombre = None
            ultima_actividad = time.monotonic()

            while proceso.poll() is None:
                if shutil.disk_usage(BASE).free < ESPACIO_MINIMO:
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

                # Detecta una captura estancada.
                if time.monotonic() - ultima_actividad > 120:
                    raise RuntimeError(
                        "No aparecen nuevos segmentos "
                        "desde hace 120 segundos."
                    )

                limpiar_buffer()
                time.sleep(2)

            raise RuntimeError(
                f"FFmpeg terminó con código "
                f"{proceso.returncode}. Revisa {ruta_log}"
            )

        finally:
            detener(proceso)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    for programa in ("ffmpeg", "ffprobe"):
        if shutil.which(programa) is None:
            raise SystemExit(
                f"No se encontró {programa} en el PATH."
            )

    BUFFER.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)

    try:
        while True:
            try:
                capturar()
            except Exception as error:
                logging.error("%s", error)
                logging.info(
                    "Reintentando dentro de 10 segundos..."
                )
                time.sleep(10)

    except KeyboardInterrupt:
        logging.info("Captura detenida por el usuario.")


if __name__ == "__main__":
    main()
