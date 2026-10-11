FROM python:3.13-slim
RUN pip install --no-cache-dir "paho-mqtt>=2,<3" tzdata \
 && useradd --uid 1000 --create-home app && mkdir /data && chown app:app /data
WORKDIR /app
COPY app.py bosch_air.py discover.py snapshot.py index.html ./
ENV DATA_DIR=/data PORT=8080 PYTHONUNBUFFERED=1
USER app
VOLUME /data
EXPOSE 8080
HEALTHCHECK CMD python3 -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/healthz', timeout=5)" || exit 1
CMD ["python3", "app.py"]
