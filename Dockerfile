# Hubclips en la nube (Render, Fly, un VPS...). Imagen con FFmpeg incluido.
FROM python:3.12-slim

# ffmpeg/ffprobe: grabación, miniaturas y render 9:16. Las fuentes dan
# subtítulos legibles al quemarlos (libass usa fontconfig; Arial/Impact no
# existen en Linux y se sustituyen por la más parecida instalada).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg fontconfig fonts-liberation fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN sed -i 's/\r$//' docker-entrypoint.sh \
    && chmod +x docker-entrypoint.sh \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /data \
    && chown -R app:app /app /data

# Todo lo generado (clips, registros, destinos.json con tokens cifrados y la
# caché del modelo Whisper) vive en /data: monta ahí un disco persistente.
ENV PYTHONUNBUFFERED=1 \
    HUBCLIPS_DATA_DIR=/data \
    HF_HOME=/data/modelos \
    PORT=10000

# Render inyecta PORT (10000 por defecto) y el panel escucha en 0.0.0.0:$PORT.
EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','10000'), timeout=4)" || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
