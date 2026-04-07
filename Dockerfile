FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run behind a load balancer / reverse proxy; use a high port in-container.
ENV GENESYS_LISTEN_HOST=0.0.0.0 \
    GENESYS_LISTEN_PORT=8080 \
    GENESYS_PATH=/audiohook

EXPOSE 8080

CMD ["python", "main.py"]

