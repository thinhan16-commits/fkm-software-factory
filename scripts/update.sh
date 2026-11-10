#!/usr/bin/env bash
# ==============================================================================
# FKM SOFTWARE FACTORY — SCRIPT CẬP NHẬT & KHỞI ĐỘNG LẠI (UPDATE & RESTART)
# ==============================================================================
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo -e "${CYAN}🔄 Đang cập nhật mã nguồn FKM Software Factory từ git...${NC}"
git pull --rebase origin main

echo -e "${YELLOW}🛑 Dừng các dịch vụ đang chạy...${NC}"
./scripts/stop.sh

echo -e "${GREEN}⚡ Khởi động lại các dịch vụ (Daemon Mode)...${NC}"
./scripts/start.sh --daemon

echo -e "${GREEN}✓ Hoàn tất cập nhật và khởi động lại!${NC}"
