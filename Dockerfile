FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/static/covers /data

VOLUME ["/data", "/app/static/covers", "/roms"]

EXPOSE 8080

ENV HOST=0.0.0.0 \
    PORT=8080 \
    DEBUG=false \
    DB_URL=sqlite:////data/rom_finder.db

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
