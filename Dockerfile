FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 AIHUB_DATA_DIR=/data
WORKDIR /app
COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt
COPY aihub ./aihub
COPY docs ./docs
RUN useradd -r -u 10001 aihub && mkdir /data && chown aihub /data
USER aihub
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request as u;u.urlopen('http://127.0.0.1:8000/api/v1/healthz',timeout=2)"
CMD ["python","-m","aihub.server","--host","0.0.0.0","--port","8000","--data","/data"]
