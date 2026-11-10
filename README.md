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

2. **Workspace Harness Engine:**
   - Tự động nhận diện cấu trúc file từ AI, tự sinh toàn bộ thư mục lồng nhau (`os.makedirs`).
   - Ghi mã nguồn sạch trực tiếp vào ổ đĩa, loại bỏ hoàn toàn các đoạn chat râu ria.
   - Tự động lưu vết lịch sử phiên bản qua Git Commit.

3. **Multi-Provider Web Bridge Tích hợp:**
   - Kết nối trực tiếp qua trình duyệt web Playwright tới ChatGPT Web & DeepSeek Web.
   - Sử dụng mô hình AI cao cấp (`GPT-6`, `GPT-5.6 Sol`, `deepseek-v4-pro`...) mà không cần nạp thẻ tín dụng hay lo cạn token API.

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
git clone https://github.com/your-username/fkm-software-factory.git
cd fkm-software-factory
```

### Bước 2: Chạy bộ cài đặt tự động
```bash
./scripts/install.sh
```
*Script sẽ tự động tạo môi trường ảo `.venv`, cài đặt các thư viện cần thiết và nạp trình duyệt Chromium của Playwright.*

### Bước 3: Đăng nhập tài khoản AI (Chỉ làm 1 lần đầu)
```bash
./scripts/login.sh
```
*Cửa sổ trình duyệt sẽ mở ra. Bạn đăng nhập tài khoản ChatGPT của mình, sau đó quay lại cửa sổ dòng lệnh nhấn **Enter** để lưu phiên.*

### Bước 4: Khởi động hệ thống
```bash
./scripts/start.sh
```
*Mở trình duyệt truy cập:* **`http://localhost:8090`**

---

## 🛠️ Các Lệnh Quản Trị Hệ Thống

| Thao tác | Lệnh thực hiện |
| :--- | :--- |
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
