"""Límites de CPU/GPU y limpieza de temporales para los procesos pesados.

Variables de entorno (todas opcionales):

    HUBCLIPS_HILOS_FFMPEG      hilos por FFmpeg (por defecto, la mitad de los núcleos)
    HUBCLIPS_EXPORTS_PARALELAS exportaciones 9:16 a la vez (por defecto 1)
    HUBCLIPS_ENCODER           cpu (por defecto), auto, nvenc, qsv o amf
    HUBCLIPS_DATA_DIR          carpeta de datos (por defecto ./datos); en la nube,
                               el disco persistente (p. ej. /data)
"""
import gc
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent

# Todo lo que se genera (clips, buffer, logs, registros) cuelga de aquí.
DATOS = Path(os.environ.get("HUBCLIPS_DATA_DIR") or BASE / "datos").resolve()


def ruta_de_datos(relativa):
    """Ruta absoluta de una carpeta configurada.

    Las relativas que empiezan por `datos/` se resuelven dentro de DATOS
    (así `datos/publicar/tiktok` sigue valiendo con HUBCLIPS_DATA_DIR); el
    resto, desde la raíz del proyecto.
    """
    ruta = Path(relativa)

    if ruta.is_absolute():
        return ruta

    if ruta.parts and ruta.parts[0] == "datos":
        return DATOS.joinpath(*ruta.parts[1:])

    return BASE / ruta


# Windows: BELOW_NORMAL_PRIORITY_CLASS. Se aplica a los procesos FFmpeg que
# no son de tiempo real (render, miniaturas, análisis): los hijos heredan
# la prioridad, así que no quitan CPU a la captura ni al panel.
BAJA_PRIORIDAD = 0x00004000 if os.name == "nt" else 0


def _entero_env(nombre, defecto, minimo, maximo):
    try:
        valor = int(os.environ.get(nombre, ""))
    except ValueError:
        return defecto

    return max(minimo, min(maximo, valor))


NUCLEOS = os.cpu_count() or 2

# La mitad de los núcleos, entre 1 y 8: deja CPU libre para el resto.
HILOS_FFMPEG = _entero_env(
    "HUBCLIPS_HILOS_FFMPEG", max(1, min(8, NUCLEOS // 2)), 1, 64
)
EXPORTS_PARALELAS = _entero_env("HUBCLIPS_EXPORTS_PARALELAS", 1, 1, 4)

# Exportaciones esperando turno antes de rechazar nuevas (evita acumular
# hilos del servidor y trabajo que nadie va a recoger).
MAX_EN_COLA = 6


class ColaLlena(RuntimeError):
    """Demasiados trabajos de render pendientes."""


def args_hilos():
    """Argumentos de FFmpeg que acotan sus hilos."""
    n = str(HILOS_FFMPEG)
    return ["-threads", n, "-filter_threads", n, "-filter_complex_threads", n]


# --------------------------------------------------------------------------
# Códec de vídeo (CPU por defecto; GPU opcional)
# --------------------------------------------------------------------------

_CODECS = {
    "cpu": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"],
    "nvenc": ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr",
              "-cq", "23", "-b:v", "0"],
    "qsv": ["-c:v", "h264_qsv", "-global_quality", "23"],
    "amf": ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp",
            "-qp_i", "23", "-qp_p", "23"],
}
_CODEC_ELEGIDO = {"nombre": None}
_CERROJO_CODEC = threading.Lock()


def _funciona(nombre):
    """Prueba real: codifica 0,2 s con ese códec (¿hay GPU compatible?)."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
             "color=c=black:s=256x256:d=0.2", *_CODECS[nombre],
             "-pix_fmt", "yuv420p", "-f", "null", "-"],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
            creationflags=BAJA_PRIORIDAD,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def codec_video():
    """Argumentos `-c:v ...` según HUBCLIPS_ENCODER; cae a CPU si falla."""
    with _CERROJO_CODEC:
        if _CODEC_ELEGIDO["nombre"] is None:
            pedido = (os.environ.get("HUBCLIPS_ENCODER") or "cpu").lower()
            candidatos = (
                ["nvenc", "qsv", "amf"] if pedido == "auto"
                else [pedido] if pedido in _CODECS and pedido != "cpu"
                else []
            )
            elegido = "cpu"

            for nombre in candidatos:
                if _funciona(nombre):
                    elegido = nombre
                    break

                logging.warning(
                    "Encoder %s no disponible; se usa la CPU.", nombre
                )

            _CODEC_ELEGIDO["nombre"] = elegido
            logging.info("Encoder de vídeo: %s", elegido)

        return list(_CODECS[_CODEC_ELEGIDO["nombre"]])


# --------------------------------------------------------------------------
# Limpieza
# --------------------------------------------------------------------------

def limpiar_antiguos(carpeta, patron, edad_s, directorios=False):
    """Borra hijos directos de `carpeta` que cumplen `patron` (regex del
    nombre completo) y llevan más de `edad_s` sin modificarse.

    Solo mira un nivel y nunca sigue enlaces simbólicos. Devuelve cuántos
    borró. No lanza excepciones: la limpieza es secundaria.
    """
    carpeta = Path(carpeta)
    regex = re.compile(patron)
    limite = time.time() - edad_s
    borrados = 0

    try:
        hijos = list(carpeta.iterdir())
    except OSError:
        return 0

    for ruta in hijos:
        try:
            if ruta.is_symlink() or not regex.fullmatch(ruta.name):
                continue

            if ruta.is_dir() != directorios:
                continue

            if ruta.stat().st_mtime > limite:
                continue

            if directorios:
                shutil.rmtree(ruta, ignore_errors=True)
            else:
                ruta.unlink()

            borrados += 1
        except OSError:
            continue

    if borrados:
        logging.info("Limpieza: %s elemento(s) antiguos en %s", borrados,
                     carpeta.name)

    return borrados


def liberar_memoria():
    """Devuelve a Python/SO la memoria de objetos grandes ya sin uso."""
    gc.collect()
