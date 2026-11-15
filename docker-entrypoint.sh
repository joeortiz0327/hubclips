#!/bin/sh
# Los discos persistentes suelen montarse como root: se ajusta el dueño y
# después el panel (que ejecuta yt-dlp y FFmpeg sobre URLs externas) corre
# sin privilegios.
set -e

if [ "$(id -u)" = "0" ]; then
    mkdir -p "${HUBCLIPS_DATA_DIR:-/data}"
    chown -R app:app "${HUBCLIPS_DATA_DIR:-/data}" 2>/dev/null || true
    exec setpriv --reuid=app --regid=app --init-groups python panel_web.py "$@"
fi

exec python panel_web.py "$@"
