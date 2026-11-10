#!/usr/bin/env bash
# ==============================================================================
# FKM SOFTWARE FACTORY — SCRIPT ĐĂNG NHẬP CHATGPT / DEEPSEEK LẦN ĐẦU
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

PROVIDER="${1:-chatgpt}"
PROFILE_DIR="$ROOT_DIR/bridge/profiles/$PROVIDER"
mkdir -p "$PROFILE_DIR"

echo -e "${PURPLE}==================================================================${NC}"
echo -e "${CYAN}🔐 ĐĂNG NHẬP WEB AI: ${YELLOW}$PROVIDER${NC}"
echo -e "${PURPLE}==================================================================${NC}"
echo -e "Trình duyệt có giao diện (Headful Chromium) sẽ mở ra ngay bây giờ."
echo -e "👉 Anh vui lòng đăng nhập tài khoản vào trang web."
echo -e "👉 Sau khi đăng nhập thành công vào màn hình chat, hãy quay lại đây và bấm ${GREEN}[Enter]${NC} để lưu phiên."
echo -e "${PURPLE}==================================================================${NC}\n"

"$VENV_PYTHON" -c "
import asyncio
import os
import sys
from playwright.async_api import async_playwright

async def main():
    provider = '$PROVIDER'
    profile_dir = '$PROFILE_DIR'
    target_url = 'https://chatgpt.com' if provider == 'chatgpt' else 'https://chat.deepseek.com'

    async with async_playwright() as p:
        args = [
            '--disable-blink-features=AutomationControlled',
            '--no-sandbox',
            '--start-maximized',
        ]
        context = await p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            args=args,
            viewport=None
        )
        page = context.pages[0] if context.pages else await context.new_page()
        print(f'Đang mở trang: {target_url} ...')
        await page.goto(target_url)

        print('\n👉 Vui lòng đăng nhập trên cửa sổ trình duyệt vừa mở.')
        print('👉 Khi đăng nhập xong và thấy khung chat, nhấn ENTER tại dòng lệnh này để hoàn tất...\n')
        
        # Chờ người dùng bấm Enter
        await asyncio.to_thread(input, 'Nhấn [Enter] để lưu hồ sơ đăng nhập và đóng trình duyệt: ')

        print('Đang lưu phiên đăng nhập và đóng trình duyệt...')
        await context.close()
        print('✅ Hoàn tất lưu phiên!')

asyncio.run(main())
"

echo -e "\n${GREEN}🎉 Đã lưu hồ sơ đăng nhập $PROVIDER thành công!${NC}"
echo -e "Giờ đây anh có thể chạy: ${CYAN}./scripts/start.sh${NC} để bắt đầu sử dụng.\n"
