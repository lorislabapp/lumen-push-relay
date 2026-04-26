FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY relay.py .
COPY src/ ./src/

ENV PYTHONUNBUFFERED=1

# Auto-call REST sync endpoint (Doorbell Call Mode). The iOS app POSTs
# AutoCallConfig here so the relay knows which device tokens want VoIP.
EXPOSE 8765

ENTRYPOINT ["python3", "relay.py"]
