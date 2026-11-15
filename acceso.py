"""Acceso con contraseña para el panel cuando se despliega fuera de tu equipo.

En local (127.0.0.1) el panel no pide nada. Al desplegarlo en la nube (host
0.0.0.0) el panel exige HUBCLIPS_PASSWORD: sin ella no arranca, porque
cualquiera con la URL podría ver, publicar o borrar tus clips.

Variables de entorno:
    HUBCLIPS_PASSWORD     contraseña de acceso (mín. 12 caracteres)
    HUBCLIPS_SESSION_KEY  clave para firmar la cookie de sesión; si falta se
                          usa HUBCLIPS_SECRET_KEY, y si tampoco, una al azar
                          (las sesiones se cierran al reiniciar)
"""
import base64
import hashlib
import hmac
import os
import secrets
import threading
import time

COOKIE = "hubclips_sesion"
DURACION_SESION = 12 * 3600
MIN_PASSWORD = 12

# Intentos fallidos permitidos: por IP y en total, en la ventana indicada.
VENTANA = 300
MAX_POR_IP = 6
MAX_TOTAL = 40


class Acceso:
    def __init__(self):
        self.password = os.environ.get("HUBCLIPS_PASSWORD", "")
        clave = (
            os.environ.get("HUBCLIPS_SESSION_KEY")
            or os.environ.get("HUBCLIPS_SECRET_KEY")
            or secrets.token_hex(32)
        )
        self._clave = clave.encode("utf-8")
        self._fallos = []          # (momento, ip)
        self._cerrojo = threading.Lock()

    @property
    def activo(self):
        return bool(self.password)

    # -- contraseña ---------------------------------------------------------

    def bloqueado(self, ip):
        ahora = time.time()

        with self._cerrojo:
            self._fallos = [f for f in self._fallos if ahora - f[0] < VENTANA]
            de_ip = sum(1 for f in self._fallos if f[1] == ip)
            return de_ip >= MAX_POR_IP or len(self._fallos) >= MAX_TOTAL

    def comprobar(self, candidata, ip):
        """True si la contraseña es correcta. Registra los fallos."""
        if self.bloqueado(ip):
            return False

        # Comparación en tiempo constante sobre resúmenes de igual longitud.
        a = hashlib.sha256((candidata or "").encode("utf-8")).digest()
        b = hashlib.sha256(self.password.encode("utf-8")).digest()

        if hmac.compare_digest(a, b):
            return True

        with self._cerrojo:
            self._fallos.append((time.time(), ip))

        return False

    # -- sesión (cookie firmada, sin estado en el servidor) -----------------

    def _firma(self, texto):
        return hmac.new(
            self._clave, texto.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def nueva_sesion(self):
        caduca = int(time.time()) + DURACION_SESION
        cuerpo = f"{caduca}.{secrets.token_hex(8)}"
        return f"{cuerpo}.{self._firma(cuerpo)}"

    def sesion_valida(self, valor):
        try:
            caduca, nonce, firma = (valor or "").split(".")
            if not hmac.compare_digest(firma, self._firma(f"{caduca}.{nonce}")):
                return False
            return int(caduca) > time.time()
        except (ValueError, TypeError):
            return False


def cookie_de(cabecera):
    """Valor de nuestra cookie dentro de la cabecera Cookie."""
    for trozo in (cabecera or "").split(";"):
        nombre, _, valor = trozo.strip().partition("=")

        if nombre == COOKIE:
            return valor

    return None


def pagina_login(error=""):
    """HTML de la pantalla de acceso (autocontenida, sin recursos externos)."""
    import html

    aviso = (
        f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
    )
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Hubclips · Acceso</title>
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<style>
:root {{ color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 20px;
  font: 15px/1.5 system-ui, "Segoe UI", sans-serif; color: #eef0f6;
  background: radial-gradient(800px 400px at 80% -10%, rgba(124,92,255,.18), transparent 60%), #0b0d12; }}
form {{ width: min(380px, 100%); background: #171b24; border: 1px solid #232837; border-radius: 18px;
  padding: 28px 24px; box-shadow: 0 20px 60px rgba(0,0,0,.5); }}
h1 {{ margin: 0 0 4px; font-size: 22px; letter-spacing: -.02em; }}
p.sub {{ margin: 0 0 20px; color: #8b93a7; font-size: 13px; }}
label {{ display: block; font-size: 12px; color: #8b93a7; margin-bottom: 6px; }}
input {{ width: 100%; padding: 12px 14px; font: inherit; color: inherit; background: #0f121a;
  border: 1px solid #232837; border-radius: 10px; outline: 0; }}
input:focus {{ border-color: #7c5cff; box-shadow: 0 0 0 3px rgba(124,92,255,.25); }}
button {{ width: 100%; margin-top: 16px; padding: 12px; font: inherit; font-weight: 600; color: #fff;
  border: 0; border-radius: 10px; cursor: pointer;
  background: linear-gradient(135deg, #7c5cff, #4f8cff); }}
button:hover {{ filter: brightness(1.1); }}
.err {{ margin: 12px 0 0; color: #ff9a9a; font-size: 13px; }}
</style></head>
<body>
<form method="post" action="/login" autocomplete="off">
  <h1>Hubclips</h1>
  <p class="sub">Panel privado. Introduce la contraseña de acceso.</p>
  <label for="p">Contraseña</label>
  <input id="p" name="password" type="password" required autofocus autocomplete="current-password">
  <button type="submit">Entrar</button>
  {aviso}
</form></body></html>"""
