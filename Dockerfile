FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOOKOUT_CONFIG=/config/config.yaml \
    LOOKOUT_STATE_FILE=/data/state.json

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY lookout ./lookout

VOLUME ["/data", "/config"]
ENTRYPOINT ["python", "-m", "lookout"]
