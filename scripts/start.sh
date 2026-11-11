#!/usr/bin/env bash
# ==============================================================================
# FKM SOFTWARE FACTORY — SCRIPT KHỞI CHẠY (START SERVICES)
# ==============================================================================
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Nạp file cấu hình .env nếu có
if [ -f "$ROOT_DIR/.env" ]; then
    set -a
    source "$ROOT_DIR/.env"
    set +a
fi

BRIDGE_PORT="${BRIDGE_PORT:-8001}"
FACTORY_PORT="${FACTORY_PORT:-8090}"
FACTORY_HOST="${FACTORY_HOST:-0.0.0.0}"
if [ -f "$ROOT_DIR/.venv/bin/python" ]; then
    VENV_PYTHON="$ROOT_DIR/.venv/bin/python"
elif command -v python3 &>/dev/null; then
    VENV_PYTHON="python3"
elif command -v python &>/dev/null; then
    VENV_PYTHON="python"
else
    echo -e "${RED}❌ Chưa tìm thấy môi trường Python! Vui lòng chạy './scripts/install.sh' trước.${NC}"
    exit 1
fi

mkdir -p "$ROOT_DIR/logs"

# 1. Khởi chạy Bridge nếu cần (hoặc dùng Bridge từ xa trên Mac Mini)
if [ -n "$FACTORY_BRIDGE_HOST" ] && [ "$FACTORY_BRIDGE_HOST" != "127.0.0.1" ] && [ "$FACTORY_BRIDGE_HOST" != "localhost" ]; then
    echo -e "${GREEN}✓ Sử dụng Multi-Provider Web Bridge từ xa trên Mac Mini: http://$FACTORY_BRIDGE_HOST:$BRIDGE_PORT${NC}"
elif [ "$START_LOCAL_BRIDGE" = "0" ] || [ "$START_LOCAL_BRIDGE" = "false" ] || [ "$NO_BRIDGE" = "1" ]; then
    echo -e "${YELLOW}ℹ️ Bỏ qua khởi động Bridge cục bộ trên máy này (theo cấu hình START_LOCAL_BRIDGE=false).${NC}"
else
    echo -e "${CYAN}🔍 Kiểm tra Multi-Provider Web Bridge (Cổng :$BRIDGE_PORT)...${NC}"
    if lsof -nP -iTCP:"$BRIDGE_PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
        echo -e "${GREEN}✓ Bridge đã đang chạy trên cổng :$BRIDGE_PORT.${NC}"
    else
        echo -e "${YELLOW}⚡ Đang khởi động Bridge trên cổng :$BRIDGE_PORT...${NC}"
        (
            cd "$ROOT_DIR/bridge"
            export PYTHONPATH="$ROOT_DIR/bridge:$ROOT_DIR"
            export BRIDGE_PORT="$BRIDGE_PORT"
            export BRIDGE_PROFILES_DIR="$ROOT_DIR/bridge/profiles"
            nohup "$VENV_PYTHON" server.py >> "$ROOT_DIR/logs/bridge.log" 2>&1 &
            echo $! > "$ROOT_DIR/logs/bridge.pid"
        )
        sleep 2
        if lsof -nP -iTCP:"$BRIDGE_PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
            echo -e "${GREEN}✓ Bridge đã khởi động thành công (PID: $(cat "$ROOT_DIR/logs/bridge.pid 2>/dev/null || echo '?'))!${NC}"
        else
            echo -e "${YELLOW}⚠️ Bridge đang khởi động nền... Xem log tại: logs/bridge.log${NC}"
        fi
    fi
fi

# 2. Khởi chạy Factory Web Server
echo -e "\n${CYAN}🏭 Khởi chạy FKM Software Factory (Cổng :$FACTORY_PORT)...${NC}"
if lsof -nP -iTCP:"$FACTORY_PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo -e "${RED}⚠️ Cổng $FACTORY_PORT đang bị chiếm dụng bởi tiến trình khác!${NC}"
    echo "Dùng lệnh './scripts/stop.sh' để dừng tiến trình cũ trước khi chạy lại."
    exit 1
fi

export PYTHONPATH="$ROOT_DIR/factory:$ROOT_DIR"
export FACTORY_ROOT="$ROOT_DIR"
export FACTORY_PORT="$FACTORY_PORT"
export FACTORY_BRIDGE_PORT="$BRIDGE_PORT"

if [ "$1" == "--daemon" ] || [ "$1" == "-d" ]; then
    (
        cd "$ROOT_DIR/factory"
        nohup "$VENV_PYTHON" factory_server.py >> "$ROOT_DIR/logs/factory.log" 2>&1 &
        echo $! > "$ROOT_DIR/logs/factory.pid"
    )
    echo -e "${GREEN}✓ Cả Bridge và Factory đã được khởi chạy ngầm (Daemon Mode)!${NC}"
    echo -e "Trình duyệt: ${PURPLE}http://localhost:$FACTORY_PORT${NC}"
    echo -e "Theo dõi log: ${CYAN}tail -f logs/factory.log${NC}"
else
    echo -e "${GREEN}==================================================================${NC}"
    echo -e "${GREEN}✨ FKM SOFTWARE FACTORY ĐANG HOẠT ĐỘNG!${NC}"
    echo -e "🌐 Giao diện Web: ${PURPLE}http://localhost:$FACTORY_PORT${NC}"
    echo -e "📌 Bấm [Ctrl + C] để dừng máy chủ bất cứ lúc nào."
    echo -e "${GREEN}==================================================================${NC}\n"
    cd "$ROOT_DIR/factory"
    exec "$VENV_PYTHON" factory_server.py
fi
