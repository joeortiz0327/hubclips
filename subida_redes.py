"""Subida directa de un vídeo exportado a las APIs oficiales.

- YouTube Data API v3: subida reanudable (`videos.insert`).
- TikTok Content Posting API: `inbox` (borrador en la app) o publicación
  directa, subida de archivo en un único trozo (máx. 64 MB).
- Meta Graph API: Reels de página en 3 fases (start, subida, finish).

Cada función devuelve `{"id", "url", "detalle"}` o lanza `ErrorSubida` con un
mensaje apto para mostrar (nunca incluye tokens).

Aviso: las plataformas limitan lo que puede hacer una app sin revisar.
YouTube deja los vídeos de proyectos sin verificar en privado, TikTok solo
permite publicar en privado (`SELF_ONLY`) hasta que audita la app, y Meta
exige revisión de la app para publicar fuera de cuentas de prueba/roles.
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_TIKTOK = 64 * 1024 * 1024


class ErrorSubida(RuntimeError):
    pass


def _mensaje(cuerpo):
    try:
        datos = json.loads(cuerpo)
    except ValueError:
        return cuerpo.decode("utf-8", "replace")[:150]

    if isinstance(datos, dict):
        error = datos.get("error")

        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or "error")[:150]

        if isinstance(error, str):
            return error[:150]

    return "error"


def _peticion(url, datos=None, cabeceras=None, metodo=None, timeout=60):
    """Devuelve (cabeceras, cuerpo_bytes). `datos` puede ser bytes o archivo."""
    solicitud = urllib.request.Request(
        url, data=datos, headers=cabeceras or {}, method=metodo
    )

    try:
        with urllib.request.urlopen(solicitud, timeout=timeout) as r:
            return r.headers, r.read(1048576)
    except urllib.error.HTTPError as error:
        raise ErrorSubida(
            f"HTTP {error.code}: {_mensaje(error.read(4096))}"
        ) from None
    except (urllib.error.URLError, OSError) as error:
        raise ErrorSubida(
            "No se pudo contactar con la plataforma: "
            f"{str(getattr(error, 'reason', error))[:120]}"
        ) from None


def _json(cuerpo):
    try:
        datos = json.loads(cuerpo)
    except ValueError:
        raise ErrorSubida("Respuesta no válida de la plataforma.") from None

    if not isinstance(datos, dict):
        raise ErrorSubida("Respuesta no válida de la plataforma.")

    return datos


def _texto(descripcion, hashtags):
    return "\n\n".join(
        p for p in (descripcion, " ".join(hashtags or [])) if p
    )


# --------------------------------------------------------------------------

def subir_youtube(token, mp4, titulo, descripcion, hashtags, privado):
    tam = mp4.stat().st_size
    texto = _texto(descripcion, hashtags)

    if "#shorts" not in texto.lower():
        texto = (texto + "\n\n#Shorts").strip()

    meta = {
        "snippet": {
            "title": titulo[:100],
            "description": texto[:5000],
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": "private" if privado else "public",
            "selfDeclaredMadeForKids": False,
        },
    }
    cabeceras, _ = _peticion(
        "https://www.googleapis.com/upload/youtube/v3/videos"
        "?uploadType=resumable&part=snippet,status",
        datos=json.dumps(meta).encode("utf-8"),
        cabeceras={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(tam),
        },
    )
    destino = cabeceras.get("Location")

    if not destino or not destino.startswith("https://"):
        raise ErrorSubida("YouTube no devolvió la dirección de subida.")

    with open(mp4, "rb") as archivo:
        _, cuerpo = _peticion(
            destino, datos=archivo, metodo="PUT", timeout=900,
            cabeceras={"Content-Type": "video/mp4",
                       "Content-Length": str(tam)},
        )

    ident = _json(cuerpo).get("id")

    if not ident:
        raise ErrorSubida("YouTube no confirmó el vídeo subido.")

    return {
        "id": ident,
        "url": f"https://www.youtube.com/watch?v={ident}",
        "detalle": "Subido como privado" if privado else "Publicado",
    }


# --------------------------------------------------------------------------

def _tiktok(token, ruta, cuerpo):
    _, datos = _peticion(
        "https://open.tiktokapis.com/v2/" + ruta,
        datos=json.dumps(cuerpo).encode("utf-8"),
        cabeceras={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
    )
    datos = _json(datos)
    error = datos.get("error") or {}

    if error.get("code") not in (None, "ok"):
        raise ErrorSubida(f"TikTok: {str(error.get('message') or error.get('code'))[:150]}")

    return datos.get("data") or {}


def subir_tiktok(token, mp4, titulo, descripcion, hashtags, privado):
    tam = mp4.stat().st_size

    if tam > MAX_TIKTOK:
        raise ErrorSubida("El vídeo supera los 64 MB que admite la subida a TikTok.")

    fuente = {
        "source": "FILE_UPLOAD", "video_size": tam,
        "chunk_size": tam, "total_chunk_count": 1,
    }

    if privado:
        # Borrador: llega a la bandeja de TikTok y el usuario lo publica.
        datos = _tiktok(token, "post/publish/inbox/video/init/",
                        {"source_info": fuente})
    else:
        info = _tiktok(token, "post/publish/creator_info/query/", {})
        opciones = info.get("privacy_level_options") or []
        nivel = (
            "PUBLIC_TO_EVERYONE" if "PUBLIC_TO_EVERYONE" in opciones
            else (opciones[0] if opciones else "SELF_ONLY")
        )
        leyenda = " ".join([titulo] + (hashtags or []))[:2200]
        datos = _tiktok(token, "post/publish/video/init/", {
            "post_info": {"title": leyenda, "privacy_level": nivel},
            "source_info": fuente,
        })

    ident, subida = datos.get("publish_id"), datos.get("upload_url")

    if not ident or not subida or not subida.startswith("https://"):
        raise ErrorSubida("TikTok no devolvió la dirección de subida.")

    with open(mp4, "rb") as archivo:
        _peticion(
            subida, datos=archivo, metodo="PUT", timeout=900,
            cabeceras={
                "Content-Type": "video/mp4",
                "Content-Length": str(tam),
                "Content-Range": f"bytes 0-{tam - 1}/{tam}",
            },
        )

    estado = ""

    for _ in range(6):
        estado_datos = _tiktok(
            token, "post/publish/status/fetch/", {"publish_id": ident}
        )
        estado = estado_datos.get("status", "")

        if estado == "FAILED":
            raise ErrorSubida(
                "TikTok rechazó el vídeo: "
                f"{estado_datos.get('fail_reason', 'sin motivo')}"
            )

        if estado in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            break

        time.sleep(4)

    detalle = (
        "Enviado a la bandeja de borradores de TikTok"
        if estado == "SEND_TO_USER_INBOX" or privado
        else "Publicado" if estado == "PUBLISH_COMPLETE"
        else "TikTok sigue procesándolo"
    )
    return {"id": ident, "url": None, "detalle": detalle}


# --------------------------------------------------------------------------

def _graph(url, datos):
    _, cuerpo = _peticion(
        url, datos=urllib.parse.urlencode(datos).encode("ascii"),
        timeout=60,
    )
    return _json(cuerpo)


def subir_facebook(token, page_id, mp4, titulo, descripcion, hashtags, privado):
    if not page_id or not re.fullmatch(r"\d{1,30}", str(page_id)):
        raise ErrorSubida("Falta el ID de la página: vuelve a conectar Facebook.")

    tam = mp4.stat().st_size
    base = f"https://graph.facebook.com/v21.0/{page_id}/video_reels"
    inicio = _graph(base, {"upload_phase": "start", "access_token": token})
    ident, subida = inicio.get("video_id"), inicio.get("upload_url")

    if not ident or not subida or not subida.startswith("https://"):
        raise ErrorSubida("Facebook no devolvió la dirección de subida.")

    with open(mp4, "rb") as archivo:
        _, cuerpo = _peticion(
            subida, datos=archivo, timeout=900,
            cabeceras={
                "Authorization": f"OAuth {token}",
                "offset": "0",
                "file_size": str(tam),
                "Content-Type": "application/octet-stream",
                "Content-Length": str(tam),
            },
        )

    if _json(cuerpo).get("success") is False:
        raise ErrorSubida("Facebook no aceptó el archivo.")

    fin = _graph(base, {
        "upload_phase": "finish", "video_id": ident,
        "video_state": "DRAFT" if privado else "PUBLISHED",
        "title": titulo[:100],
        "description": _texto(descripcion, hashtags)[:2200],
        "access_token": token,
    })

    if fin.get("success") is False:
        raise ErrorSubida("Facebook no confirmó la publicación del Reel.")

    return {
        "id": ident,
        "url": f"https://www.facebook.com/reel/{ident}",
        "detalle": "Guardado como borrador en la página" if privado
        else "Publicado",
    }


def subir(plataforma, credenciales, mp4, titulo, descripcion, hashtags,
          privado, token):
    """Despacha según la plataforma. `token` ya está vigente."""
    if plataforma == "youtube_shorts":
        return subir_youtube(token, mp4, titulo, descripcion, hashtags, privado)

    if plataforma == "tiktok":
        return subir_tiktok(token, mp4, titulo, descripcion, hashtags, privado)

    if plataforma == "facebook_reels":
        return subir_facebook(
            token, credenciales.get("page_id"), mp4, titulo, descripcion,
            hashtags, privado,
        )

    raise ErrorSubida("Esta plataforma no admite subida directa.")
