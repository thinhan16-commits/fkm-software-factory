# FKM Software Factory & Multi-Web Bridge

Hệ thống Công xưởng Phần mềm AI Tự Vận Hành (Self-hosted AI Software Factory) — Tự động hóa toàn bộ quy trình phát triển phần mềm từ tiếp nhận ý tưởng, họp hội đồng, lập tài liệu kỹ thuật (Tech Spec), phân rã công việc cho 5 kỹ sư AI và ghi code trực tiếp lên máy tính.

---

## 🌟 Tính Năng Nổi Bật

1. **Hội đồng Nhân sự AI 12 Vai trò:**
   - **Trợ lý Điều phối (Chief of Staff):** Tiếp nhận yêu cầu, chuẩn hóa bài toán.
   - **Giám đốc Kỹ thuật (AI Director):** Lập kiến trúc, phân chia task và giám sát kỹ sư.
   - **Thư ký Kỹ thuật (Secretary):** Lập Spec và Roadmap triển khai.
   - **5 Kỹ sư AI (Subagents #1 - #5):** Làm việc song song trên từng module mã nguồn.
   - **Hội đồng 3 Cố vấn:** Tranh biện đa chiều (Ủng hộ, Phản biện, Dung hòa).
   - **Giao liên & Tester:** Đóng gói bản xuất và chạy kiểm thử tự động.

2. **Workspace Harness Engine & Staging Gate:**
   - **Cơ chế Staging Gate (Bảo vệ 100%):** Mọi phiên bản code được trích xuất và build/test trong thư mục cách ly (`staging_build/iter_N`). Chỉ khi build PASS mới đồng bộ đè lên nhánh chính (`build_workspace`), nếu build FAIL nhánh chính được giữ nguyên vẹn tuyệt đối.
   - **Vá code thông minh (Fuzzy Patch Matching):** Tự động phát hiện và xử lý chênh lệch dòng trống, khoảng trắng khi AI áp dụng khối `SEARCH/REPLACE`.
   - Tự động nhận diện cấu trúc file từ AI, tự sinh toàn bộ thư mục lồng nhau (`os.makedirs`).
   - Tự động lưu vết lịch sử phiên bản qua Git Commit.

3. **Đa Nguồn AI & Native API Tool Calling:**
   - **ChatGPT Web Bridge:** Kết nối trực tiếp qua trình duyệt tới ChatGPT Web (`GPT-6`, `GPT-5.6 Sol`...).
   - **DeepSeek Web Bridge:** Kết nối miễn phí qua `chat.deepseek.com`, hỗ trợ `deepseek-chat` (V3) và `deepseek-reasoner` (R1 DeepThink).
   - **DeepSeek API Chính Thức:** Kết nối siêu tốc qua `api.deepseek.com`, quản lý và kiểm tra kết nối API Key trực tiếp trên giao diện Web.
   - **Native OpenAI Tool Calling Adapter:** Hỗ trợ chuẩn `tools` / `functions` của OpenAI, tự bóc tách `<tool_call>` thành đối tượng gọi hàm, hỗ trợ SSE streaming và tự động xoay vòng phiên (`auto-rotation`) chống tràn token.

4. **Trải nghiệm Mobile & Remote Terminal:**
   - Giao diện Web PWA tối ưu mượt mà cho điện thoại (iPhone, Android).
   - Remote Terminal điều khiển lệnh máy chủ từ xa với tính năng đa tab (Multi-tab) và gửi tệp tin.
   - Đồng hồ đo Quota dự báo ngữ cảnh phiên thời gian thực.

---

## 🚀 Hướng Dẫn Cài Đặt Nhanh

### Yêu cầu hệ thống:
* **Hệ điều hành:** macOS (Apple Silicon / Intel) hoặc Linux (Ubuntu 20.04+ / Debian).
* **Python:** Phiên bản `>= 3.9`.
* **Git:** Đã cài sẵn trên máy.

---

### Bước 1: Tải mã nguồn về máy
```bash
git clone https://github.com/thinhan16-commits/fkm-software-factory.git
cd fkm-software-factory
```

### Bước 2: Chạy bộ cài đặt tự động
```bash
./scripts/install.sh
```
*Script sẽ tự động tạo môi trường ảo `.venv`, cài đặt các thư viện cần thiết và nạp trình duyệt Chromium của Playwright.*

### Bước 3: Đăng nhập tài khoản AI (Chỉ làm 1 lần đầu)
```bash
# Đăng nhập ChatGPT Web:
./scripts/login.sh chatgpt

# Hoặc đăng nhập DeepSeek Web:
./scripts/login.sh deepseek
```
*Cửa sổ trình duyệt sẽ mở ra. Bạn đăng nhập tài khoản của mình, sau đó quay lại cửa sổ dòng lệnh nhấn **Enter** để lưu phiên. Ngoài ra, bạn cũng có thể nhập trực tiếp **DeepSeek API Key** ngay trên giao diện Web mà không cần đăng nhập trình duyệt.*

### Bước 4: Khởi động hệ thống
```bash
./scripts/start.sh
```
*Mở trình duyệt truy cập:* **`http://localhost:8090`**

---

## 🛠️ Các Lệnh Quản Trị Hệ Thống

| Thao tác | Lệnh thực hiện |
| :--- | :--- |
| **Cập nhật & Khởi động lại** | `./scripts/update.sh` |
| **Khởi động nền (Daemon Mode)** | `./scripts/start.sh --daemon` |
| **Kiểm tra trạng thái hệ thống** | `./scripts/status.sh` |
| **Dừng toàn bộ dịch vụ** | `./scripts/stop.sh` |
| **Xem log Factory** | `tail -f logs/factory.log` |
| **Xem log Web Bridge** | `tail -f logs/bridge.log` |

---

## 📁 Cấu Trúc Thư Mục

```text
fkm-software-factory/
├── bridge/          # Lõi Multi-Provider Web Bridge (FastAPI + Playwright)
├── factory/         # Lõi Factory Server & Workspace Harness
├── static/          # Giao diện Web App (HTML5, PWA, CSS, JS)
├── scripts/         # Bộ script tự động hoá (install, start, stop, login, status)
├── docker/          # Dockerfile & docker-compose cho môi trường container
├── .env.example     # Tệp mẫu cấu hình cổng và môi trường
├── requirements.txt # Danh sách thư viện Python
└── README.md        # Tài liệu hướng dẫn sử dụng
```

---

## 📄 Giấy Phép (License)

Dự án phát hành dưới giấy phép MIT License. Bạn có thể tự do sử dụng, chỉnh sửa và phân phối.
