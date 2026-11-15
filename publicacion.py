"""Automatización de publicación: paquetes 9:16 listos para redes sociales.

Un *destino* (definido en `destinos.json`) es una carpeta, opcionalmente con
un webhook. Al "enviar a borrador" o "programar", el clip se exporta en
vertical con los subtítulos quemados (exportar_clip.exportar) y se entrega
como paquete en la carpeta del destino:

    <id_clip>_<id_publicacion>.mp4   vídeo 9:16 con subtítulos quemados
    <id_clip>_<id_publicacion>.txt   texto listo para pegar (título, descripción, hashtags)
    <id_clip>_<id_publicacion>.json  metadatos de la publicación

Hubclips NO sube nada a TikTok, YouTube o Instagram por sí mismo (eso exige
credenciales OAuth de cada plataforma). La carpeta puede ser una carpeta
sincronizada o vigilada por una herramienta de publicación, y el webhook
opcional avisa a servicios como Make, Zapier o n8n.

Cada publicación se anota en `datos/publicaciones.json`.
"""
import json
import logging
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import oauth
import secretos
from recursos import DATOS, ColaLlena, ruta_de_datos
import subida_redes
from exportar_clip import AJUSTES, ESTILO_POR_DEFECTO, ESTILOS, exportar

BASE = Path(__file__).resolve().parent

# Con HUBCLIPS_DATA_DIR (despliegue en la nube) destinos.json también vive en
# el disco persistente: contiene los tokens (cifrados) y no debe perderse.
RUTA_DESTINOS = Path(
    os.environ.get("HUBCLIPS_DESTINOS")
    or (DATOS / "destinos.json" if os.environ.get("HUBCLIPS_DATA_DIR")
        else BASE / "destinos.json")
)
RUTA_REGISTRO = Path(
    os.environ.get("HUBCLIPS_PUBLICACIONES")
    or DATOS / "publicaciones.json"
)

ID_DESTINO = re.compile(r"[a-z0-9_-]{1,30}")
ID_PUBLICACION = re.compile(r"[0-9a-f]{8}")

ESTADOS_ACTIVOS = ("programado", "exportando")

# Límites orientativos de cada plataforma (se pueden cambiar por destino).
LIMITES = {
    "tiktok": {"titulo_max": 90, "descripcion_max": 2200},
    "youtube_shorts": {"titulo_max": 100, "descripcion_max": 5000},
    "instagram_reels": {"titulo_max": 100, "descripcion_max": 2200},
    "facebook_reels": {"titulo_max": 100, "descripcion_max": 2200},
    "otro": {"titulo_max": 100, "descripcion_max": 2200},
}

# Credenciales que admite cada plataforma. `access_token` y `client_secret`
# son secretos: se guardan en destinos.json pero la web nunca los recibe.
CAMPOS_CREDENCIALES = {
    "youtube_shorts": (
        "access_token", "refresh_token", "expires_at", "cuenta",
    ),
    "tiktok": (
        "access_token", "refresh_token", "expires_at", "cuenta", "open_id",
    ),
    "facebook_reels": ("access_token", "page_id", "cuenta"),
    "instagram_reels": (),
    "otro": (),
}
# Se guardan cifrados (secretos.py) y la web nunca los recibe.
CAMPOS_SECRETOS = ("access_token", "refresh_token")

# Endpoint oficial que confirma que un token es válido (host fijo).
VERIFICACION = {
    "youtube_shorts":
        "https://www.googleapis.com/youtube/v3/channels"
        "?part=snippet&mine=true",
    "tiktok":
        "https://open.tiktokapis.com/v2/user/info/"
        "?fields=open_id,display_name",
    "facebook_reels": "https://graph.facebook.com/v21.0/me?fields=id,name",
}

RUTA_ESTADO_DESTINOS = Path(
    os.environ.get("HUBCLIPS_ESTADO_DESTINOS")
    or DATOS / "estado_destinos.json"
)
_CERROJO_DESTINOS = threading.Lock()

DESTINOS_POR_DEFECTO = {
    "destinos": [
        {
            "id": "tiktok", "nombre": "TikTok", "plataforma": "tiktok",
            "carpeta": "datos/publicar/tiktok",
            "hashtags": ["#fyp", "#parati"], "webhook": None,
        },
        {
            "id": "shorts", "nombre": "YouTube Shorts",
            "plataforma": "youtube_shorts",
            "carpeta": "datos/publicar/shorts",
            "hashtags": ["#shorts"], "webhook": None,
        },
        {
            "id": "facebook", "nombre": "Facebook Reels",
            "plataforma": "facebook_reels",
            "carpeta": "datos/publicar/facebook",
            "hashtags": ["#reels"], "webhook": None,
        },
        {
            "id": "reels", "nombre": "Instagram Reels",
            "plataforma": "instagram_reels",
            "carpeta": "datos/publicar/reels",
            "hashtags": ["#reels"], "webhook": None,
        },
        {
            "id": "borradores", "nombre": "Borradores (carpeta local)",
            "plataforma": "otro",
            "carpeta": "datos/publicar/borradores",
            "hashtags": [], "webhook": None,
        },
    ]
}


def _ahora():
    return datetime.now().replace(microsecond=0)


def _iso(momento):
    return momento.isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Destinos
# --------------------------------------------------------------------------

def normalizar_hashtags(valor):
    """Acepta lista o texto ('#a, #b c') y devuelve ['#a', '#b', '#c']."""
    if isinstance(valor, str):
        valor = re.split(r"[\s,]+", valor)

    if not isinstance(valor, (list, tuple)):
        raise ValueError("Hashtags no válidos.")

    etiquetas = []

    for item in valor:
        if not isinstance(item, str):
            raise ValueError("Hashtags no válidos.")

        palabra = re.sub(r"[^\w]", "", item.lstrip("#"), flags=re.UNICODE)

        if palabra and f"#{palabra}" not in etiquetas:
            etiquetas.append(f"#{palabra[:50]}")

    if len(etiquetas) > 30:
        raise ValueError("Máximo 30 hashtags.")

    return etiquetas


_CACHE_DESTINOS = {"clave": None, "destinos": None}


def _clave_archivo(ruta):
    try:
        return (str(ruta), ruta.stat().st_mtime_ns)
    except OSError:
        return (str(ruta), None)


def cargar_destinos(ruta=None):
    """Destinos de destinos.json, releídos solo si el archivo cambia."""
    ruta = Path(ruta or RUTA_DESTINOS)

    if (
        _CACHE_DESTINOS["destinos"] is not None
        and _CACHE_DESTINOS["clave"] == _clave_archivo(ruta)
    ):
        return dict(_CACHE_DESTINOS["destinos"])

    destinos = _leer_destinos(ruta)
    _CACHE_DESTINOS["destinos"] = destinos
    _CACHE_DESTINOS["clave"] = _clave_archivo(ruta)
    return dict(destinos)


def _leer_destinos(ruta):
    """Lee destinos.json (lo crea con valores por defecto si no existe)."""
    if not ruta.exists():
        try:
            ruta.parent.mkdir(parents=True, exist_ok=True)
            ruta.write_text(
                json.dumps(DESTINOS_POR_DEFECTO, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            logging.warning("No se pudo crear %s", ruta)

    try:
        # utf-8-sig: el Bloc de notas de Windows añade un BOM al guardar.
        datos = json.loads(ruta.read_text(encoding="utf-8-sig"))
        crudos = datos["destinos"]
    except (OSError, ValueError, KeyError, TypeError):
        logging.warning("destinos.json no válido; se usan los de fábrica.")
        crudos = DESTINOS_POR_DEFECTO["destinos"]

    destinos = {}

    for d in crudos if isinstance(crudos, list) else []:
        try:
            ident = d["id"]
            plataforma = d.get("plataforma", "otro")

            if not isinstance(ident, str) or not ID_DESTINO.fullmatch(ident):
                raise ValueError("id")

            if plataforma not in LIMITES:
                plataforma = "otro"

            carpeta = Path(d["carpeta"])

            carpeta = ruta_de_datos(carpeta)

            webhook = d.get("webhook") or None

            if webhook is not None and (
                not isinstance(webhook, str)
                or urlsplit(webhook).scheme not in ("http", "https")
            ):
                raise ValueError("webhook")

            crudas = d.get("credenciales")
            credenciales = {}

            if isinstance(crudas, dict):
                for campo in CAMPOS_CREDENCIALES[plataforma]:
                    valor = crudas.get(campo)

                    if isinstance(valor, str) and valor.strip():
                        valor = valor.strip()

                        if campo in CAMPOS_SECRETOS:
                            valor = secretos.desproteger(valor)

                        if valor:
                            credenciales[campo] = valor

            destinos[ident] = {
                "id": ident,
                "nombre": str(d.get("nombre") or ident)[:60],
                "plataforma": plataforma,
                "carpeta": carpeta,
                "hashtags": normalizar_hashtags(d.get("hashtags") or []),
                "webhook": webhook,
                "activo": d.get("activo", True) is not False,
                "credenciales": credenciales,
                "titulo_max": int(
                    d.get("titulo_max") or LIMITES[plataforma]["titulo_max"]
                ),
                "descripcion_max": int(
                    d.get("descripcion_max")
                    or LIMITES[plataforma]["descripcion_max"]
                ),
            }
        except (KeyError, TypeError, ValueError):
            logging.warning("Destino ignorado por configuración no válida: %r", d)

    return destinos


def _leer_estados(ruta=None):
    try:
        datos = json.loads(
            Path(ruta or RUTA_ESTADO_DESTINOS).read_text(encoding="utf-8")
        )
        return datos if isinstance(datos, dict) else {}
    except (OSError, ValueError):
        return {}


def _anotar_estado(ident, entrada, ruta=None):
    """Guarda (o borra, con None) el resultado de la última prueba."""
    ruta = Path(ruta or RUTA_ESTADO_DESTINOS)

    with _CERROJO_DESTINOS:
        estados = _leer_estados(ruta)

        if entrada is None:
            estados.pop(ident, None)
        else:
            estados[ident] = entrada

        try:
            ruta.parent.mkdir(parents=True, exist_ok=True)
            temporal = ruta.with_suffix(".json.tmp")
            temporal.write_text(
                json.dumps(estados, ensure_ascii=False), encoding="utf-8"
            )
            temporal.replace(ruta)
        except OSError:
            logging.warning("No se pudo guardar el estado de %s", ident)


def _enmascarar(campo, valor):
    if campo not in CAMPOS_SECRETOS:
        return valor

    return "••••" + valor[-4:] if len(valor) >= 12 else "••••"


def destino_publico(d, estados=None):
    """Vista de un destino para la web.

    Nunca incluye la URL completa del webhook ni los secretos
    (`access_token`, `client_secret`): solo si están definidos.
    """
    estados = _leer_estados() if estados is None else estados
    credenciales = {
        campo: _enmascarar(campo, valor)
        for campo, valor in d["credenciales"].items()
    }
    configurado = bool(d["webhook"] or d["credenciales"])
    prueba = estados.get(d["id"]) if configurado else None
    conectado = "access_token" in d["credenciales"]

    if not configurado:
        estado = "sin_configurar"
    elif not d["activo"]:
        estado = "desactivado"
    elif conectado:
        # Conectado por OAuth; una prueba fallida lo marca como error.
        estado = "error" if prueba and not prueba.get("ok") else "conectado"
    elif prueba is None or (
        # Escribir un archivo en la carpeta no prueba ninguna conexión.
        prueba.get("ok") and prueba.get("modo") == "envio"
        and not d["webhook"]
    ):
        estado = "configurado"
    else:
        estado = "conectado" if prueba.get("ok") else "error"

    return {
        "id": d["id"],
        "nombre": d["nombre"],
        "plataforma": d["plataforma"],
        "carpeta": str(d["carpeta"]),
        "hashtags": d["hashtags"],
        "webhook": bool(d["webhook"]),
        "activo": d["activo"],
        "webhook_host": urlsplit(d["webhook"]).hostname
        if d["webhook"] else None,
        "credenciales": credenciales,
        "campos": list(CAMPOS_CREDENCIALES[d["plataforma"]]),
        "estado": estado,
        "prueba": prueba,
        "titulo_max": d["titulo_max"],
        "descripcion_max": d["descripcion_max"],
    }


def destinos_publicos(destinos):
    estados = _leer_estados()
    return [destino_publico(d, estados) for d in destinos.values()]


def _validar_credencial(campo, valor):
    if not isinstance(valor, str):
        raise ValueError(f"«{campo}» no es válido.")

    if campo == "cuenta":  # nombre visible: texto libre sin control
        return "".join(
            c for c in " ".join(valor.split()) if c.isprintable()
        )[:80]

    valor = valor.strip()

    if len(valor) > 2000 or not re.fullmatch(r"[\x21-\x7e]+", valor):
        raise ValueError(
            f"«{campo}» no es válido (sin espacios ni saltos de línea)."
        )

    return valor


def guardar_destino(datos, ruta=None):
    """Crea o edita un destino en destinos.json (escritura atómica).

    Campos opcionales: ausente = no cambiar. `webhook` vacío lo quita; una
    credencial vacía conserva la guardada (para no tener que reescribir el
    secreto). `desvincular: true` borra webhook y credenciales.
    """
    ruta = Path(ruta or RUTA_DESTINOS)
    ident = datos.get("id")
    plataforma = datos.get("plataforma")

    if not isinstance(ident, str) or not ID_DESTINO.fullmatch(ident):
        raise ValueError("Identificador de destino no válido.")

    if plataforma not in LIMITES:
        raise ValueError("Plataforma no válida.")

    with _CERROJO_DESTINOS:
        cargar_destinos(ruta)  # crea el archivo si no existe

        try:
            crudo = json.loads(ruta.read_text(encoding="utf-8-sig"))
            lista = crudo["destinos"]

            if not isinstance(lista, list):
                raise TypeError
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError(
                "destinos.json no es válido: corrígelo a mano antes de "
                "guardar desde el panel."
            )

        entrada = next(
            (
                d for d in lista
                if isinstance(d, dict) and d.get("id") == ident
            ),
            None,
        )

        if entrada is None:
            entrada = {"id": ident, "plataforma": plataforma}
            lista.append(entrada)
        elif entrada.get("plataforma", "otro") != plataforma:
            raise ValueError(
                "No se puede cambiar la plataforma de un destino existente."
            )

        if "nombre" in datos:
            nombre = str(datos["nombre"] or "").strip()[:60]

            if nombre:
                entrada["nombre"] = nombre

        entrada.setdefault("nombre", ident)

        if "carpeta" in datos:
            carpeta = str(datos["carpeta"] or "").strip()

            if not carpeta or len(carpeta) > 260 or "\x00" in carpeta:
                raise ValueError("La carpeta no es válida.")

            entrada["carpeta"] = carpeta

        entrada.setdefault("carpeta", f"datos/publicar/{ident}")

        if "hashtags" in datos:
            entrada["hashtags"] = normalizar_hashtags(datos["hashtags"] or [])

        if "activo" in datos:
            if not isinstance(datos["activo"], bool):
                raise ValueError("«activo» debe ser verdadero o falso.")

            entrada["activo"] = datos["activo"]

        if datos.get("desvincular") is True:
            entrada["webhook"] = None
            entrada.pop("credenciales", None)
        else:
            if "webhook" in datos:
                url = (datos["webhook"] or "")

                if not isinstance(url, str):
                    raise ValueError("El webhook no es válido.")

                url = url.strip()

                if url:
                    partes = urlsplit(url)

                    if (
                        partes.scheme not in ("http", "https")
                        or not partes.hostname
                        or len(url) > 1000
                        or not re.fullmatch(r"[\x21-\x7e]+", url)
                    ):
                        raise ValueError(
                            "El webhook debe ser una URL http(s) válida."
                        )

                entrada["webhook"] = url or None

            nuevas = datos.get("credenciales")

            if nuevas is not None:
                if not isinstance(nuevas, dict):
                    raise ValueError("Credenciales no válidas.")

                permitidos = CAMPOS_CREDENCIALES[plataforma]
                guardadas = (
                    {} if datos.get("reemplazar_credenciales") is True
                    else dict(entrada.get("credenciales") or {})
                )

                for campo, valor in nuevas.items():
                    if campo not in permitidos:
                        raise ValueError(
                            f"«{campo}» no se usa en esta plataforma."
                        )

                    if valor is None or (
                        isinstance(valor, str) and not valor.strip()
                    ):
                        continue  # vacío: conservar la guardada

                    valor = _validar_credencial(campo, valor)

                    if campo in CAMPOS_SECRETOS:
                        valor = secretos.proteger(valor)

                    guardadas[campo] = valor

                if datos.get("reemplazar_credenciales") is True:
                    entrada.pop("credenciales", None)

                if guardadas:
                    entrada["credenciales"] = guardadas

        temporal = ruta.with_suffix(".json.tmp")
        temporal.write_text(
            json.dumps(crudo, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporal.replace(ruta)
        _CACHE_DESTINOS["destinos"] = None

    # La configuración cambió: la prueba anterior ya no vale.
    _anotar_estado(ident, None)
    destino = cargar_destinos(ruta).get(ident)

    if destino is None:
        raise ValueError("El destino guardado no es válido.")

    return destino_publico(destino)


NOMBRES_PLATAFORMA = {
    "youtube_shorts": "YouTube Shorts", "tiktok": "TikTok",
    "facebook_reels": "Facebook Reels",
}


def guardar_tokens(red, tokens):
    """Guarda en destinos.json los tokens de una conexión OAuth completada.

    `red` es la clave de oauth.PLATAFORMAS. Usa el destino existente de esa
    plataforma o crea el de fábrica. Los tokens se cifran al guardarlos.
    """
    p = oauth.PLATAFORMAS[red]
    existente = next(
        (d for d in cargar_destinos().values()
         if d["plataforma"] == p["plataforma"]),
        None,
    )
    ident = existente["id"] if existente else p["destino"]
    cuerpo = {
        "id": ident,
        "plataforma": p["plataforma"],
        "nombre": NOMBRES_PLATAFORMA[p["plataforma"]],
        "activo": True,
        "reemplazar_credenciales": True,
        "credenciales": {k: v for k, v in tokens.items() if v},
    }
    return guardar_destino(cuerpo)


def token_vigente(dest):
    """Token de acceso listo para usar; lo renueva si está por caducar."""
    cred = dest["credenciales"]
    token = cred.get("access_token")

    if not token:
        raise ValueError("La cuenta no está conectada.")

    if not oauth.ha_caducado(cred.get("expires_at")):
        return token

    refresco = cred.get("refresh_token")

    if not refresco:
        raise RuntimeError(
            "La sesión caducó: vuelve a conectar la cuenta en Automatización."
        )

    red = next(
        k for k, v in oauth.PLATAFORMAS.items()
        if v["plataforma"] == dest["plataforma"]
    )

    try:
        nuevos = oauth.refrescar(red, refresco)
    except oauth.ErrorOAuth as error:
        raise RuntimeError(
            f"No se pudo renovar la sesión ({error}). "
            "Vuelve a conectar la cuenta."
        ) from None

    guardar_destino({
        "id": dest["id"], "plataforma": dest["plataforma"],
        "credenciales": nuevos,
    })
    return nuevos["access_token"]


def _verificar_token(plataforma, token):
    """Pregunta al API oficial de la plataforma si el token vale."""
    url = VERIFICACION.get(plataforma)

    if url is None:
        raise ValueError("Esta plataforma no se puede verificar con token.")

    solicitud = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(solicitud, timeout=10) as r:
            datos = json.loads(r.read(65536).decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        return False, (
            f"La plataforma rechazó el token (HTTP {error.code}). "
            "Puede haber caducado o faltarle permisos."
        )
    except (urllib.error.URLError, OSError, ValueError) as error:
        motivo = str(getattr(error, "reason", error))[:150]
        return False, f"No se pudo contactar con la plataforma: {motivo}"

    if not isinstance(datos, dict):
        return False, "Respuesta inesperada de la plataforma."

    if plataforma == "youtube_shorts":
        items = datos.get("items") or []

        if not items:
            return False, "Token válido, pero la cuenta no tiene canal."

        return True, "Canal: " + str(
            (items[0].get("snippet") or {}).get("title", "?")
        )[:80]

    if plataforma == "tiktok":
        error = datos.get("error") or {}

        if error.get("code") not in (None, "ok"):
            return False, "TikTok: " + str(error.get("message", "error"))[:120]

        usuario = (datos.get("data") or {}).get("user") or {}
        return True, "Cuenta: " + str(usuario.get("display_name", "?"))[:80]

    return True, "Página: " + str(datos.get("name", "?"))[:80]


def _enviar_webhook(url, carga):
    """POST JSON al webhook. Devuelve {ok, estado|error}."""
    try:
        solicitud = urllib.request.Request(
            url,
            data=json.dumps(carga, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )

        with urllib.request.urlopen(solicitud, timeout=10) as r:
            return {"ok": 200 <= r.status < 300, "estado": r.status}
    except (urllib.error.URLError, OSError, ValueError) as error:
        return {"ok": False, "error": str(error)[:200]}


def probar_destino(ident, modo, cargar=None):
    """Prueba un destino. modo: 'conexion' (token) o 'envio' (carpeta+webhook).

    Anota el resultado para mostrar el estado de la tarjeta.
    """
    dest = (cargar or cargar_destinos)().get(ident)

    if dest is None:
        raise LookupError("El destino no existe.")

    if modo == "conexion":
        if not dest["credenciales"].get("access_token"):
            raise ValueError("La cuenta no está conectada.")

        try:
            token = token_vigente(dest)
        except RuntimeError as error:
            token = None
            ok, detalle = False, str(error)[:200]

        if token:
            ok, detalle = _verificar_token(dest["plataforma"], token)
    elif modo == "envio":
        ok, detalle = True, ""
        carpeta = dest["carpeta"]

        try:
            carpeta.mkdir(parents=True, exist_ok=True)
            (carpeta / "_prueba_hubclips.txt").write_text(
                "Prueba de envío de Hubclips. Puedes borrar este archivo.\n",
                encoding="utf-8",
            )
            detalle = f"Archivo de prueba escrito en {carpeta}."
        except OSError as error:
            ok = False
            detalle = f"No se pudo escribir en la carpeta: {error.strerror}"

        if ok and dest["webhook"]:
            r = _enviar_webhook(
                dest["webhook"],
                {
                    "evento": "prueba",
                    "destino": dest["nombre"],
                    "plataforma": dest["plataforma"],
                },
            )
            ok = r["ok"]
            detalle += (
                f" Webhook respondió HTTP {r['estado']}."
                if "estado" in r and ok
                else f" Webhook falló: {r.get('error') or r.get('estado')}."
            )
    else:
        raise ValueError("Modo de prueba no válido.")

    entrada = {
        "ok": ok, "modo": modo, "detalle": detalle,
        "fecha": _iso(_ahora()),
    }
    _anotar_estado(ident, entrada)
    return {"ok": ok, "detalle": detalle, "destino": destino_publico(dest)}


# --------------------------------------------------------------------------
# Registro
# --------------------------------------------------------------------------

class Registro:
    """Lista persistente de publicaciones (JSON, escritura atómica)."""

    def __init__(self, ruta=None):
        self.ruta = Path(ruta or RUTA_REGISTRO)
        self._cerrojo = threading.RLock()

    def _leer(self):
        try:
            datos = json.loads(self.ruta.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            # Archivo dañado: se conserva una copia y se empieza de cero.
            try:
                self.ruta.replace(self.ruta.with_suffix(".corrupto"))
            except OSError:
                pass

            logging.warning("publicaciones.json dañado; copia en .corrupto")
            return []

        return datos if isinstance(datos, list) else []

    def _guardar(self, lista):
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        temporal = self.ruta.with_suffix(".json.tmp")
        temporal.write_text(
            json.dumps(lista, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporal.replace(self.ruta)

    def listar(self):
        with self._cerrojo:
            return sorted(
                self._leer(), key=lambda p: p.get("creado", ""), reverse=True
            )

    def obtener(self, identificador):
        with self._cerrojo:
            return next(
                (p for p in self._leer() if p.get("id") == identificador),
                None,
            )

    def agregar(self, pub):
        with self._cerrojo:
            lista = self._leer()
            lista.append(pub)
            self._guardar(lista)

    def actualizar(self, identificador, **campos):
        with self._cerrojo:
            lista = self._leer()

            for pub in lista:
                if pub.get("id") == identificador:
                    pub.update(campos)
                    self._guardar(lista)
                    return dict(pub)

        return None

    def eliminar(self, identificador):
        with self._cerrojo:
            lista = self._leer()
            nueva = [p for p in lista if p.get("id") != identificador]

            if len(nueva) == len(lista):
                return False

            self._guardar(nueva)
            return True


# --------------------------------------------------------------------------
# Publicador (cola + programador)
# --------------------------------------------------------------------------

def _texto_linea(valor, maximo, campo, obligatorio=False):
    if not isinstance(valor, str):
        raise ValueError(f"{campo} no válido.")

    valor = " ".join(valor.split())

    if obligatorio and not valor:
        raise ValueError(f"{campo} es obligatorio.")

    if len(valor) > maximo:
        raise ValueError(f"{campo} supera {maximo} caracteres.")

    return valor


def _parsear_fecha(valor):
    if not isinstance(valor, str):
        raise ValueError("Fecha no válida.")

    try:
        return datetime.fromisoformat(valor.strip()).replace(tzinfo=None)
    except ValueError:
        raise ValueError("Fecha no válida (usa AAAA-MM-DDTHH:MM).")


class Publicador:
    def __init__(self, carpeta_clips, registro=None, cargar=cargar_destinos,
                 intervalo=10):
        self.carpeta_clips = Path(carpeta_clips)
        self.registro = registro or Registro()
        self._cargar = cargar
        self.intervalo = intervalo
        self._despertar = threading.Event()
        self._parar = threading.Event()
        self._hilo = None

    # ---- ciclo de vida ----------------------------------------------------
    def iniciar(self):
        # Una exportación interrumpida por un cierre se reintenta ahora.
        for pub in self.registro.listar():
            if pub.get("estado") == "exportando":
                self.registro.actualizar(
                    pub["id"], estado="programado",
                    programado_para=_iso(_ahora()),
                    aviso="Reanudado tras un reinicio del panel.",
                )

        self._hilo = threading.Thread(
            target=self._bucle, name="publicador", daemon=True
        )
        self._hilo.start()

    def detener(self):
        self._parar.set()
        self._despertar.set()

    def destinos(self):
        return self._cargar()

    # ---- operaciones ------------------------------------------------------
    def crear(self, clip, destino, titulo, descripcion, hashtags, modo,
              programado_para=None, estilo=None, ajuste=None):
        from analisis_local import leer_metadatos

        if not isinstance(clip, str) or not re.fullmatch(
            r"clip_[0-9A-Za-z_]+", clip
        ):
            raise ValueError("Clip no válido.")

        mkv = self.carpeta_clips / f"{clip}.mkv"

        if not mkv.is_file():
            raise LookupError("El clip no existe.")

        meta = leer_metadatos(mkv)

        if meta.get("subtitulos") == "pendiente" and not mkv.with_suffix(
            ".vtt"
        ).is_file():
            raise RuntimeError(
                "El clip aún no tiene subtítulos. Espera a que la IA termine "
                "(pestaña «En proceso»)."
            )

        destinos = self._cargar()
        dest = destinos.get(destino) if isinstance(destino, str) else None

        if dest is None:
            raise LookupError("El destino no existe.")

        if not dest["activo"]:
            raise ValueError(
                f"{dest['nombre']} está desactivado: actívalo en "
                "Automatización."
            )

        titulo = _texto_linea(titulo, dest["titulo_max"], "El título", True)
        descripcion = (descripcion or "")

        if not isinstance(descripcion, str):
            raise ValueError("Descripción no válida.")

        descripcion = descripcion.replace("\r", "").strip()

        if len(descripcion) > dest["descripcion_max"]:
            raise ValueError(
                f"La descripción supera {dest['descripcion_max']} caracteres."
            )

        etiquetas = normalizar_hashtags(hashtags or [])

        ahora = _ahora()

        if modo == "borrador":
            cuando = ahora
        elif modo == "programar":
            cuando = _parsear_fecha(programado_para)

            if cuando < ahora - timedelta(minutes=1):
                raise ValueError("La fecha programada ya pasó.")

            if cuando > ahora + timedelta(days=366):
                raise ValueError("La fecha programada es demasiado lejana.")
        else:
            raise ValueError("Modo no válido.")

        # Lo elegido en la web manda; si no llega, la preferencia del clip.
        pref = meta.get("preferencias")
        pref = pref if isinstance(pref, dict) else {}

        if estilo is not None and estilo not in ESTILOS:
            raise ValueError("Plantilla de subtítulos no válida.")

        if ajuste is not None and ajuste not in AJUSTES:
            raise ValueError("Ajuste 9:16 no válido.")

        estilo = estilo or pref.get("estilo", ESTILO_POR_DEFECTO)
        ajuste = ajuste or pref.get("ajuste", "adaptar")

        pub = {
            "id": uuid.uuid4().hex[:8],
            "clip": clip,
            "destino": dest["id"],
            "destino_nombre": dest["nombre"],
            "plataforma": dest["plataforma"],
            "titulo": titulo,
            "descripcion": descripcion,
            "hashtags": etiquetas,
            "estilo": estilo if estilo in ESTILOS else ESTILO_POR_DEFECTO,
            "ajuste": ajuste if ajuste in AJUSTES else "adaptar",
            "modo": modo,
            "estado": "programado",
            "creado": _iso(ahora),
            "programado_para": _iso(cuando),
            "terminado": None,
            "archivo": None,
            "error": None,
            "webhook": None,
        }

        self.registro.agregar(pub)
        self._despertar.set()
        return pub

    def cancelar(self, identificador):
        pub = self.registro.obtener(identificador)

        if pub is None:
            raise LookupError("La publicación no existe.")

        if pub["estado"] != "programado":
            raise RuntimeError("Solo se pueden cancelar las programadas.")

        return self.registro.actualizar(identificador, estado="cancelado")

    def reintentar(self, identificador):
        pub = self.registro.obtener(identificador)

        if pub is None:
            raise LookupError("La publicación no existe.")

        if pub["estado"] not in ("error", "cancelado"):
            raise RuntimeError("Solo se reintentan las fallidas o canceladas.")

        self._despertar.set()
        return self.registro.actualizar(
            identificador, estado="programado", error=None,
            programado_para=_iso(_ahora()),
        )

    def eliminar(self, identificador):
        pub = self.registro.obtener(identificador)

        if pub is None:
            raise LookupError("La publicación no existe.")

        if pub["estado"] == "exportando":
            raise RuntimeError("Se está exportando; inténtalo al terminar.")

        # Solo se borra el registro: los archivos entregados no se tocan.
        self.registro.eliminar(identificador)

    def ruta_archivo(self, identificador):
        """Archivo entregado, solo si está dentro de una carpeta de destino."""
        pub = self.registro.obtener(identificador)

        if not pub or not pub.get("archivo"):
            return None

        ruta = Path(pub["archivo"]).resolve()

        for dest in self._cargar().values():
            try:
                ruta.relative_to(dest["carpeta"].resolve())
            except ValueError:
                continue

            return ruta if ruta.is_file() else None

        return None

    # ---- ejecución --------------------------------------------------------
    def _bucle(self):
        while not self._parar.is_set():
            ahora = _ahora()

            for pub in self.registro.listar()[::-1]:   # las más antiguas antes
                if self._parar.is_set():
                    return

                if pub.get("estado") != "programado":
                    continue

                try:
                    cuando = _parsear_fecha(pub.get("programado_para"))
                except ValueError:
                    cuando = ahora

                if cuando <= ahora:
                    self._ejecutar(pub)

            self._despertar.wait(self.intervalo)
            self._despertar.clear()

    def _exportar_esperando(self, mkv, ajuste, estilo, maximo_s=900):
        """exportar() que espera si la cola de renders está llena."""
        limite = time.monotonic() + maximo_s

        while True:
            try:
                return exportar(mkv, ajuste, estilo)
            except ColaLlena:
                if self._parar.is_set() or time.monotonic() > limite:
                    raise

                time.sleep(5)

    def _ejecutar(self, pub):
        ident = pub["id"]
        self.registro.actualizar(
            ident, estado="exportando", inicio=_iso(_ahora()), error=None
        )

        try:
            dest = self._cargar().get(pub["destino"])

            if dest is None:
                raise LookupError("El destino ya no está en destinos.json.")

            if not dest["activo"]:
                raise ValueError("El destino está desactivado.")

            mkv = self.carpeta_clips / f"{pub['clip']}.mkv"

            if not mkv.is_file():
                raise FileNotFoundError("El clip ya no existe.")

            mp4 = self._exportar_esperando(mkv, pub["ajuste"], pub["estilo"])

            carpeta = dest["carpeta"]
            carpeta.mkdir(parents=True, exist_ok=True)
            base = f"{pub['clip']}_{ident}"
            final = carpeta / f"{base}.mp4"
            temporal = carpeta / f".{base}.mp4.tmp"

            shutil.copy2(mp4, temporal)
            temporal.replace(final)

            etiquetas = " ".join(pub["hashtags"])
            partes = [pub["titulo"], pub["descripcion"], etiquetas]
            (carpeta / f"{base}.txt").write_text(
                "\n\n".join(p for p in partes if p) + "\n", encoding="utf-8"
            )

            paquete = {
                "id": ident,
                "clip": pub["clip"],
                "plataforma": pub["plataforma"],
                "destino": dest["nombre"],
                "titulo": pub["titulo"],
                "descripcion": pub["descripcion"],
                "hashtags": pub["hashtags"],
                "programado_para": pub["programado_para"],
                "formato": "vertical 9:16 1080x1920 con subtítulos quemados",
                "plantilla": pub["estilo"],
                "archivo": final.name,
            }
            (carpeta / f"{base}.json").write_text(
                json.dumps(paquete, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            webhook = self._avisar_webhook(dest, paquete, final)
            subida, error_subida = None, None

            if dest["credenciales"].get("access_token"):
                try:
                    token = token_vigente(dest)
                    subida = subida_redes.subir(
                        dest["plataforma"], dest["credenciales"], final,
                        pub["titulo"], pub["descripcion"], pub["hashtags"],
                        pub["modo"] == "borrador", token,
                    )
                    subida["ok"] = True
                except (subida_redes.ErrorSubida, RuntimeError,
                        ValueError, OSError) as error:
                    error_subida = (
                        f"La subida a {dest['nombre']} falló "
                        f"(el paquete sí se entregó en la carpeta): "
                        f"{str(error)[:220]}"
                    )

            self.registro.actualizar(
                ident, estado="error" if error_subida else "listo",
                terminado=_iso(_ahora()), archivo=str(final),
                webhook=webhook, subida=subida, error=error_subida,
            )
            logging.info("Publicación %s lista en %s", ident, final)

        except Exception as error:
            logging.exception("Publicación %s fallida", ident)
            self.registro.actualizar(
                ident, estado="error", terminado=_iso(_ahora()),
                error=str(error)[:300] or error.__class__.__name__,
            )

    @staticmethod
    def _avisar_webhook(dest, paquete, archivo):
        if not dest["webhook"]:
            return None

        # El paquete ya está entregado; un fallo solo se anota.
        return _enviar_webhook(
            dest["webhook"],
            {"evento": "clip_listo", **paquete, "ruta": str(archivo)},
        )
