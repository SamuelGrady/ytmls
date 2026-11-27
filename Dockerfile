FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    YTMLS_DATA_DIR=/data \
    YTMLS_MUSIC_DIR=/music

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Run as an unprivileged user. PUID/PGID can be matched to the owner of
# your Jellyfin music folder with `user:` in compose.yaml.
RUN useradd --system --uid 1000 --home /app ytmls \
 && mkdir -p /data /music && chown ytmls:ytmls /data /music
USER ytmls

VOLUME ["/data", "/music"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else 1)"

CMD ["uvicorn", "app.main:get_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-server-header"]
