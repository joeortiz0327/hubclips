import mimetypes
from pathlib import Path
from uuid import uuid4
import os

from dotenv import load_dotenv
from supabase import create_client


BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

url = os.getenv("SUPABASE_URL")
clave = os.getenv("SUPABASE_KEY")
bucket = os.getenv("SUPABASE_BUCKET", "hubclips-clips")

if not url or not clave:
    raise SystemExit(
        "Faltan SUPABASE_URL o SUPABASE_KEY en el archivo .env"
    )

carpeta = BASE / "datos" / "clips"

clips = [
    archivo
    for archivo in carpeta.glob("*")
    if archivo.is_file()
    and archivo.suffix.lower() in {".mkv", ".mp4", ".webm"}
]

if not clips:
    raise SystemExit(f"No hay clips en: {carpeta}")

# Elegimos el clip más reciente.
clip = max(clips, key=lambda archivo: archivo.stat().st_mtime)
tamano = clip.stat().st_size

print(f"Clip seleccionado: {clip.name}")
print(f"Tamaño: {tamano / (1024 * 1024):.2f} MB")

# Límite conservador para la prueba con el plan gratuito.
if tamano > 50_000_000:
    raise SystemExit(
        "El clip supera los 50 MB. Necesitamos uno más pequeño para la prueba."
    )

cliente = create_client(url, clave)

# Nombre único para evitar sobrescribir otros vídeos.
ruta_remota = f"clips/{uuid4().hex}_{clip.name}"

tipo = (
    mimetypes.guess_type(clip.name)[0]
    or "application/octet-stream"
)

print("Subiendo a Supabase...")

with clip.open("rb") as archivo:
    cliente.storage.from_(bucket).upload(
        path=ruta_remota,
        file=archivo,
        file_options={"content-type": tipo},
    )

print("\n¡CLIP SUBIDO CORRECTAMENTE!")
print(f"Bucket: {bucket}")
print(f"Archivo: {ruta_remota}")
print("El archivo original sigue guardado en tu PC.")
