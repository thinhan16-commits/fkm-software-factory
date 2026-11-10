#!/usr/bin/env bash
# ==============================================================================
# FKM SOFTWARE FACTORY — SCRIPT CÀI ĐẶT TỰ ĐỘNG (macOS & Linux)
# ==============================================================================
set -e

# Màu sắc hiển thị
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo -e "${PURPLE}==================================================================${NC}"
echo -e "${CYAN}🚀 CHƯƠNG TRÌNH CÀI ĐẶT FKM SOFTWARE FACTORY & MULTI-WEB BRIDGE${NC}"
echo -e "${PURPLE}==================================================================${NC}"

# 1. Kiểm tra Python 3
echo -e "\n${BLUE}[1/5] Kiểm tra phiên bản Python...${NC}"
PYTHON_BIN=""
if command -v python3 &>/dev/null; then
    PYTHON_BIN="python3"
elif command -v python &>/dev/null; then
    PYTHON_BIN="python"
else
    echo -e "${RED}❌ Lỗi: Không tìm thấy Python 3 trên hệ thống! Vui lòng cài đặt Python >= 3.9.${NC}"
    exit 1
fi

PY_VER=$($PYTHON_BIN -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo -e "${GREEN}✓ Đã tìm thấy Python: $PYTHON_BIN (phiên bản $PY_VER)${NC}"

# 2. Khởi tạo Virtual Environment (.venv)
echo -e "\n${BLUE}[2/5] Thiết lập Môi trường ảo (Virtual Environment)...${NC}"
if [ ! -d "$ROOT_DIR/.venv" ]; then
    echo "Đang tạo môi trường ảo tại $ROOT_DIR/.venv ..."
    $PYTHON_BIN -m venv "$ROOT_DIR/.venv"
    echo -e "${GREEN}✓ Đã tạo .venv thành công.${NC}"
else
    echo -e "${GREEN}✓ Môi trường ảo .venv đã tồn tại.${NC}"
fi

VENV_PIP="$ROOT_DIR/.venv/bin/pip"
VENV_PYTHON="$ROOT_DIR/.venv/bin/python"

# 3. Cài đặt các thư viện Python
echo -e "\n${BLUE}[3/5] Cài đặt các gói phụ thuộc (requirements.txt)...${NC}"
"$VENV_PIP" install --upgrade pip
"$VENV_PIP" install -r "$ROOT_DIR/requirements.txt"
echo -e "${GREEN}✓ Đã cài đặt xong các thư viện FastAPI, Uvicorn, Playwright.${NC}"

# 4. Cài đặt Trình duyệt Playwright Chromium
echo -e "\n${BLUE}[4/5] Tải trình duyệt Playwright Chromium (cho Web Bridge)...${NC}"
"$VENV_PYTHON" -m playwright install chromium

if [[ "$OSTYPE" == "linux-gnu"* ]]; then
    echo "Hệ điều hành Linux phát hiện. Kiểm tra thư viện hệ thống cho Playwright..."
    if command -v sudo &>/dev/null; then
        sudo "$VENV_PYTHON" -m playwright install-deps chromium || true
    fi
fi
echo -e "${GREEN}✓ Trình duyệt Chromium đã sẵn sàng.${NC}"

# 5. Cấu hình tệp môi trường & thư mục làm việc
echo -e "\n${BLUE}[5/5] Chuẩn bị tệp cấu hình và thư mục...${NC}"
if [ ! -f "$ROOT_DIR/.env" ]; then
    cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
    echo -e "${GREEN}✓ Đã tạo tệp cấu hình .env từ .env.example.${NC}"
else
    echo -e "${GREEN}✓ Tệp cấu hình .env đã có sẵn.${NC}"
fi

mkdir -p "$ROOT_DIR/logs"
mkdir -p "$ROOT_DIR/projects"
mkdir -p "$ROOT_DIR/downloads"
mkdir -p "$ROOT_DIR/bridge/profiles"

chmod +x "$ROOT_DIR"/scripts/*.sh 2>/dev/null || true

echo -e "\n${GREEN}==================================================================${NC}"
echo -e "${GREEN}🎉 CÀI ĐẶT HOÀN TẤT THÀNH CÔNG!${NC}"
echo -e "${GREEN}==================================================================${NC}"
echo -e "Bước tiếp theo:"
echo -e " 1. ${YELLOW}Đăng nhập ChatGPT lần đầu:${NC}"
echo -e "    ${CYAN}./scripts/login.sh${NC}"
echo -e " 2. ${YELLOW}Khởi chạy hệ thống:${NC}"
echo -e "    ${CYAN}./scripts/start.sh${NC}"
echo -e " 3. Mở trình duyệt truy cập: ${PURPLE}http://localhost:8090${NC}\n"
