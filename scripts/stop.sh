#!/usr/bin/env bash
# ==============================================================================
# FKM SOFTWARE FACTORY — SCRIPT DỪNG HỆ THỐNG (STOP SERVICES)
# ==============================================================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ -f "$ROOT_DIR/.env" ]; then
    set -a
    source "$ROOT_DIR/.env"
    set +a
fi

BRIDGE_PORT="${BRIDGE_PORT:-8001}"
FACTORY_PORT="${FACTORY_PORT:-8090}"

echo -e "${YELLOW}🛑 Đang dừng FKM Software Factory & Multi-Web Bridge...${NC}"

# 1. Dừng Factory
FACTORY_PID=$(lsof -nP -iTCP:"$FACTORY_PORT" -sTCP:LISTEN -t 2>/dev/null || true)
if [ -n "$FACTORY_PID" ]; then
    echo "Dừng Factory Server (PID: $FACTORY_PID, Cổng :$FACTORY_PORT)..."
    kill "$FACTORY_PID" 2>/dev/null || true
    sleep 1
    if kill -0 "$FACTORY_PID" 2>/dev/null; then
        kill -9 "$FACTORY_PID" 2>/dev/null || true
    fi
    echo -e "${GREEN}✓ Đã dừng Factory Server.${NC}"
else
    echo "Factory Server không hoạt động."
fi
rm -f "$ROOT_DIR/logs/factory.pid"

# 2. Dừng Bridge
BRIDGE_PID=$(lsof -nP -iTCP:"$BRIDGE_PORT" -sTCP:LISTEN -t 2>/dev/null || true)
if [ -n "$BRIDGE_PID" ]; then
    echo "Dừng Multi-Web Bridge (PID: $BRIDGE_PID, Cổng :$BRIDGE_PORT)..."
    kill "$BRIDGE_PID" 2>/dev/null || true
    sleep 1
    if kill -0 "$BRIDGE_PID" 2>/dev/null; then
        kill -9 "$BRIDGE_PID" 2>/dev/null || true
    fi
    echo -e "${GREEN}✓ Đã dừng Multi-Web Bridge.${NC}"
else
    echo "Multi-Web Bridge không hoạt động."
fi
rm -f "$ROOT_DIR/logs/bridge.pid"

echo -e "${GREEN}✅ Đã dừng toàn bộ dịch vụ an toàn.${NC}\n"
