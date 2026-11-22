FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOOKOUT_CONFIG=/config/config.yaml \
    LOOKOUT_DATA=/data \
    LOOKOUT_STATE_FILE=/data/state.json \
    LOOKOUT_PORT=18099

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY lookout ./lookout

EXPOSE 18099
VOLUME ["/data", "/config"]
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('LOOKOUT_PORT','18099'), timeout=4)" || exit 1
ENTRYPOINT ["python", "-m", "lookout"]
