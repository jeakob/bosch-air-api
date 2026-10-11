FROM python:3.13-alpine
LABEL org.opencontainers.image.source=https://github.com/jeakob/bosch-air-api \
      org.opencontainers.image.description="Bosch Air 2000i ring-light scheduler dashboard"
RUN pip install --no-cache-dir "paho-mqtt>=2,<3" tzdata \
 && adduser -D -u 1000 app && mkdir /data && chown app:app /data
WORKDIR /app
COPY app.py bosch_air.py discover.py snapshot.py index.html ./
ENV DATA_DIR=/data PORT=8080 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER app
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s CMD wget -qO- "http://127.0.0.1:$PORT/healthz" >/dev/null || exit 1
CMD ["python3", "app.py"]
