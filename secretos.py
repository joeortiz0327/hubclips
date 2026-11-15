"""Protección de tokens en reposo.

En Windows se cifran con DPAPI (CryptProtectData): solo el mismo usuario de
Windows, en este equipo, puede descifrarlos. Quien copie `destinos.json`
no obtiene tokens utilizables. En Linux/nube se cifran con Fernet (AES) usando
la variable de entorno HUBCLIPS_SECRET_KEY (mín. 24 caracteres; si se pierde
o cambia, los tokens guardados dejan de servir y hay que reconectar las
cuentas). Sin esa clave quedan en claro (prefijo `plano:`).

Un valor sin prefijo se trata como texto en claro (compatibilidad).
"""
import base64
import ctypes
import hashlib
import logging
import os
import sys
from ctypes import wintypes

PREFIJO_DPAPI = "dpapi:"
PREFIJO_FERNET = "fernet:"
PREFIJO_PLANO = "plano:"


class _Blob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi(funcion, datos):
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    memoria = ctypes.create_string_buffer(datos, len(datos))
    entrada = _Blob(
        len(datos), ctypes.cast(memoria, ctypes.POINTER(ctypes.c_char))
    )
    salida = _Blob()

    if funcion == "proteger":
        correcto = crypt32.CryptProtectData(
            ctypes.byref(entrada), None, None, None, None, 0,
            ctypes.byref(salida),
        )
    else:
        correcto = crypt32.CryptUnprotectData(
            ctypes.byref(entrada), None, None, None, None, 0,
            ctypes.byref(salida),
        )

    if not correcto:
        raise OSError("DPAPI no pudo procesar el valor.")

    try:
        return ctypes.string_at(salida.pbData, salida.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(salida.pbData, ctypes.c_void_p))


def _fernet():
    """Cifrado con HUBCLIPS_SECRET_KEY (Linux/nube). None si no se puede."""
    clave = os.environ.get("HUBCLIPS_SECRET_KEY", "")

    if len(clave) < 24:
        return None

    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None

    derivada = hashlib.sha256(clave.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(derivada))


def proteger(texto):
    if sys.platform == "win32":
        cifrado = _dpapi("proteger", texto.encode("utf-8"))
        return PREFIJO_DPAPI + base64.b64encode(cifrado).decode("ascii")

    f = _fernet()

    if f is not None:
        return PREFIJO_FERNET + f.encrypt(texto.encode("utf-8")).decode("ascii")

    logging.warning(
        "Sin HUBCLIPS_SECRET_KEY: los tokens se guardan sin cifrar."
    )
    return PREFIJO_PLANO + texto


def desproteger(valor):
    """Texto en claro, o None si no se puede descifrar (otro usuario/equipo)."""
    if not isinstance(valor, str):
        return None

    if valor.startswith(PREFIJO_DPAPI):
        if sys.platform != "win32":
            return None

        try:
            claro = _dpapi(
                "desproteger", base64.b64decode(valor[len(PREFIJO_DPAPI):])
            )
            return claro.decode("utf-8")
        except (OSError, ValueError):
            return None

    if valor.startswith(PREFIJO_FERNET):
        f = _fernet()

        try:
            return f.decrypt(valor[len(PREFIJO_FERNET):].encode("ascii")).decode(
                "utf-8"
            ) if f is not None else None
        except Exception:  # clave distinta o dato dañado
            return None

    if valor.startswith(PREFIJO_PLANO):
        return valor[len(PREFIJO_PLANO):]

    return valor
