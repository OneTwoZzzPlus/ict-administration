FROM python:3.13-slim

RUN pip install --no-cache-dir fastapi uvicorn

COPY webhook.py /app/webhook.py

WORKDIR /app

CMD ["uvicorn", "webhook:app", "--host", "0.0.0.0", "--port", "8080"]