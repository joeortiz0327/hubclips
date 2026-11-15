"""Arranca y detiene captura_buffer.py como subproceso, para el panel web."""
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from recursos import DATOS

BASE = Path(__file__).resolve().parent
LOGS = DATOS / "logs"

DURACIONES_PERMITIDAS = (30, 60, 90)

HOSTS_YOUTUBE = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
}


def validar_url(texto):
    """Devuelve la URL limpia de YouTube o lanza ValueError."""
    texto = (texto or "").strip()

    if not texto or len(texto) > 300 or any(c.isspace() for c in texto):
        raise ValueError("Pega una URL válida.")

    partes = urlsplit(texto)

    if partes.scheme not in ("http", "https") or (
        (partes.hostname or "").lower() not in HOSTS_YOUTUBE
    ):
        raise ValueError("La URL debe ser de YouTube.")

    return texto


def _python():
    # Preferimos el entorno virtual del proyecto si existe.
    venv = BASE / ".venv" / "Scripts" / "python.exe"

    if not venv.is_file():
        venv = BASE / ".venv" / "bin" / "python"

    return str(venv) if venv.is_file() else sys.executable


class Lanzador:
    def __init__(self):
        self._cerrojo = threading.Lock()
        self._proceso = None
        self._log = None
        self._url = None
        self._duracion_max = None
        self._inicio = None
        self._ruta_log = None

    def _activo(self):
        return self._proceso is not None and self._proceso.poll() is None

    def iniciar(self, url, duracion_max=None):
        url = validar_url(url)

        if duracion_max is not None and (
            type(duracion_max) is not int
            or duracion_max not in DURACIONES_PERMITIDAS
        ):
            raise ValueError(
                "Duración máxima no válida: elige "
                + ", ".join(f"{d} s" for d in DURACIONES_PERMITIDAS)
                + "."
            )

        with self._cerrojo:
            if self._activo():
                raise RuntimeError("Ya hay una captura en marcha.")

            LOGS.mkdir(parents=True, exist_ok=True)
            marca = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._ruta_log = LOGS / f"captura_{marca}.log"

            if self._log is not None:
                self._log.close()

            self._log = self._ruta_log.open("wb")

            entorno = dict(
                os.environ,
                PYTHONUNBUFFERED="1",
                PYTHONIOENCODING="utf-8",
            )

            opciones = {}

            if os.name == "nt":
                # Grupo propio: permite enviarle CTRL_BREAK sin afectar
                # al panel.
                opciones["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                )

            # Lista de argumentos, sin shell: la URL nunca se interpreta.
            self._proceso = subprocess.Popen(
                [
                    _python(),
                    str(BASE / "captura_buffer.py"),
                    url,
                    *(
                        ["--duracion-max", str(duracion_max)]
                        if duracion_max
                        else []
                    ),
                ],
                cwd=BASE,
                env=entorno,
                stdin=subprocess.DEVNULL,
                stdout=self._log,
                stderr=subprocess.STDOUT,
                **opciones,
            )

            self._url = url
            self._duracion_max = duracion_max
            self._inicio = datetime.now().isoformat(timespec="seconds")

    def detener(self):
        with self._cerrojo:
            proceso = self._proceso

            if proceso is None or proceso.poll() is not None:
                return

            try:
                if os.name == "nt":
                    proceso.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    proceso.send_signal(signal.SIGINT)

                # Da tiempo a guardar el clip pendiente.
                proceso.wait(timeout=30)
            except (subprocess.TimeoutExpired, OSError):
                proceso.kill()
                proceso.wait()

    def estado(self):
        with self._cerrojo:
            if self._proceso is None:
                return {"activo": False}

            codigo = self._proceso.poll()

            return {
                "activo": codigo is None,
                "codigo": codigo,
                "url": self._url,
                "duracion_max": self._duracion_max,
                "inicio": self._inicio,
                "log": self._ultimas_lineas(),
            }

    def _ultimas_lineas(self, cuantas=4):
        try:
            with self._ruta_log.open("rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 4096))
                texto = f.read().decode("utf-8", errors="replace")
        except OSError:
            return []

        lineas = [l.strip() for l in texto.splitlines() if l.strip()]
        return lineas[-cuantas:]
