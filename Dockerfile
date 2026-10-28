FROM python:3.12-slim

LABEL org.opencontainers.image.title="lumen-push-relay" \
      org.opencontainers.image.version="1.2.2"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY relay.py .
COPY src/ ./src/

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python3", "relay.py"]
