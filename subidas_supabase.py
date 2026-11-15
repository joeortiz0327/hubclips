import logging
import mimetypes
import os
import threading
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client


BASE = Path(__file__).resolve().parent
CLIPS = BASE / "datos" / "clips"
REGISTROS = BASE / "datos" / "subidos_supabase"

INTERVALO_REVISION = 15

# Límite conservador. El límite real depende de tu bucket y plan.
TAMANO_MAXIMO = 50_000_000


def iniciar_subidas():
    """Inicia un único trabajador de subida en segundo plano."""
    load_dotenv(BASE / ".env")

    url = os.getenv("SUPABASE_URL")
    clave = os.getenv("SUPABASE_KEY")
    bucket = os.getenv("SUPABASE_BUCKET", "hubclips")

    if not url or not clave:
        raise RuntimeError(
            "Faltan SUPABASE_URL o SUPABASE_KEY en el archivo .env"
        )

    CLIPS.mkdir(parents=True, exist_ok=True)
    REGISTROS.mkdir(parents=True, exist_ok=True)

    detener = threading.Event()

    def trabajar():
        cliente = None
        grandes_avisados = set()

        while not detener.is_set():
            try:
                if cliente is None:
                    cliente = create_client(url, clave)

                # Solo clips terminados de la carpeta principal.
                # No se recorren los segmentos temporales.
                archivos = sorted(
                    archivo
                    for archivo in CLIPS.iterdir()
                    if archivo.is_file()
                    and archivo.suffix.lower()
                    in {".mkv", ".mp4", ".webm"}
                )

                for clip in archivos:
                    if detener.is_set():
                        break

                    registro = REGISTROS / f"{clip.name}.txt"

                    if registro.exists():
                        continue

                    tamano = clip.stat().st_size

                    if tamano == 0:
                        continue

                    if tamano > TAMANO_MAXIMO:
                        if clip.name not in grandes_avisados:
                            logging.warning(
                                "SUBIDA OMITIDA: %s supera el límite "
                                "configurado de 50 MB. Se conserva localmente.",
                                clip.name,
                            )
                            grandes_avisados.add(clip.name)

                        continue

                    # Ruta estable: un reintento no crea otro nombre.
                    ruta_remota = f"clips/{clip.name}"

                    tipo = (
                        mimetypes.guess_type(clip.name)[0]
                        or "application/octet-stream"
                    )

                    logging.info(
                        "SUBIENDO: %s | %.2f MB",
                        clip.name,
                        tamano / (1024 * 1024),
                    )

                    with clip.open("rb") as archivo:
                        cliente.storage.from_(bucket).upload(
                            path=ruta_remota,
                            file=archivo,
                            file_options={
                                "content-type": tipo,
                                "upsert": "true",
                            },
                        )

                    # Se marca solo después de una subida correcta.
                    registro.write_text(
                        f"{bucket}/{ruta_remota}\n",
                        encoding="utf-8",
                    )

                    logging.info(
                        "CLIP SUBIDO: %s/%s",
                        bucket,
                        ruta_remota,
                    )

            except Exception as error:
                logging.warning(
                    "No se pudo completar la subida: %s. "
                    "Se reintentará en %s segundos. "
                    "El archivo local se conserva.",
                    error,
                    INTERVALO_REVISION,
                )

                cliente = None

            detener.wait(INTERVALO_REVISION)

    hilo = threading.Thread(
        target=trabajar,
        name="subidas-supabase",
        daemon=True,
    )
    hilo.start()

    logging.info(
        "Subida automática activada. Bucket: %s",
        bucket,
    )

    return detener
