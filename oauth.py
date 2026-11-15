"""OAuth 2.0 (código de autorización) para YouTube, TikTok y Facebook.

Las credenciales de TU aplicación (las que das de alta en cada consola de
desarrolladores) se leen de variables de entorno o del archivo `.env`:

    YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET     (Google Cloud Console)
    TIKTOK_CLIENT_ID  / TIKTOK_CLIENT_SECRET      (client key/secret, TikTok for Developers)
    FACEBOOK_CLIENT_ID / FACEBOOK_CLIENT_SECRET   (ID y secreto de la app de Meta)
    OAUTH_BASE_URL    (opcional; por defecto http://127.0.0.1:<puerto del panel>)

La URL de retorno que hay que registrar en cada consola es
`<OAUTH_BASE_URL>/auth/<youtube|tiktok|facebook>/callback`.

Este módulo no guarda nada: `iniciar()` devuelve la URL de autorización y
`completar()` devuelve los tokens; quien llama los almacena.
"""
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
RUTA_ENV = BASE / ".env"

CADUCIDAD_ESTADO = 600  # segundos para completar el inicio de sesión

PLATAFORMAS = {
    "youtube": {
        "nombre": "YouTube",
        "plataforma": "youtube_shorts",
        "destino": "shorts",
        "env": ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET"),
        "autorizar": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scopes": " ".join([
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly",
        ]),
    },
    "tiktok": {
        "nombre": "TikTok",
        "plataforma": "tiktok",
        "destino": "tiktok",
        "env": ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"),
        "autorizar": "https://www.tiktok.com/v2/auth/authorize/",
        "token": "https://open.tiktokapis.com/v2/oauth/token/",
        "scopes": "user.info.basic,video.upload,video.publish",
    },
    "facebook": {
        "nombre": "Facebook",
        "plataforma": "facebook_reels",
        "destino": "facebook",
        "env": ("FACEBOOK_CLIENT_ID", "FACEBOOK_CLIENT_SECRET"),
        "autorizar": "https://www.facebook.com/v21.0/dialog/oauth",
        "token": "https://graph.facebook.com/v21.0/oauth/access_token",
        "scopes": "pages_show_list,pages_read_engagement,"
                  "pages_manage_posts,publish_video",
    },
}

_CERROJO = threading.Lock()
_ESTADOS = {}  # state -> {"red": ..., "t": ...}
_CACHE_ENV = {"clave": None, "valores": {}}


class ErrorOAuth(RuntimeError):
    """Error con un mensaje apto para mostrar (nunca incluye secretos)."""


# --------------------------------------------------------------------------
# Configuración (.env)
# --------------------------------------------------------------------------

def _leer_env():
    try:
        clave = RUTA_ENV.stat().st_mtime_ns
    except OSError:
        return {}

    if _CACHE_ENV["clave"] == clave:
        return _CACHE_ENV["valores"]

    valores = {}

    try:
        for linea in RUTA_ENV.read_text(encoding="utf-8-sig").splitlines():
            linea = linea.strip()

            if not linea or linea.startswith("#") or "=" not in linea:
                continue

            nombre, _, valor = linea.partition("=")
            nombre = nombre.strip().removeprefix("export ").strip()
            valor = valor.strip()

            if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "'\"":
                valor = valor[1:-1]

            valores[nombre] = valor
    except OSError:
        return {}

    _CACHE_ENV.update(clave=clave, valores=valores)
    return valores


def _variable(nombre):
    return (os.environ.get(nombre) or _leer_env().get(nombre) or "").strip()


def credenciales_app(red):
    id_var, secreto_var = PLATAFORMAS[red]["env"]
    cliente = _variable(id_var)

    if not cliente and red == "tiktok":
        cliente = _variable("TIKTOK_CLIENT_ID")  # nombre alternativo

    return cliente, _variable(secreto_var)


def base_url(puerto):
    # En Render, RENDER_EXTERNAL_URL ya es la URL https pública del servicio.
    return (
        _variable("OAUTH_BASE_URL")
        or os.environ.get("RENDER_EXTERNAL_URL", "").strip()
        or f"http://127.0.0.1:{puerto}"
    ).rstrip("/")


def url_retorno(red, puerto):
    """URL de retorno. `<RED>_REDIRECT_URI` (p. ej. TIKTOK_REDIRECT_URI) manda
    y se usa tal cual, igual en la autorización y al canjear el código."""
    fija = _variable(f"{red.upper()}_REDIRECT_URI")

    if fija:
        return fija

    return f"{base_url(puerto)}/auth/{red}/callback"


def estado_config(puerto):
    """Qué redes tienen sus claves en .env (sin revelar ningún valor)."""
    resultado = {}

    for red, p in PLATAFORMAS.items():
        faltan = [
            nombre for nombre, valor in zip(p["env"], credenciales_app(red))
            if not valor
        ]
        resultado[red] = {
            "nombre": p["nombre"],
            "plataforma": p["plataforma"],
            "configurado": not faltan,
            "faltan": faltan,
            "url_retorno": url_retorno(red, puerto),
        }

    return resultado


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _mensaje_error(cuerpo):
    try:
        datos = json.loads(cuerpo)
    except ValueError:
        return cuerpo.decode("utf-8", "replace")[:150]

    for clave in ("error_description", "message"):
        if isinstance(datos, dict) and isinstance(datos.get(clave), str):
            return datos[clave][:150]

    error = datos.get("error") if isinstance(datos, dict) else None

    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "error")[:150]

    return str(error or "error")[:150]


def _http(url, datos=None, cabeceras=None, metodo=None):
    """Petición que devuelve JSON. Los errores no incluyen secretos."""
    cuerpo = None

    if datos is not None:
        cuerpo = urllib.parse.urlencode(datos).encode("ascii")

    solicitud = urllib.request.Request(
        url, data=cuerpo, method=metodo,
        headers={"Accept": "application/json", **(cabeceras or {})},
    )

    try:
        with urllib.request.urlopen(solicitud, timeout=20) as r:
            return json.loads(r.read(262144).decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        raise ErrorOAuth(
            f"La plataforma respondió HTTP {error.code}: "
            f"{_mensaje_error(error.read(4096))}"
        ) from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise ErrorOAuth(
            "No se pudo contactar con la plataforma: "
            f"{str(getattr(error, 'reason', error))[:120]}"
        ) from None


def _caduca(segundos):
    if not segundos:
        return None

    momento = datetime.now(timezone.utc) + timedelta(seconds=int(segundos))
    return momento.isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Flujo
# --------------------------------------------------------------------------

def iniciar(red, puerto):
    """URL de autorización a la que redirigir al usuario."""
    p = PLATAFORMAS[red]
    cliente, secreto = credenciales_app(red)

    if not cliente or not secreto:
        raise ErrorOAuth(
            f"Faltan {' y '.join(p['env'])} en el archivo .env. "
            "Añádelos y reinicia el panel."
        )

    estado = secrets.token_urlsafe(24)
    ahora = time.time()

    with _CERROJO:
        for k in [k for k, v in _ESTADOS.items()
                  if ahora - v["t"] > CADUCIDAD_ESTADO]:
            del _ESTADOS[k]

        _ESTADOS[estado] = {"red": red, "t": ahora}

    parametros = {
        "response_type": "code",
        "redirect_uri": url_retorno(red, puerto),
        "state": estado,
    }

    if red == "tiktok":
        parametros.update(client_key=cliente, scope=p["scopes"])
    else:
        parametros.update(client_id=cliente, scope=p["scopes"])

    if red == "youtube":
        parametros.update(access_type="offline", prompt="consent")

    return p["autorizar"] + "?" + urllib.parse.urlencode(parametros)


def _consumir_estado(red, estado):
    with _CERROJO:
        dato = _ESTADOS.pop(estado, None) if isinstance(estado, str) else None

    if (
        dato is None or dato["red"] != red
        or time.time() - dato["t"] > CADUCIDAD_ESTADO
    ):
        raise ErrorOAuth(
            "La sesión de conexión no es válida o caducó. Vuelve a pulsar «Conectar»."
        )


def completar(red, codigo, estado, puerto):
    """Canjea el código por tokens. Devuelve el dict de credenciales."""
    _consumir_estado(red, estado)

    if not isinstance(codigo, str) or not codigo or len(codigo) > 2000:
        raise ErrorOAuth("La plataforma no devolvió un código válido.")

    p = PLATAFORMAS[red]
    cliente, secreto = credenciales_app(red)
    retorno = url_retorno(red, puerto)

    if red == "youtube":
        t = _http(p["token"], {
            "code": codigo, "client_id": cliente, "client_secret": secreto,
            "redirect_uri": retorno, "grant_type": "authorization_code",
        })
        token = t.get("access_token")

        if not token:
            raise ErrorOAuth("Google no devolvió un token de acceso.")

        canal = _http(
            "https://www.googleapis.com/youtube/v3/channels"
            "?part=snippet&mine=true",
            cabeceras={"Authorization": f"Bearer {token}"},
        )
        items = canal.get("items") or []

        if not items:
            raise ErrorOAuth(
                "Esa cuenta de Google no tiene un canal de YouTube."
            )

        return {
            "access_token": token,
            "refresh_token": t.get("refresh_token"),
            "expires_at": _caduca(t.get("expires_in")),
            "cuenta": (items[0].get("snippet") or {}).get("title", "YouTube"),
        }

    if red == "tiktok":
        t = _http(p["token"], {
            "client_key": cliente, "client_secret": secreto, "code": codigo,
            "grant_type": "authorization_code", "redirect_uri": retorno,
        })
        token = t.get("access_token")

        if not token:
            raise ErrorOAuth(
                "TikTok no devolvió un token: "
                f"{t.get('error_description') or t.get('error') or '?'}"
            )

        usuario = _http(
            "https://open.tiktokapis.com/v2/user/info/?fields=display_name",
            cabeceras={"Authorization": f"Bearer {token}"},
        )
        nombre = ((usuario.get("data") or {}).get("user") or {}).get(
            "display_name", "TikTok"
        )
        return {
            "access_token": token,
            "refresh_token": t.get("refresh_token"),
            "expires_at": _caduca(t.get("expires_in")),
            "open_id": t.get("open_id"),
            "cuenta": nombre,
        }

    # Facebook: código → token de usuario → larga duración → token de página.
    corto = _http(p["token"] + "?" + urllib.parse.urlencode({
        "client_id": cliente, "client_secret": secreto,
        "redirect_uri": retorno, "code": codigo,
    }))

    if not corto.get("access_token"):
        raise ErrorOAuth("Facebook no devolvió un token de acceso.")

    largo = _http(p["token"] + "?" + urllib.parse.urlencode({
        "grant_type": "fb_exchange_token", "client_id": cliente,
        "client_secret": secreto, "fb_exchange_token": corto["access_token"],
    }))
    usuario = largo.get("access_token") or corto["access_token"]
    paginas = _http(
        "https://graph.facebook.com/v21.0/me/accounts?fields=id,name,access_token",
        cabeceras={"Authorization": f"Bearer {usuario}"},
    ).get("data") or []

    if not paginas:
        raise ErrorOAuth(
            "Tu cuenta de Facebook no administra ninguna página (o no diste "
            "permiso sobre ella). Publicar Reels exige una página."
        )

    pagina = paginas[0]  # los tokens de página derivados no caducan
    return {
        "access_token": pagina["access_token"],
        "page_id": str(pagina["id"]),
        "cuenta": pagina.get("name", "Página de Facebook"),
    }


def refrescar(red, refresh_token):
    """Renueva el token de acceso. Devuelve los campos nuevos."""
    p = PLATAFORMAS[red]
    cliente, secreto = credenciales_app(red)

    if red == "youtube":
        t = _http(p["token"], {
            "client_id": cliente, "client_secret": secreto,
            "refresh_token": refresh_token, "grant_type": "refresh_token",
        })
    elif red == "tiktok":
        t = _http(p["token"], {
            "client_key": cliente, "client_secret": secreto,
            "refresh_token": refresh_token, "grant_type": "refresh_token",
        })
    else:
        raise ErrorOAuth("Facebook no usa refresh_token.")

    if not t.get("access_token"):
        raise ErrorOAuth("La plataforma no devolvió un token nuevo.")

    return {
        "access_token": t["access_token"],
        # Google conserva el refresh_token; TikTok puede rotarlo.
        "refresh_token": t.get("refresh_token") or refresh_token,
        "expires_at": _caduca(t.get("expires_in")),
    }


def ha_caducado(expires_at, margen=120):
    if not expires_at:
        return False

    try:
        momento = datetime.fromisoformat(expires_at)
    except ValueError:
        return False

    return momento - timedelta(seconds=margen) <= datetime.now(timezone.utc)


logging.getLogger(__name__).addHandler(logging.NullHandler())
