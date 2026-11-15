"""Estado en vivo de la captura, para la pestaña "En proceso" del panel.

captura_buffer.py (otro proceso) escribe `datos/estado_proceso.json` con un
latido cada pocos segundos; panel_web.py lo lee. Si el archivo deja de
actualizarse (la captura terminó o se cayó) se considera caducado.
"""
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from recursos import DATOS

# HUBCLIPS_ESTADO permite apuntar a otro archivo (útil en pruebas).
RUTA_ESTADO = Path(
    os.environ.get("HUBCLIPS_ESTADO") or DATOS / "estado_proceso.json"
)

# Segundos sin latido tras los que el estado se da por caducado.
MAX_EDAD = 15

# Segundos máximos entre escrituras aunque nada cambie (latido de vida).
REPOSO_MAX = 5

FASES_ACTIVAS ={"iniciando", "resolviendo", "grabando", "reintentando"}


class EstadoProceso:
    """Escribe el estado de la captura de forma atómica y con latido."""

    def __init__(self, ruta=RUTA_ESTADO, proveedor_ia=None, intervalo=1.5):
        self.ruta = Path(ruta)
        self.proveedor_ia = proveedor_ia
        self.intervalo = intervalo
        self._cerrojo = threading.Lock()
        self._parar = threading.Event()
        self._hilo = None
        self._ultima_firma = None
        self._ultima_escritura = 0.0
        self._datos = {
            "pid": os.getpid(),
            "inicio": datetime.now().isoformat(timespec="seconds"),
            "fase": "iniciando",
            "detalle": "",
            "url": None,
            "duracion_max": None,
            "segmentos": 0,
            "clips_guardados": 0,
            "clip": None,
        }

    def actualizar(self, **campos):
        with self._cerrojo:
            self._datos.update(campos)

    def incrementar(self, campo, cantidad=1):
        with self._cerrojo:
            self._datos[campo] = self._datos.get(campo, 0) + cantidad

    def iniciar(self):
        self._escribir(forzar=True)
        self._hilo = threading.Thread(
            target=self._latido, name="estado-proceso", daemon=True
        )
        self._hilo.start()

    def cerrar(self, fase="finalizado", detalle=None):
        """Último latido. Si detalle es None se conserva el anterior."""
        self._parar.set()

        if self._hilo is not None:
            self._hilo.join(timeout=3)

        campos = {"fase": fase, "clip": None}

        if detalle is not None:
            campos["detalle"] = detalle

        self.actualizar(**campos)
        self._escribir(forzar=True)

    def _latido(self):
        # Hilo propio: sigue latiendo aunque el bucle principal esté
        # ocupado, por ejemplo ensamblando un clip con FFmpeg.
        while not self._parar.wait(self.intervalo):
            self._escribir()

    def _escribir(self, forzar=False):
        try:
            with self._cerrojo:
                datos = dict(self._datos)

            if self.proveedor_ia is not None:
                try:
                    datos["ia"] = self.proveedor_ia()
                except Exception:
                    datos["ia"] = None

            # Sin cambios no se reescribe el archivo cada 1,5 s: basta un
            # latido cada REPOSO_MAX s (MAX_EDAD es 15 s) para seguir vivo.
            firma = json.dumps(datos, ensure_ascii=False, sort_keys=True)
            ahora = time.time()

            if (
                not forzar
                and firma == self._ultima_firma
                and ahora - self._ultima_escritura < REPOSO_MAX
            ):
                return

            datos["t"] = ahora
            datos["actualizado"] = datetime.now().isoformat(
                timespec="seconds"
            )

            self.ruta.parent.mkdir(parents=True, exist_ok=True)
            temporal = self.ruta.with_suffix(".json.tmp")
            temporal.write_text(
                json.dumps(datos, ensure_ascii=False), encoding="utf-8"
            )
            temporal.replace(self.ruta)
            self._ultima_firma = firma
            self._ultima_escritura = ahora
        except OSError:
            # El estado es informativo: nunca debe romper la captura.
            pass


def leer_estado(ruta=RUTA_ESTADO, max_edad=MAX_EDAD):
    """Estado vigente de la captura o None si no hay latido reciente."""
    try:
        datos = json.loads(Path(ruta).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    if not isinstance(datos, dict):
        return None

    if time.time() - float(datos.get("t", 0)) > max_edad:
        return None

    return datos
