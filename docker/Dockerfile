FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    FACTORY_ROOT=/app \
    FACTORY_HOST=0.0.0.0 \
    FACTORY_PORT=8090 \
    FACTORY_BRIDGE_HOST=127.0.0.1 \
    FACTORY_BRIDGE_PORT=8001 \
    BRIDGE_HOST=0.0.0.0 \
    BRIDGE_PORT=8001 \
    BRIDGE_PROFILES_DIR=/app/bridge/profiles

# Cài đặt thêm các công cụ hệ thống cần thiết
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl net-tools procps \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x scripts/*.sh

EXPOSE 8090 8001

CMD ["./scripts/start.sh"]
