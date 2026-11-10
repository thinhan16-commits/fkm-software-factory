#!/usr/bin/env bash
# ==============================================================================
# FKM SOFTWARE FACTORY — SCRIPT KIỂM TRA TRẠNG THÁI (HEALTH CHECK)
# ==============================================================================
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -f "$ROOT_DIR/.env" ]; then
    set -a
    source "$ROOT_DIR/.env"
    set +a
fi

BRIDGE_PORT="${BRIDGE_PORT:-8001}"
FACTORY_PORT="${FACTORY_PORT:-8090}"

echo -e "${PURPLE}==================================================================${NC}"
echo -e "${CYAN}📊 TRẠNG THÁI HỆ THỐNG FKM SOFTWARE FACTORY${NC}"
echo -e "${PURPLE}==================================================================${NC}"

# Kiểm tra Factory Server
FACTORY_PID=$(lsof -nP -iTCP:"$FACTORY_PORT" -sTCP:LISTEN -t 2>/dev/null || true)
if [ -n "$FACTORY_PID" ]; then
    echo -e "🏭 ${GREEN}Factory Web Server:${NC} ĐANG CHẠY (Port: $FACTORY_PORT, PID: $FACTORY_PID)"
    echo -e "   🔗 Web URL: ${PURPLE}http://localhost:$FACTORY_PORT${NC}"
else
    echo -e "🏭 ${RED}Factory Web Server:${NC} ĐÃ DỪNG (Port: $FACTORY_PORT)"
fi

# Kiểm tra Bridge
BRIDGE_PID=$(lsof -nP -iTCP:"$BRIDGE_PORT" -sTCP:LISTEN -t 2>/dev/null || true)
if [ -n "$BRIDGE_PID" ]; then
    echo -e "🌉 ${GREEN}Multi-Web Bridge:${NC}   ĐANG CHẠY (Port: $BRIDGE_PORT, PID: $BRIDGE_PID)"
    # Thử lấy danh sách model
    MODELS=$(curl -s --connect-timeout 2 "http://127.0.0.1:$BRIDGE_PORT/v1/models?provider=chatgpt" 2>/dev/null || echo "")
    if [[ "$MODELS" == *"GPT"* ]] || [[ "$MODELS" == *"chatgpt"* ]]; then
        echo -e "   🤖 Trạng thái AI: ${GREEN}Đã kết nối ChatGPT thành công${NC}"
    else
        echo -e "   🤖 Trạng thái AI: ${YELLOW}Đang chờ đăng nhập hoặc khởi tạo${NC}"
    fi
else
    echo -e "🌉 ${RED}Multi-Web Bridge:${NC}   ĐÃ DỪNG (Port: $BRIDGE_PORT)"
fi

# Thống kê thư mục dự án
PROJ_COUNT=$(find "$ROOT_DIR/projects" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l | tr -d ' ' || echo "0")
echo -e "📁 ${CYAN}Dự án đã tạo:${NC}       $PROJ_COUNT dự án trong thư mục projects/"
echo -e "${PURPLE}==================================================================${NC}\n"
