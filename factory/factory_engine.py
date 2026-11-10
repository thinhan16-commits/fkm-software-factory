import asyncio
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from typing import Dict, List, Any, Optional

import workspace_harness

# Bridge riêng của factory (cổng 8001, hồ sơ trình duyệt riêng), tách khỏi bridge 8000 dùng chung với AVS và Codex
BRIDGE_PORT = int(os.environ.get("FACTORY_BRIDGE_PORT", "8001"))
BRIDGE_HOST = os.environ.get("FACTORY_BRIDGE_HOST", "127.0.0.1")
BRIDGE_API_URL = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}/v1/chat/completions"
BRIDGE_CLOSE_URL = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}/v1/factory/sessions/close"
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_CURRENT_DIR) == "factory":
    _DEFAULT_ROOT = os.path.dirname(_CURRENT_DIR)
else:
    _DEFAULT_ROOT = _CURRENT_DIR
FACTORY_ROOT = os.environ.get("FACTORY_ROOT", _DEFAULT_ROOT)
PROJECTS_DIR = os.path.join(FACTORY_ROOT, "projects")
# Lịch sử và trạng thái của từng dự án, lưu ngay trong thư mục của dự án để khởi động lại server vẫn còn
PROJECT_STATE_FILE = "project_state.json"
DOWNLOADS_DIR = os.path.join(FACTORY_ROOT, "downloads")

os.makedirs(PROJECTS_DIR, exist_ok=True)
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Ngữ cảnh gửi model: danh sách file quá dài thì gom theo thư mục cấp 1 (không giới hạn số file nạp vào)
CODE_LIST_MAX_CHARS = 3000
BRIDGE_CONNECT_RETRIES = 24  # bridge đang khởi động lại: chờ tối đa khoảng 2 phút
BRIDGE_CONNECT_WAIT = 5
BRIDGE_MIN_TIMEOUT = 6000  # 100 phút: một phiên ChatGPT thường 25-35 phút, có khi 90 phút; cắt sớm là mất cả phiên
BRIDGE_HTTP_MARGIN = 45  # client chờ lâu hơn hạn của bridge một chút để nhận được lỗi 504 có lý do thay vì socket timeout

# Bộ nhớ riêng của từng vai trò: giữ gần như toàn bộ, chỉ tóm tắt khi gần đầy ngữ cảnh
MEMORY_CHAR_BUDGET = 120_000
MEMORY_KEEP_RECENT = 12
# Phiên Giám đốc bền: một cuộc trò chuyện ChatGPT duy nhất, không gửi lại lịch sử (trang web tự giữ).
# Cứ khoảng 20-30 lượt tương tác / sửa code (mặc định 25), tự động tóm tắt tiến trình và mở phiên sạch mới
# để chống lag DOM trình duyệt, đơ tab và nuốt tin nhắn.
DIRECTOR_ROTATE_EVERY = 30
# Nghỉ lâu hơn mức này thì máy ảo của ChatGPT có thể đã được làm mới: đính kèm lại zip trong cùng phiên (không mở phiên mới)
DIRECTOR_REFRESH_IDLE_S = 2 * 3600
DIRECTOR_REFRESH_NOTE = (
    "Công xưởng báo: đã nghỉ khá lâu nên máy ảo của bạn có thể đã được làm mới. Công xưởng đã đính kèm lại zip mã nguồn gốc và các zip mới nhất của chính bạn; "
    "zip mới nhất là bản đúng, mở lại nếu cần rồi làm tiếp việc dưới đây."
)
MEETING_FULL_LIMIT = 30_000  # lịch sử một thành viên dài hơn mức này thì tóm tắt trước khi lập biên bản
# Phòng họp: các cố vấn tranh luận trong phòng chung rồi bỏ phiếu theo phương án đang xét
MEETING_DEFAULT_ROUNDS = 6
MEETING_MIN_ROUNDS = 3
MEETING_MAX_ROUNDS = 10
MEETING_VOTERS = ("advocate", "critic", "arbiter")
MEETING_VOTE_WEIGHT = {"dong_y": 1.0, "dong_y_co_dieu_kien": 0.5, "phan_doi": 0.0}
MEETING_VOTE_LABELS = {"dong_y": "Đồng ý", "dong_y_co_dieu_kien": "Đồng ý có điều kiện", "phan_doi": "Phản đối"}
MEETING_CONSENSUS_RATIO = 2 / 3
MEETING_ROOM_LIMIT = 40_000        # phòng họp dài hơn mức này thì tóm tắt các vòng cũ
MEETING_OWN_MEMORY_LIMIT = 20_000  # lịch sử riêng đưa vào mỗi lượt tranh luận
MEETING_CALL_TIMEOUT = 240         # mỗi lượt phát biểu có thể lâu hơn lượt chat thường
MEETING_STATUS_LABELS = {
    "dang_hop": "đang tranh luận",
    "dong_thuan": "đạt đồng thuận",
    "het_vong": "hết số vòng, chưa đồng thuận",
    "bi_te": "bế tắc, không ai đổi phiếu",
    "gian_doan": "bị gián đoạn",
}
MEETING_RULES = (
    "Bạn đang họp với các thành viên Hội đồng để chọn phương án. Quy tắc phát biểu:\n"
    "- Trả lời bằng JSON với các trường: \"phieu\" (dong_y, dong_y_co_dieu_kien hoặc phan_doi), "
    "\"noi_dung\" (lời phát biểu cho phòng họp, tối đa 200 từ), \"dieu_kien\" (nếu có), "
    "\"tom_tat_lap_luan\" (tóm tắt lập luận của bạn, tối đa 200 từ).\n"
    "- Phải nêu tên một thành viên và một điểm cụ thể bạn đồng ý hoặc phản đối. Không nhắc lại nguyên văn ý cũ.\n"
    "- Phiếu đồng ý có điều kiện hoặc phản đối thì phải nêu rõ điều kiện hoặc phương án thay thế.\n"
    "- Đổi phiếu hay giữ phiếu đều phải có lý do."
)
MEETING_COORDINATOR_RULES = (
    "Bạn là Giám đốc Điều hành, điều phối phòng họp và KHÔNG bỏ phiếu. "
    "Trả lời bằng JSON với hai trường: \"phuong_an\" (phương án đang xét, viết rõ ràng để các cố vấn bỏ phiếu) "
    "và \"diem_chua_dong_thuan\" (danh sách các điểm các cố vấn còn bất đồng, mỗi điểm một câu)."
)
# Bridge tách khối <reasoning> khỏi câu trả lời và trả về riêng (reasoning_content), để lưu lại cho các lượt sau
BRIDGE_HEALTH_URL = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}/health"


def _bridge_started_at() -> Optional[float]:
    """Thời điểm bridge khởi động. Trả None nếu không đọc được (bridge tắt hoặc bản cũ không báo)."""
    try:
        with urllib.request.urlopen(BRIDGE_HEALTH_URL, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8")).get("started_at")
    except Exception:
        return None


REASONING_INSTRUCTION = (
    "Cuối câu trả lời, viết thêm một khối <reasoning>...</reasoning> tóm tắt lập luận của bạn: "
    "các giả định, lý do chọn hướng đi, và điều còn cân nhắc (tối đa 300 từ). "
    "Khối này được lưu lại để bạn giữ mạch suy nghĩ ở các lượt sau; Chủ tịch không đọc khối này."
)

DELEGATION_INSTRUCTION = (
    "Khi cần một kỹ sư làm việc cụ thể, cuối câu trả lời viết khối sau (kỹ sư là subagent-1 đến subagent-5):\n"
    "```giao_viec\n"
    '[{"to": "subagent-1", "task": "nội dung việc cần làm"}]\n'
    "```\n"
    "Chỉ giao khi thật cần. Không cần thì không viết khối này. Chủ tịch chỉ thấy phần tổng hợp của bạn; "
    "kỹ sư làm việc trong thread riêng của họ."
)
DELEGATION_BLOCK_RE = re.compile(r"```(?:giao_viec|mo_worker|delegate|subagents?)\s*(.*?)```", re.DOTALL | re.IGNORECASE)

ASSISTANT_INSTRUCTION = (
    "Bạn là Trợ lý Điều phối Cấp cao của Chủ tịch. Bạn bao quát mục tiêu và KHÔNG đọc chi tiết mã nguồn từng dòng.\n"
    "1. Khi đã làm rõ yêu cầu của Chủ tịch và cần chuyển sang kỹ thuật triển khai, bạn xuất khối lệnh:\n"
    "```lenh_tro_ly\n"
    '{"action": "giao_giam_doc", "brief": "Bản đặc tả yêu cầu nghiệp vụ chi tiết đã tổ chức"}\n'
    "```\n"
    "2. Nếu Chủ tịch yêu cầu mở phiên họp thảo luận hoặc phản biện đa chiều, bạn xuất khối lệnh:\n"
    "```lenh_tro_ly\n"
    '{"action": "bat_hoi_dong", "topic": "Chủ đề cần thảo luận"}\n'
    "```\n"
    "3. Nếu chỉ trao đổi thông thường, giải đáp hoặc làm rõ ý tưởng, KHÔNG viết khối lệnh này."
)
ASSISTANT_BLOCK_RE = re.compile(r"```(?:lenh_tro_ly|assistant_cmd|tro_ly_cmd)\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _safe_parse_json_dict(body: str) -> Optional[Dict[str, Any]]:
    if not body:
        return None
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    try:
        import ast
        data = ast.literal_eval(body)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def parse_assistant_command(text: str) -> tuple:
    """Tách khối lenh_tro_ly hoặc thẻ [LENH: ...] khỏi câu trả lời của Trợ lý. Trả về (cleaned_text, cmd_dict)."""
    raw = text or ""
    # 1. Thử bắt khối fenced code block ```lenh_tro_ly ... ```
    match = ASSISTANT_BLOCK_RE.search(raw)
    if match:
        cleaned = (raw[: match.start()] + raw[match.end():]).strip()
        body = match.group(1).strip()
        data = _safe_parse_json_dict(body)
        if data:
            return cleaned, data
        if "giao_giam_doc" in body.lower():
            return cleaned, {"action": "giao_giam_doc", "brief": body}
        elif "bat_hoi_dong" in body.lower():
            return cleaned, {"action": "bat_hoi_dong", "topic": body}
        return cleaned, None

    # 2. Thử bắt khối plain-text lenh_tro_ly { ... } (khi model quên bọc backticks)
    pt_match = re.search(r"(?:lenh_tro_ly|assistant_cmd|tro_ly_cmd)\s*(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})", raw, re.DOTALL | re.IGNORECASE)
    if pt_match:
        cleaned = (raw[: pt_match.start()] + raw[pt_match.end():]).strip()
        body = pt_match.group(1).strip()
        data = _safe_parse_json_dict(body)
        if data:
            return cleaned, data

    # 3. Thử bắt dạng thẻ dòng [LENH: GIAO_GIAM_DOC: ...] hoặc [LENH: BAT_HOI_DONG: ...]
    inline_re = re.compile(r"\[(?:LENH|COMMAND):\s*(GIAO_GIAM_DOC|BAT_HOI_DONG)(?::\s*([^\]]+))?\]", re.IGNORECASE)
    m2 = inline_re.search(raw)
    if m2:
        action_type = m2.group(1).lower()
        param = (m2.group(2) or "").strip()
        cleaned = (raw[: m2.start()] + raw[m2.end():]).strip()
        if "giao" in action_type:
            return cleaned, {"action": "giao_giam_doc", "brief": param or cleaned}
        elif "hoi_dong" in action_type:
            return cleaned, {"action": "bat_hoi_dong", "topic": param or "Thảo luận yêu cầu dự án"}

    return raw, None

FILE_REQUEST_INSTRUCTION = (
    "Toàn bộ mã nguồn dự án đang nằm trên ổ đĩa máy chủ (workspace) của Công xưởng. "
    "Bạn KHÔNG CẦN và TUYỆT ĐỐI KHÔNG YÊU CẦU Chủ tịch tải file ZIP lên. "
    "Danh sách file bạn thấy là cây thư mục thực tế trên đĩa. "
    "Khi cần xem nội dung chi tiết của bất kỳ file nào để phân tích, đối chiếu hoặc sửa đổi, bạn chỉ cần xuất khối xem_file:\n"
    "```xem_file\n"
    "apps/api/src/app.controller.ts\n"
    "```\n"
    "(Mỗi dòng một đường dẫn file. Bạn có thể xin nhiều file cùng lúc). "
    "Nếu cần tìm kiếm từ khóa, hàm, biến trong toàn bộ dự án:\n"
    "```tim_ma\n"
    "từ_khóa_cần_tìm\n"
    "```\n"
    "Công xưởng sẽ tự động đọc file trực tiếp từ đĩa và gửi lại nội dung kèm số dòng (L0001:) cho bạn ngay lập tức. "
    "Khi đã có nội dung cần sửa, bạn xuất bản vá chuẩn theo khối <<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE "
    "hoặc tạo file mới để Công xưởng tự động đồng bộ trực tiếp vào đĩa cho Chủ tịch."
)
ZIP_SANDBOX_INSTRUCTION = FILE_REQUEST_INSTRUCTION


def file_access_instruction(backend: Optional[str]) -> str:
    """Tất cả các model (ChatGPT, DeepSeek, Dola) đều dùng chuẩn Harness Native xem_file và SEARCH/REPLACE trực tiếp trên đĩa."""
    return FILE_REQUEST_INSTRUCTION


FILE_BLOCK_RE = re.compile(r"```(xem_file|read_file|view_file|tim_ma|search_code)\s*(.*?)```", re.IGNORECASE | re.DOTALL)
# Ngân sách cho mỗi lượt xem file, để prompt gửi qua bridge không quá dài. Dự án không bị giới hạn số file.
FILE_REQUEST_MAX_ROUNDS = 3  # tối đa 3 lượt xem file cho một tin
FILE_REQUEST_MAX_PATHS = 15  # mỗi lượt xin tối đa 15 file; cần thêm thì xin ở lượt sau
FILE_REQUEST_MAX_KEYWORDS = 3  # mỗi lượt tìm tối đa 3 từ khóa
FILE_CHUNK_MAX_CHARS = 20_000  # mỗi file gửi tối đa 20.000 ký tự đầu
FILE_ROUND_MAX_CHARS = 40_000  # tổng nội dung gửi trong một lượt
SEARCH_MAX_HITS = 40  # số kết quả hiện ra cho mỗi từ khóa
FILE_FOLLOWUP_TIMEOUT = 240  # lượt gửi kèm nội dung file có thể lâu hơn lượt chat thường

# Model và mức suy luận cho từng vai trò. Lưu ra file JSON để giữ lại sau khi khởi động lại.
LLM_EFFORTS = ("none", "low", "medium", "high")
LLM_ROLE_IDS = (
    "assistant", "director", "secretary", "advocate", "critic", "arbiter",
    "subagent-1", "subagent-2", "subagent-3", "subagent-4", "subagent-5",
)
# Nguồn của vai trò. 'chatgpt', 'deepseek', 'dola' đi qua Web Bridge (cổng 8000/8001), đúng trang đã đăng nhập trong bridge.
# 'deepseek_api' gọi DeepSeek API trực tiếp; hiện không dùng, giữ lại để sau này.
LLM_BACKENDS = ("chatgpt", "deepseek", "dola", "deepseek_api")
BRIDGE_BACKENDS = ("chatgpt", "deepseek", "dola")
LLM_BACKEND_DEFAULT_MODEL = {"chatgpt": "chatgpt-web-latest", "deepseek": "deepseek-chat", "dola": "Dola AI", "deepseek_api": "deepseek-chat"}
LLM_DEFAULT = {"backend": "chatgpt", "model": LLM_BACKEND_DEFAULT_MODEL["chatgpt"], "reasoning_effort": "none"}
# DeepSeek: Mô hình chuẩn V3 và R1 (DeepThink)
DEEPSEEK_MODELS = ("deepseek-chat", "deepseek-reasoner")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_MAX_TOKENS = 8192
DEEPSEEK_MIN_TIMEOUT = 300  # suy luận có thể lâu hơn lượt chat thường
ROLE_LLM_FILE = os.path.join(FACTORY_ROOT, "role_llm_settings.json")
GLOBAL_LLM_FILE = os.path.join(FACTORY_ROOT, "global_llm_settings.json")
DEEPSEEK_KEY_FILE = os.path.join(FACTORY_ROOT, "deepseek_key.json")


def get_deepseek_api_key() -> str:
    """Đọc DeepSeek API key từ biến môi trường hoặc tệp lưu trữ bảo mật deepseek_key.json."""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    if os.path.exists(DEEPSEEK_KEY_FILE):
        try:
            with open(DEEPSEEK_KEY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                saved_key = (data.get("api_key") or "").strip()
                if saved_key:
                    os.environ["DEEPSEEK_API_KEY"] = saved_key
                    return saved_key
        except Exception:
            pass
    return ""


def set_deepseek_api_key(api_key: str) -> None:
    """Lưu DeepSeek API key vào tệp bảo mật và cập nhật biến môi trường."""
    clean_key = (api_key or "").strip()
    os.environ["DEEPSEEK_API_KEY"] = clean_key
    try:
        with open(DEEPSEEK_KEY_FILE, "w", encoding="utf-8") as f:
            json.dump({"api_key": clean_key, "updated_at": int(time.time())}, f, indent=2)
    except Exception as e:
        print(f"⚠️ Không thể lưu {DEEPSEEK_KEY_FILE}: {e}")


def _load_global_llm_setting() -> Dict[str, str]:
    try:
        with open(GLOBAL_LLM_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
            return {**LLM_DEFAULT, **saved}
    except (OSError, ValueError):
        return dict(LLM_DEFAULT)


GLOBAL_LLM_SETTING: Dict[str, str] = _load_global_llm_setting()


def save_global_llm_setting(backend: str, reasoning_effort: str, model: str = "") -> Dict[str, str]:
    if backend not in LLM_BACKENDS:
        raise ValueError(f"Nguồn không hợp lệ: {backend}")
    if reasoning_effort not in LLM_EFFORTS:
        raise ValueError(f"Mức suy luận không hợp lệ: {reasoning_effort}")
    model = (model or "").strip() or LLM_BACKEND_DEFAULT_MODEL.get(backend, "chatgpt-web-latest")
    GLOBAL_LLM_SETTING["backend"] = backend
    GLOBAL_LLM_SETTING["model"] = model
    GLOBAL_LLM_SETTING["reasoning_effort"] = reasoning_effort
    with open(GLOBAL_LLM_FILE, "w", encoding="utf-8") as f:
        json.dump(GLOBAL_LLM_SETTING, f, ensure_ascii=False, indent=2)
    # Cập nhật lại các vai trò không có ngoại lệ
    for role in LLM_ROLE_IDS:
        if not ROLE_LLM_SETTINGS.get(role, {}).get("is_override", False):
            ROLE_LLM_SETTINGS[role] = {**GLOBAL_LLM_SETTING, "is_override": False}
    return dict(GLOBAL_LLM_SETTING)


def _load_role_llm_settings() -> Dict[str, Dict[str, Any]]:
    try:
        with open(ROLE_LLM_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = {}
    res = {}
    for role in LLM_ROLE_IDS:
        if role in saved and not saved[role].get("inherit", False):
            res[role] = {**saved[role], "is_override": True}
        else:
            res[role] = {**GLOBAL_LLM_SETTING, "is_override": False}
    return res


ROLE_LLM_SETTINGS: Dict[str, Dict[str, Any]] = _load_role_llm_settings()


# Người ship và Trợ lý roadmap không nằm trong danh sách cài đặt riêng; mặc định dùng GPT như mọi vai trò khác
AUTOMATION_ROLE_LLM: Dict[str, Dict[str, str]] = {}


def role_llm_kwargs(role_id: str) -> Dict[str, str]:
    key = "director" if role_id == "director-main" else role_id
    setting = ROLE_LLM_SETTINGS.get(key)
    if not setting or not setting.get("is_override", False):
        setting = GLOBAL_LLM_SETTING
    return {
        "backend": setting.get("backend", "chatgpt"),
        "model": setting.get("model", LLM_BACKEND_DEFAULT_MODEL["chatgpt"]),
        "reasoning_effort": setting.get("reasoning_effort", "none"),
    }


def save_role_llm_setting(role_id: str, model: str, reasoning_effort: str, backend: str = "chatgpt", inherit: bool = False) -> Dict[str, Any]:
    if role_id not in LLM_ROLE_IDS:
        raise ValueError(f"Vai trò không hợp lệ: {role_id}")
    try:
        with open(ROLE_LLM_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = {}

    if inherit:
        saved.pop(role_id, None)
        ROLE_LLM_SETTINGS[role_id] = {**GLOBAL_LLM_SETTING, "is_override": False}
    else:
        if reasoning_effort not in LLM_EFFORTS:
            raise ValueError(f"Mức suy luận không hợp lệ: {reasoning_effort}")
        if backend not in LLM_BACKENDS:
            raise ValueError(f"Nguồn không hợp lệ: {backend}")
        model = (model or "").strip() or LLM_BACKEND_DEFAULT_MODEL.get(backend, "")
        saved[role_id] = {
            "backend": backend,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "is_override": True,
        }
        ROLE_LLM_SETTINGS[role_id] = saved[role_id]

    with open(ROLE_LLM_FILE, "w", encoding="utf-8") as f:
        json.dump(saved, f, ensure_ascii=False, indent=2)
    return ROLE_LLM_SETTINGS[role_id]


# ============================================================================
# 1. ĐỊNH NGHĨA HỘI ĐỒNG 3 TÁC NHÂN, THƯ KÝ & GIÁM ĐỐC ĐIỀU HÀNH
# ============================================================================

COUNCIL_PERSONAS = {
    "assistant": {
        "id": "assistant",
        "name": "Trợ lý Điều phối (Executive Assistant)",
        "avatar": "👩‍💼",
        "role_title": "Trợ lý Điều phối Cấp cao của Chủ tịch",
        "system_prompt": (
            "Bạn là Trợ lý Điều phối Cấp cao (Chief of Staff & AI Orchestrator) của Chủ tịch. "
            "Chủ tịch là người ra quyết định chiến lược và đưa ra bài toán sản phẩm.\n"
            "VAI TRÒ & PHẠM VI QUYỀN HẠN CỦA BẠN:\n"
            "1. Lắng nghe, thấu hiểu và làm rõ yêu cầu của Chủ tịch. Giao tiếp nhã nhặn, thông minh, lịch thiệp, xưng 'Em' và gọi 'Chủ tịch'.\n"
            "2. Quản lý vĩ mô dự án: bạn KHÔNG đọc chi tiết mã nguồn từng file và KHÔNG làm việc kỹ thuật chi tiết để giữ đầu óc luôn minh mẫn, không bị nghẽn ngữ cảnh.\n"
            "3. Khi Chủ tịch nêu ý tưởng bài toán mới: Bạn hỏi rõ nhu cầu; nếu Chủ tịch muốn mở cuộc họp phản biện đa chiều thì bạn phát lệnh kích hoạt Hội đồng. Nếu Chủ tịch không yêu cầu họp, bạn đi thẳng vào tổ chức yêu cầu.\n"
            "4. Khi yêu cầu đã rõ: Bạn tổ chức lại các yêu cầu nghiệp vụ một cách mạch lạc, chặt chẽ (Requirement Brief), rồi phát lệnh chuyển giao cho Giám đốc Kỹ thuật bằng khối lệnh chuẩn sau:\n"
            "```lenh_tro_ly\n"
            "{\n"
            '  "action": "giao_giam_doc",\n'
            '  "brief": "Bản đặc tả yêu cầu nghiệp vụ chi tiết đã được Trợ lý chuẩn hóa"\n'
            "}\n"
            "```\n"
            "Nếu Chủ tịch muốn họp Hội đồng 3 Cố vấn (Ủng hộ, Phản biện, Trung lập), xuất khối lệnh:\n"
            "```lenh_tro_ly\n"
            "{\n"
            '  "action": "bat_hoi_dong",\n'
            '  "topic": "Chủ đề cần thảo luận và phản biện"\n'
            "}\n"
            "```\n"
            "5. Bạn KHÔNG cần biết Giám đốc chia việc cho bao nhiêu Kỹ sư hay Kỹ sư gọi ship thế nào. "
            "Hệ thống sẽ tự động phối hợp; khi kỹ thuật hoàn thành, bạn sẽ nhận được báo cáo tổng hợp để trình bày lại cho Chủ tịch bằng ngôn ngữ điều hành súc tích, chuyên nghiệp."
        ),
    },
    "director": {
        "id": "director",
        "name": "Giám đốc Kỹ thuật (Technical Lead)",
        "avatar": "👔",
        "role_title": "Chỉ huy Kỹ thuật & Trưởng nhóm Kiến trúc",
        "system_prompt": (
            "Bạn là Giám đốc Kỹ thuật (Technical Lead & Chief Architect) của Công xưởng Phần mềm AI. "
            "Bạn nhận nhiệm vụ kỹ thuật từ Trợ lý Điều phối hoặc trực tiếp từ Chủ tịch.\n"
            "VAI TRÒ & PHẠM VI QUYỀN HẠN CỦA BẠN:\n"
            "1. Phân tích kiến trúc phần mềm, cấu trúc thư mục và các module cần triển khai.\n"
            "2. QUYỀN TỰ CODE: Nếu nhiệm vụ đơn giản hoặc chỉ sửa đổi nhỏ (1-2 file), bạn có thể tự tra cứu file (xem_file:) và xuất bản vá code SEARCH/REPLACE trực tiếp.\n"
            "3. QUYỀN PHÂN CÔNG KỸ SƯ WORKER (SUBAGENTS): Nếu bài toán lớn gồm nhiều phần độc lập, bạn CÓ QUYỀN ra lệnh cho App mở từ 1 đến 5 phiên Kỹ sư chuyên trách song song (subagent-1 đến subagent-5) bằng cách xuất khối lệnh cuối câu trả lời:\n"
            "```giao_viec\n"
            "[\n"
            '  {"to": "subagent-1", "task": "Nhiệm vụ cụ thể, module, file cần làm"},\n'
            '  {"to": "subagent-2", "task": "Nhiệm vụ cụ thể, module, file cần làm"}\n'
            "]\n"
            "```\n"
            "Mỗi phần tử trong danh sách sẽ được App mở thành một phiên ChatGPT độc lập trên máy chủ, nạp đúng hợp đồng bạn giao.\n"
            "4. Sau khi các Kỹ sư hoàn thành và nộp code về qua Shipper, bạn sẽ nghiệm thu, tổng hợp kết quả và báo cáo lại để Trợ lý trình Chủ tịch.\n"
            "5. Toàn bộ mã nguồn đã có sẵn trên đĩa (build_workspace). Muốn xem file dùng `xem_file: path/to/file`. Tuyệt đối không hỏi file ZIP."
        ),
    },
    "advocate": {
        "id": "advocate",
        "name": "Tác nhân Ủng hộ (Visionary Architect)",
        "avatar": "🚀",
        "role_title": "Kiến trúc sư Tầm nhìn & Đổi mới",
        "system_prompt": (
            "Bạn là Tác nhân Ủng hộ (Visionary Architect) trong Hội đồng Thiết kế Phần mềm AI. "
            "Tính cách của bạn: Nhiệt huyết, sáng tạo, luôn tìm cách tối đa hóa giá trị sản phẩm, "
            "đề xuất các tính năng đột phá, trải nghiệm người dùng xuất sắc và kiến trúc mở rộng mạnh mẽ. "
            "Hãy phân tích ý tưởng của Chủ tịch (User), làm nổi bật các điểm mạnh, đề xuất thêm các tính năng "
            "đắt giá và phương án kiến trúc hiện đại nhất để dự án thành công vang dội. Trả lời ngắn gọn, súc tích bằng tiếng Việt."
        ),
    },
    "critic": {
        "id": "critic",
        "name": "Tác nhân Phản biện (Chief Critic & Security Auditor)",
        "avatar": "🛡️",
        "role_title": "Chuyên gia Phản biện & Rà soát Rủi ro",
        "system_prompt": (
            "Bạn là Tác nhân Phản biện (Devil's Advocate & Chief Critic) trong Hội đồng Thiết kế Phần mềm AI. "
            "Tính cách của bạn: Sắc sảo, khắt khe, thực tế và không khoan nhượng với các thiết kế mơ hồ. "
            "Nhiệm vụ của bạn là chỉ ra các điểm nghẽn kỹ thuật, lỗ hổng kiến trúc, edge-cases, nguy cơ lỗi khi build/chạy, "
            "sự phức tạp thừa thãi và rủi ro bảo mật trong ý tưởng hoặc trong đề xuất của Tác nhân Ủng hộ. "
            "Hãy phản biện thẳng thắn nhưng mang tính xây dựng cao bằng tiếng Việt."
        ),
    },
    "arbiter": {
        "id": "arbiter",
        "name": "Tác nhân Trung lập (Pragmatic Tech Lead)",
        "avatar": "⚖️",
        "role_title": "Trưởng nhóm Kỹ thuật Trung lập & Thực dụng",
        "system_prompt": (
            "Bạn là Tác nhân Trung lập (Pragmatic Tech Lead) trong Hội đồng Thiết kế Phần mềm AI. "
            "Tính cách của bạn: Điềm tĩnh, khách quan, thực dụng. Sau khi nghe ý tưởng của Chủ tịch cùng ý kiến "
            "của Tác nhân Ủng hộ và Tác nhân Phản biện, bạn tổng hợp lại phương án cân bằng nhất: "
            "giữ lại những tính năng cốt lõi đắt giá nhất nhưng cắt bỏ sự rườm rà để đảm bảo phần mềm có thể code xong nhanh, "
            "chạy ổn định 100% trên macOS và dễ kiểm thử. Trả lời mạch lạc bằng tiếng Việt."
        ),
    },
    "secretary": {
        "id": "secretary",
        "name": "Thư ký Kỹ thuật (Technical Secretary)",
        "avatar": "📝",
        "role_title": "Thư ký Kỹ thuật • Biên bản, Spec & Roadmap",
        "system_prompt": (
            "Bạn là Thư ký Kỹ thuật của Hội đồng và Công xưởng. "
            "Nhiệm vụ của bạn là ghi chép biên bản cuộc họp, theo dõi các quyết định chốt của Chủ tịch, "
            "soạn thảo Đặc tả Kỹ thuật (TECH_SPEC.md) và lập Roadmap phân rã công việc. Trả lời súc tích, mạch lạc."
        ),
    },
    "subagent-1": {
        "id": "subagent-1",
        "name": "Kỹ sư 1 (Core Engine & Backend)",
        "avatar": "👨‍💻",
        "role_title": "Kỹ sư chuyên trách #1 • Core Engine & Backend",
        "system_prompt": (
            "Bạn là Kỹ sư chuyên trách 1 phụ trách phần Core Engine & Backend của dự án. "
            "Khi Chủ tịch hoặc Giám đốc nhắn tin trực tiếp, hãy báo cáo tình hình các file lõi, logic xử lý dữ liệu và tiến độ hiện tại. Trả lời chuyên môn, rõ ràng."
        ),
    },
    "subagent-2": {
        "id": "subagent-2",
        "name": "Kỹ sư 2 (UI/UX & Frontend)",
        "avatar": "👩‍💻",
        "role_title": "Kỹ sư chuyên trách #2 • UI/UX & Frontend",
        "system_prompt": (
            "Bạn là Kỹ sư chuyên trách 2 phụ trách giao diện UI/UX và tương tác người dùng. "
            "Khi được hỏi, hãy báo cáo tình trạng các file giao diện, CSS, bố cục và độ thẩm mỹ của ứng dụng. Trả lời chuyên nghiệp."
        ),
    },
    "subagent-3": {
        "id": "subagent-3",
        "name": "Kỹ sư 3 (API, Kết nối & Dữ liệu)",
        "avatar": "🧑‍💻",
        "role_title": "Kỹ sư chuyên trách #3 • API, Kết nối & Dữ liệu",
        "system_prompt": (
            "Bạn là Kỹ sư chuyên trách 3 phụ trách API, kết nối mạng và lưu trữ dữ liệu. "
            "Hãy báo cáo tình trạng các endpoint, cơ chế truyền tin và bảo toàn dữ liệu. Trả lời súc tích."
        ),
    },
    "subagent-4": {
        "id": "subagent-4",
        "name": "Kỹ sư 4 (Tích hợp & Đóng gói)",
        "avatar": "👩‍🔬",
        "role_title": "Kỹ sư chuyên trách #4 • Tích hợp & Đóng gói",
        "system_prompt": (
            "Bạn là Kỹ sư chuyên trách 4 phụ trách ghép nối các module và viết script đóng gói khởi chạy. "
            "Hãy báo cáo tiến độ tích hợp và cấu trúc đóng gói sản phẩm. Trả lời nhanh gọn."
        ),
    },
    "subagent-5": {
        "id": "subagent-5",
        "name": "Kỹ sư 5 (Kiểm thử & Tối ưu)",
        "avatar": "🕵️‍♂️",
        "role_title": "Kỹ sư chuyên trách #5 • Kiểm thử & Tối ưu",
        "system_prompt": (
            "Bạn là Kỹ sư chuyên trách 5 phụ trách kiểm tra tính toàn vẹn và tối ưu hiệu năng. "
            "Hãy báo cáo các vấn đề về tài nguyên, tốc độ phản hồi và test case. Trả lời cụ thể."
        ),
    },
    "courier": {
        "id": "courier",
        "name": "Người Giao liên (Courier & Dispatch)",
        "avatar": "🛵",
        "role_title": "Bộ phận Giao liên • Vận chuyển & Điều phối",
        "system_prompt": (
            "Bạn là Bộ phận Giao liên của công xưởng. Nhiệm vụ của bạn là nhận các gói code từ các bàn Kỹ sư "
            "đưa về bàn Giám đốc Kỹ thuật để nghiệm thu và merge. Báo cáo nhanh số chuyến giao và tình trạng hàng."
        ),
    },
    "tester": {
        "id": "tester",
        "name": "Kỹ sư Kiểm thử (Automated Tester & QA)",
        "avatar": "👷‍♂️",
        "role_title": "Kỹ sư Kiểm thử • Chạy lệnh & Xác minh",
        "system_prompt": (
            "Bạn là Kỹ sư Kiểm thử tự động. Nhiệm vụ của bạn là chạy lệnh cài đặt, build mã nguồn và test trên máy chủ, "
            "kiểm tra mã thoát (exit code) và báo cáo log chi tiết để Giám đốc tự sửa lỗi (Self-Healing)."
        ),
    },
}

SECRETARY_SYSTEM_PROMPT = """Bạn là Thư ký Hội đồng Kỹ thuật (Chief Technical Secretary).
Nhiệm vụ của bạn: Đọc toàn bộ lịch sử thảo luận của Hội đồng 3 tác nhân và quyết định chốt dự án của Chủ tịch, sau đó lập Hồ sơ Dự án chuẩn xác tuyệt đối dưới định dạng JSON hợp lệ.

Cấu trúc JSON đầu ra BẮT BUỘC:
{
  "project_name": "Tên dự án ngắn gọn",
  "meeting_minutes_md": "Biên bản cuộc họp đầy đủ bằng Markdown (Tóm tắt ý kiến Ủng hộ, Phản biện, Trung lập và Quyết định chốt)",
  "decisions": [{"text": "Quyết định đã chốt", "source": "chỉ đạo chốt của Chủ tịch hoặc thảo luận của Hội đồng"}],
  "rejected_options": [{"option": "Phương án bị loại", "proposed_by": "Tên thành viên đề xuất", "reason": "Lý do bị loại"}],
  "dissent": [{"member": "Tên thành viên", "position": "Quan điểm còn bảo lưu"}],
  "open_questions": ["Việc còn mở chưa có quyết định"],
  "tech_spec_md": "Đặc tả kỹ thuật chi tiết bằng Markdown (Kiến trúc, công nghệ sử dụng, cấu trúc file, quy ước giao tiếp giữa các file/module)",
  "install_command": "Lệnh cài đặt phụ thuộc trên macOS (ví dụ: echo 'Ready' hoặc pip install ...)",
  "test_command": "Lệnh chạy kiểm thử / xác minh phần mềm hoạt động (ví dụ: python3 test_suite.py)",
  "run_command": "Lệnh khởi chạy chính thức của ứng dụng (ví dụ: python3 main.py)",
  "roadmap": [
    {
      "phase_id": "phase_1",
      "phase_name": "Tên giai đoạn (ví dụ: Khởi tạo Core & Chia nhánh Module)",
      "tasks": [
        {
          "task_id": "task_1",
          "title": "Tên gói công việc",
          "parallel": true,
          "assigned_to": "subagent-1",
          "allowed_files": ["core/models.py", "core/__init__.py"],
          "contract_spec": "Mô tả cực kỳ chi tiết các hàm, class, input/output bắt buộc phải viết trong các file này để khớp với các module khác."
        }
      ]
    }
  ]
}

Nguyên tắc lập biên bản:
- Quyết định chốt của Chủ tịch phải ghi NGUYÊN VĂN ở mục decisions đầu tiên, không diễn giải lệch.
- Phương án bị loại, ý kiến bất đồng và việc còn mở phải ghi đúng người nêu, không gộp lẫn.
- Biên bản Markdown trình bày đúng các mục trên, ghi rõ ý kiến của từng thành viên.

Nguyên tắc lập Roadmap cho Giám đốc Điều hành:
1. Tối đa 5 task song song trong một phase (gán cho subagent-1 đến subagent-5).
2. Những task nền tảng hoặc tích hợp phụ thuộc chặt vào nhau không thể làm song song thì đặt `"parallel": false` và `"assigned_to": "director"` để Giám đốc tự làm.
3. Mỗi task phải có danh sách `allowed_files` rõ ràng, không trùng lặp file giữa các subagent chạy song song cùng một phase để khi Merge không bị xung đột.
4. BẮT BUỘC có ít nhất 1 file test tự động (ví dụ `test_suite.py`) để Worker Thực thi chạy lệnh `test_command` kiểm tra PASS/FAIL.
"""

# Cách xuất kết quả của kỹ sư: GPT có thể tạo zip và JSON; DeepSeek chỉ trả chữ nên dùng thẻ <file>
BUILDER_OUTPUT_RULES_GPT = """1. File `project_snapshot.zip` đính kèm là toàn bộ mã nguồn hiện tại của dự án. Nếu bạn có môi trường Code Interpreter / Python trên web, hãy giải nén nó, chỉ sửa các file được giao (có thể sửa nhiều file), rồi nén toàn bộ dự án đã sửa thành `{task_id}_alpha.zip` và cung cấp link tải.
2. ĐỒNG THỜI, để đảm bảo Worker Giao liên luôn thu hoạch được 100% code ngay cả khi không tải qua link, hãy xuất toàn bộ mã nguồn của từng file theo định dạng JSON chuẩn sau:
{{
  "task_id": "{task_id}",
  "summary": "Tóm tắt ngắn gọn những gì đã code",
  "files": [
    {{
      "path": "đường/dẫn/file.py",
      "content": "toàn bộ mã nguồn hoàn chỉnh của file (không viết tắt, không dùng placeholder)"
    }}
  ]
}}"""

BUILDER_OUTPUT_RULES_TEXT = """1. Bạn chỉ trả lời bằng chữ, không tạo file hay zip. Với mỗi file được giao, viết toàn bộ mã nguồn hoàn chỉnh (không viết tắt, không dùng placeholder) trong một cặp thẻ, đúng mẫu sau:
<file path="đường/dẫn/file.py">
toàn bộ mã nguồn của file
</file>
2. Không viết lời giải thích hay khối markdown bên trong thẻ <file>. Lời giải thích ngắn (nếu cần) viết ngoài các thẻ."""

DIRECTOR_TASK_PROMPT_TEMPLATE = """Bạn là Kỹ sư Phần mềm Cấp cao ({agent_id}) trong Công xưởng Phần mềm AI, làm việc dưới sự chỉ đạo của Giám đốc Điều hành.

=== ĐẶC TẢ KỸ THUẬT TOÀN DỰ ÁN (TECH SPEC) ===
{tech_spec}

=== LỆNH GIAO VIỆC BẮT BUỘC CHO BẠN ===
- Mã công việc: {task_id} — {task_title}
- Các file bạn ĐƯỢC PHÉP và BẮT BUỘC phải hoàn thiện: {allowed_files}
- Chi tiết hợp đồng kỹ thuật (Contract Spec):
{contract_spec}

=== NGUYÊN TẮC XUẤT KẾT QUẢ ===
{output_rules}
"""

DIRECTOR_FIX_PROMPT_TEMPLATE = """Bạn là Giám đốc Điều hành Kỹ thuật (Executive Director Agent).
Worker Thực thi & Kiểm thử vừa chạy bộ mã nguồn `full_source.zip` trên máy Mac và gửi về báo cáo Log lỗi như sau:

=== LỆNH ĐÃ CHẠY ===
Install: `{install_cmd}`
Test/Build: `{test_cmd}`
Exit Code: {exit_code}

=== LOG LỖI (STDOUT / STDERR) ===
{error_log}

=== DANH SÁCH FILE HIỆN TẠI TRONG DỰ ÁN ===
{current_files_summary}

Nhiệm vụ của bạn:
Phân tích nguyên nhân gốc rễ của lỗi trong log trên và xuất ra bản vá hoàn chỉnh cho các file cần sửa để dự án chạy PASS 100%.
Hãy trả về JSON đúng định dạng:
{{
  "diagnosis": "Nguyên nhân lỗi và cách khắc phục",
  "updated_install_command": "Lệnh cài đặt mới nếu cần sửa (hoặc giữ nguyên)",
  "updated_test_command": "Lệnh test mới nếu cần sửa (hoặc giữ nguyên)",
  "files_to_update": [
    {{
      "path": "đường/dẫn/file_cần_sửa.py",
      "content": "toàn bộ nội dung mới hoàn chỉnh của file đó"
    }}
  ]
}}
"""


# ============================================================================
# 2. HÀM GỌI BRIDGE API (HỖ TRỢ ĐA PHIÊN & BẮT ZIP)
# ============================================================================

class BridgeConversationLost(RuntimeError):
    """Bridge không nối lại được cuộc trò chuyện đã lưu của phiên (bị xóa, hoặc bridge không còn địa chỉ). Bridge chưa gửi gì; factory nạp lại gói khởi đầu."""


def _bridge_error_detail(err: urllib.error.HTTPError) -> str:
    """Lấy lý do bridge ghi trong trường detail (ví dụ phiên cần đăng nhập); không có thì giữ mã lỗi HTTP."""
    try:
        body = json.loads(err.read().decode("utf-8"))
        detail = body.get("detail") if isinstance(body, dict) else None
    except Exception:
        detail = None
    return str(detail) if detail else f"HTTP {err.code}: {err.reason}"


def _bridge_supports_attachments() -> bool:
    """Bridge bản cũ không đọc trường attachments và sẽ bỏ qua im lặng, nên kiểm tra trước khi gửi zip."""
    try:
        with urllib.request.urlopen(BRIDGE_HEALTH_URL, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return False
    return "attachments" in (data.get("features") or [])


async def call_bridge_llm(
    messages: List[Dict[str, Any]],
    *,
    session_id: Optional[str] = None,
    model: str = "chatgpt-web-latest",
    provider: str = "chatgpt",
    backend: str = "chatgpt",
    json_mode: bool = False,
    expect_zip: bool = False,
    download_dir: Optional[str] = None,
    stateless: bool = True,
    timeout: int = 90,
    reasoning_effort: str = "none",
    chatgpt_project: Optional[str] = None,
    attachments: Optional[List[Dict[str, str]]] = None,
    persist_session: bool = False,
    resume_expected: bool = False,
) -> Dict[str, Any]:
    """Gọi LLM của vai trò qua Multi-Provider Web Bridge (cổng 8000); trang web lấy theo backend: chatgpt hoặc deepseek.
    backend 'deepseek_api' gọi DeepSeek API trực tiếp; hiện không dùng."""
    if backend == "deepseek_api":
        # DeepSeek API không trả file zip: bước giao file dùng khối JSON trong câu trả lời, Giao liên đọc được khối đó
        return await call_deepseek(messages, model=model, json_mode=json_mode, reasoning_effort=reasoning_effort, timeout=timeout)
    provider = backend if backend in BRIDGE_BACKENDS else provider
    timeout = max(timeout, BRIDGE_MIN_TIMEOUT)
    if attachments and not await asyncio.to_thread(_bridge_supports_attachments):
        raise RuntimeError("Bridge đang chạy bản chưa đính kèm được zip; cần khởi động lại bridge khi AVS rảnh.")
    payload: Dict[str, Any] = {
        "model": model,
        "provider": provider,
        "messages": messages,
        "stateless": stateless,
        "stream": False,
        "expect_zip": expect_zip,
        "reasoning_effort": reasoning_effort,
        "plain_prompt": True,
        "timeout_sec": int(timeout),  # bridge cùng hạn với client (bridge tự cắt ở 180s nếu không có trường này)
        "keep_code_fences": True,  # giữ dấu ``` để đọc được các khối lenh, giao_viec, xem_file, tim_ma
    }
    if session_id:
        payload["session_id"] = session_id
    if chatgpt_project:
        payload["chatgpt_project"] = chatgpt_project
    if attachments:
        payload["attachments"] = attachments
    if persist_session:
        payload["persist_session"] = True  # bridge nhớ địa chỉ cuộc trò chuyện của session_id và mở lại đúng chỗ sau khi tab hoặc bridge khởi động lại
        payload["resume_expected"] = bool(resume_expected)  # phiên đã có sẵn: không nối lại được thì bridge trả 409 conversation_lost thay vì mở chat mới
    if download_dir:
        payload["download_dir"] = download_dir
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    def _do_request():
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            BRIDGE_API_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        for attempt in range(BRIDGE_CONNECT_RETRIES + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout + BRIDGE_HTTP_MARGIN) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    _clean_chat_chrome(data)
                    return data
            except urllib.error.HTTPError as err:
                # Hiện lý do cụ thể (ví dụ phiên ChatGPT cần đăng nhập) thay vì chỉ "HTTP Error 503"
                detail = _bridge_error_detail(err)
                if err.code == 409 and detail.startswith("conversation_lost"):
                    raise BridgeConversationLost(detail) from None
                raise RuntimeError(detail) from None
            except urllib.error.URLError as err:
                # Bridge đang khởi động lại (cổng bridge chưa mở): chờ rồi thử lại, chưa gửi gì nên không bị trùng tin
                if isinstance(err.reason, ConnectionRefusedError) and attempt < BRIDGE_CONNECT_RETRIES:
                    time.sleep(BRIDGE_CONNECT_WAIT)
                    continue
                raise

    return await asyncio.to_thread(_do_request)



async def close_bridge_session(session_id: str, *, delete_conversation: bool = False, provider: str = "chatgpt") -> Dict[str, Any]:
    """Đóng tab của một phiên trong bridge (phiên Giám đốc cũ, worker đã xong). delete_conversation=True: xóa luôn cuộc trò chuyện do chính phiên đó tạo.
    Việc dọn dẹp không bao giờ làm hỏng việc chính: lỗi chỉ trả về {'error': ...}."""
    payload = json.dumps({"provider": provider, "session_id": session_id, "delete_conversation": bool(delete_conversation)}).encode("utf-8")

    def _do() -> Dict[str, Any]:
        req = urllib.request.Request(BRIDGE_CLOSE_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as err:
            return {"error": str(err)[:200]}

    return await asyncio.to_thread(_do)


_CHAT_CHROME_RE = re.compile(
    r"^\s*(?:(?:Đã (?:xử lý|suy nghĩ)[^\n]*|Thought for[^\n]*|Processed[^\n]*)\n+)?\s*(?:ChatGPT đã nói:|ChatGPT said:)\s*\n+",
    re.IGNORECASE,
)


# Nhãn của giao diện ChatGPT chen giữa câu trả lời (mỗi đoạn trả lời có "ChatGPT đã nói:ChatGPT", có đoạn "Đã xử lý trong 42s")
_CHAT_CHROME_LINE_RE = re.compile(
    r"(?im)^[ \t]*(?:ChatGPT(?:[ \t]+(?:đã nói|said))?:?[ \t]*(?:ChatGPT)?|(?:Đã (?:xử lý|suy nghĩ) trong|Thought for|Processed for)[^\n]*"
    r"|Cho đến nay, cuộc hội thoại này có hữu ích với bạn không\??|Is this conversation helpful so far\??"
    r"|Bạn có thích tính cách này không\??|Do you like this personality\??)[ \t]*$\n?"
)


def _clean_chat_chrome(data: Dict[str, Any]) -> None:
    """Bỏ các dòng nhãn của giao diện ChatGPT (\"Đã xử lý trong 1m 8s\", \"ChatGPT đã nói:\") dính trong câu trả lời."""
    try:
        msg = data["choices"][0]["message"]
        if isinstance(msg.get("content"), str):
            text = _CHAT_CHROME_RE.sub("", msg["content"], count=1)
            msg["content"] = _CHAT_CHROME_LINE_RE.sub("", text).strip()
    except (KeyError, IndexError, TypeError):
        pass


_REASONING_TAG_RE = re.compile(r"<(reasoning|think)>(.*?)</\1>", re.DOTALL)


def _split_reasoning_tags(content: Optional[str], native: Optional[str]) -> tuple:
    """Tách khối <reasoning>/<think> khỏi câu trả lời, giống bridge. Ưu tiên reasoning_content do DeepSeek trả về."""
    found = [body.strip() for _, body in _REASONING_TAG_RE.findall(content or "")]
    reasoning = native or "\n".join(found) or None
    return _REASONING_TAG_RE.sub("", content or "").strip(), reasoning


async def call_deepseek(
    messages: List[Dict[str, Any]],
    *,
    model: str,
    json_mode: bool = False,
    reasoning_effort: str = "none",
    timeout: int = 90,
) -> Dict[str, Any]:
    """Gọi DeepSeek API (tương thích OpenAI). Khóa đọc từ biến môi trường hoặc tệp cấu hình bảo mật.
    Trả về cùng dạng với bridge: choices[0].message có content và reasoning_content."""
    api_key = get_deepseek_api_key()
    if not api_key:
        raise RuntimeError("Chưa cấu hình DEEPSEEK_API_KEY. Vui lòng vào Cài Đặt AI để nhập khóa API DeepSeek.")

    target_model = (model or "").strip().lower()
    is_reasoner = (
        "reasoner" in target_model
        or "r1" in target_model
        or reasoning_effort in ("low", "medium", "high")
    )
    actual_model = "deepseek-reasoner" if is_reasoner else "deepseek-chat"

    payload: Dict[str, Any] = {
        "model": actual_model,
        "messages": messages,
        "stream": False,
        "max_tokens": DEEPSEEK_MAX_TOKENS,
    }

    if json_mode:
        if actual_model == "deepseek-chat":
            payload["response_format"] = {"type": "json_object"}
        if not any("json" in str(m.get("content", "")).lower() for m in messages):
            payload["messages"] = list(messages) + [{"role": "system", "content": "Trả lời bằng JSON hợp lệ."}]

    def _do_request() -> Dict[str, Any]:
        req = urllib.request.Request(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=max(timeout, DEEPSEEK_MIN_TIMEOUT)) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"DeepSeek API trả lỗi {err.code}: {detail}") from None
        except urllib.error.URLError as err:
            raise RuntimeError(f"Không kết nối được DeepSeek API: {err.reason}") from None

    data = await asyncio.to_thread(_do_request)
    for choice in data.get("choices", []):
        msg = choice.get("message", {})
        reasoning_direct = msg.get("reasoning_content")
        clean_content, reasoning_parsed = _split_reasoning_tags(msg.get("content"), reasoning_direct)
        msg["content"] = clean_content
        msg["reasoning_content"] = reasoning_direct or reasoning_parsed
    return data


def parse_json_safely(text: str) -> Dict[str, Any]:
    """Bóc tách JSON an toàn từ phản hồi của LLM."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except Exception:
            pass
    brace = re.search(r"(\{.*\})", text, re.DOTALL)
    if brace:
        try:
            return json.loads(brace.group(1))
        except Exception:
            pass
    return {}


# ============================================================================
# 3. WORKER GIAO LIÊN (COURIER WORKER)
# ============================================================================

# ---- Người ship và trợ lý roadmap: Giám đốc (GPT) không tự mở được phiên mới ----
SHIP_MAX_ROUNDS = 6  # số lượt tối đa của một vòng ship và nhắc roadmap, để không chạy mãi
BUILD_INSTALL_TIMEOUT = 1200  # cài thư viện (npm, pip, flutter pub get...) có thể lâu hơn 2 phút
BUILD_RUN_TIMEOUT = 1800  # build hoặc chạy test cả dự án
SHIP_WORKER_TIMEOUT = 6000  # worker làm trong phiên riêng: 25-35 phút là thường, có khi 90 phút
SHIP_MAX_WORKERS = 8
INBOX_POLL_SECONDS = 3  # nhịp quét hộp thư download của Giám đốc để phát tín hiệu đánh thức Người ship
INBOX_SETTLE_SECONDS = 6  # zip phải nằm yên từng ấy giây (đã ghi xong) mới tính là tín hiệu
ACK_REPLY = "NHAN"  # mã agent điều khiển phải viết ở đầu câu trả lời: chứng tỏ nó đã đọc đúng tin vừa gửi
ACK_DONE = "XONG"  # mã worker viết ở dòng cuối khi đã làm xong và đã gửi link tải
CONTROLLER_MAX_REASK = 1  # chưa thấy mã thì nhắc thêm tối đa từng ấy lần
WORKER_MAX_REASK = 2
RULES_VERSION = 6  # tăng khi đổi luật sản phẩm: thread Giám đốc cũ được báo lại một lần
RULES_FILE = os.path.join(FACTORY_ROOT, "docs", "LUAT_SAN_PHAM.md")
PRODUCT_RULES_INSTRUCTION = (
    "LUẬT SẢN PHẨM & HARNESS NATIVE (Chủ tịch đã duyệt):\n"
    "(1) Toàn bộ mã nguồn dự án được lưu trữ và thao tác trực tiếp trên ổ đĩa máy chủ (workspace).\n"
    "(2) TUYỆT ĐỐI KHÔNG yêu cầu Chủ tịch gửi file ZIP hoặc nén ZIP.\n"
    "(3) Mọi thao tác đọc file đều dùng khối xem_file (mỗi dòng một file), mọi thao tác sửa mã đều dùng khối SEARCH/REPLACE.\n"
    "(4) Công xưởng sẽ tự động đọc file và áp dụng trực tiếp các thay đổi lên đĩa mà không cần làm thủ công."
)
HUONG_DAN_AI_STUB = """# HƯỚNG DẪN CHO AI (đọc file này đầu tiên)

## Vai của bạn
- Giám đốc: trao đổi trực tiếp 1-1 với Chủ tịch, đọc file qua khối xem_file, sửa mã qua khối SEARCH/REPLACE trên workspace đĩa.
- Kỹ sư AI: nhận việc từ Giám đốc và thực thi chính xác trên mã nguồn đĩa.
"""
DOWNLOAD_REASK_MAX = 2  # link tải không bấm được: nhờ AI tạo lại với tên mới, tối đa từng ấy lần
DOWNLOAD_REASK = (
    "Factory không tải được file {names} (link không cho tải). Hãy tạo lại file đó với tên mới (thêm hậu tố _v2, _v3...) "
    "và gửi link tải; chữ của link đúng bằng tên file."
)
DISPATCH_INSTRUCTION = (
    "QUY TẮC ĐIỀU PHỐI VÀ QUẢN LÝ MÃ NGUỒN HARNESS NATIVE:\n"
    "1. Toàn bộ mã nguồn dự án được lưu trữ và thao tác trực tiếp trên ổ đĩa máy chủ (workspace).\n"
    "2. Khi cần đọc bất kỳ file nào, hãy xuất khối xem_file. Khi cần sửa code, xuất khối SEARCH/REPLACE. "
    "Công xưởng sẽ tự động bóc tách và áp dụng trực tiếp lên đĩa cho Chủ tịch mà không cần nén ZIP.\n"
    "3. Khi muốn phân chia công việc cho các kỹ sư (subagent-1 đến subagent-5), hãy dùng khối ```giao_viec với format JSON chỉ rõ vai trò và nhiệm vụ cụ thể."
)

SHIP_WORKER_PROMPT = (
    "Bạn là worker {label} trong công xưởng phần mềm. Đọc `TAI_LIEU_SONG/HUONG_DAN_AI.md` và `LUAT_SAN_PHAM.md` trong `source_goc.zip` trước. "
    "Đính kèm có hai file zip: `source_goc.zip` là mã nguồn full mới nhất, "
    "và `ban_giao.zip` là zip hợp đồng của Giám đốc. Hãy giải nén cả hai trong môi trường của bạn và làm HỢP ĐỒNG {label}: mở thư mục `worker_{label}` trong zip hợp đồng, "
    "đọc hợp đồng và chỉ làm đúng phần đó; chỉ sửa hoặc tạo file nằm trong danh sách của hợp đồng {label}, không đụng file thuộc hợp đồng khác. "
    "Làm xong, nén kết quả (chỉ các file đã sửa hoặc tạo mới, giữ đúng đường dẫn) thành `alpha_{label}.zip` và cung cấp link tải; báo cáo ngắn (nếu cần) ghi trong `BAO_CAO_{label}.md` cùng zip đó. "
    "Khi đã cung cấp link tải, kết thúc tin nhắn bằng đúng một dòng `XONG-{token}`; chưa xong thì không viết dòng đó. Trả lời ngắn gọn."
)

SHIP_WORKER_REASK = (
    "Factory chưa nhận được file alpha_{label}.zip từ tin trước (hoặc chưa thấy dòng XONG-{token}). "
    "Nếu bạn đã làm xong, hãy cung cấp lại link tải alpha_{label}.zip rồi kết thúc bằng đúng một dòng XONG-{token}. "
    "Nếu chưa xong, hãy làm tiếp phần của worker {label} cho tới khi xong."
)

def new_ack_token() -> str:
    return uuid.uuid4().hex[:6].upper()


def ack_text(prefix: str, token: str) -> str:
    return f"{prefix}-{token}"


def has_ack(reply: str, prefix: str, token: str) -> bool:
    """Chuỗi xác nhận có trong câu trả lời không (không phân biệt hoa thường, chịu được định dạng ** hay ` bọc quanh)."""
    return ack_text(prefix, token).lower() in (reply or "").lower()


def strip_ack(reply: str, prefix: str, token: str) -> str:
    """Bỏ dòng chứa chuỗi xác nhận khỏi câu trả lời để không hiện trong chat hay nhật ký."""
    return re.sub(r"(?im)^.*" + re.escape(ack_text(prefix, token)) + r".*\n?", "", reply or "").strip()


# ---- Mã gọi vai: mọi hoạt động chạy nhờ trigger. Mỗi vai trò có một chuỗi mã riêng (khác nhau theo từng project); ai viết đúng mã thì vai đó bắt đầu ----
ROLE_CALL_TAGS = {"director": "GIAMDOC", "shipper": "SHIP", "roadmap": "ROADMAP"}


def new_role_codes() -> Dict[str, str]:
    return {role: f"GOI-{tag}-{uuid.uuid4().hex[:6].upper()}" for role, tag in ROLE_CALL_TAGS.items()}


def find_role_calls(text: str, codes: Dict[str, str]) -> List[tuple]:
    """Các lần gọi vai trong một đoạn chữ: [(vai, nội dung đi kèm)] theo thứ tự xuất hiện.
    Nội dung là phần chữ ngay sau mã, tới mã kế tiếp hoặc hết tin."""
    raw = text or ""
    hits = []
    for role, code in codes.items():
        for match in re.finditer(re.escape(code), raw, re.IGNORECASE):
            hits.append((match.start(), match.end(), role))
    hits.sort()
    calls = []
    for index, (_, end, role) in enumerate(hits):
        stop = hits[index + 1][0] if index + 1 < len(hits) else len(raw)
        calls.append((role, raw[end:stop].strip(" \t\r\n:-–—*`_>")))
    return calls


def strip_role_codes(text: str, codes: Dict[str, str]) -> str:
    """Bỏ các mã gọi vai khỏi chữ hiển thị cho Chủ tịch (giữ phần chữ quanh mã)."""
    out = text or ""
    for code in codes.values():
        out = re.sub(r"[`*_ \t]*" + re.escape(code) + r"[`*_ \t]*", " ", out, flags=re.IGNORECASE)
    out = re.sub(r"[ \t]+\n", "\n", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


_HANDOVER_HINTS = ("hop_dong", "hopdong", "hop dong", "hợp đồng", "contract", "ban_giao", "bangiao", "ban giao", "bàn giao", "handover", "giao_viec")
_SOURCE_HINTS = ("source", "nguon", "nguồn", "goc", "gốc")


CONTROLLER_MAX_STEPS = 12
CONTROLLER_ROLES = {
    "shipper": ("Người ship", "🚚"),
    "roadmap": ("Trợ lý roadmap", "🧭"),
}
CONTROLLER_COMMAND_RE = re.compile(r"```lenh\s*(.*?)```", re.DOTALL)
_LENH_PLAIN_RE = re.compile(r"(?im)^[ \t]*lenh[ \t]*(?:\n|(?=[\[{]))")

SHIPPER_PROMPT = (
    "Bạn là Người ship của công xưởng phần mềm. Bạn làm thay phần vận chuyển và build mà Chủ tịch từng làm tay: Giám đốc (GPT) trò chuyện và tạo zip, "
    "bạn không trò chuyện thay Giám đốc và không mở zip ra đọc. Khi Giám đốc gửi ra zip, hãy đọc tin của Giám đốc để biết chia làm mấy phiên (worker A, B, C...), "
    "rồi điều khiển công xưởng bằng lệnh. Viết lệnh trong một khối, mỗi lượt một khối, rồi chờ kết quả:\n"
    "```lenh\n[{\"lenh\": \"mo_worker\", \"nhan\": [\"A\", \"B\"]}]\n```\n"
    "Các lệnh:\n"
    "- xem_tin_giam_doc: xem tin mới nhất của Giám đốc và các zip đã tải về.\n"
    "- mo_worker {nhan: [...]}: mở mỗi nhãn một phiên mới, gửi hai zip (full source mới nhất và zip hợp đồng), nhắc làm hợp đồng của nhãn đó, chờ worker trả alpha.\n"
    "- mang_ve_giam_doc: gửi các alpha đã về vào thread Giám đốc và nhận lại phản hồi của Giám đốc.\n"
    "- build_app: giải nén zip mã nguồn mới nhất của Giám đốc trên máy Mac và chạy TAI_LIEU_SONG/BUILD.json theo thứ tự backup, install, test, deploy; bước nào lỗi thì factory tự chạy rollback. "
    "Trả về PASS hoặc FAIL kèm log và ROLLBACK OK/FAILED. Nếu ROLLBACK FAILED thì bao_chu_tich ngay và không build lại.\n"
    "- xem_cay: xem cây thư mục mã nguồn hiện tại trong workspace.\n"
    "- doc_code {path: '...', dong: 1, so_dong: 100}: đọc lát cắt mã nguồn của file kèm số dòng.\n"
    "- tim_code {tu_khoa: '...'}: tìm kiếm từ khóa, hàm, biến trong toàn bộ mã nguồn.\n"
    "- va_code {path: '...', tim: '...', thay: '...'}: áp dụng bản vá Search/Replace trực tiếp vào file trên đĩa mà không cần zip.\n"
    "- tao_file {path: '...', noi_dung: '...'}: tạo hoặc ghi đè file mới trong workspace.\n"
    "- gui_log_giam_doc: gửi log build vào thread Giám đốc và nhận lại phản hồi; nếu Giám đốc gửi zip mới thì build lại.\n"
    "- bao_chu_tich {noi_dung}: báo Chủ tịch và kết thúc, khi xong hoặc khi cần Chủ tịch quyết.\n"
    "Quy trình: có việc song song thì mo_worker rồi mang_ve_giam_doc. Giám đốc tự sửa mã trong zip và gửi MỘT zip full source (đã chứa thay đổi, kèm BUILD.json) mà không chia việc song song: "
    "build_app ngay, rồi gui_log_giam_doc. Sau mỗi lần Giám đốc gửi zip mã nguồn mới (sau khi merge hoặc sau khi sửa lỗi) cũng build_app rồi gui_log_giam_doc, lặp đến khi build PASS. "
    "Công xưởng ghi sẵn bên cạnh mỗi zip nó có mã nguồn hay chỉ có tài liệu: zip chỉ có tài liệu thì trả lời 'không cần ship' và không viết lệnh. Build PASS thì viết bao_chu_tich."
)

ROADMAP_PROMPT = (
    "Bạn là Trợ lý roadmap, người giám sát của công xưởng phần mềm. Giám đốc (GPT) giữ tài liệu sống trong mỗi zip (thư mục TAI_LIEU_SONG: ROADMAP.md, CHECKLIST.md). "
    "Việc của bạn: xem tài liệu trong zip mới nhất và chỉ yêu cầu Giám đốc hoàn thiện tiếp những mục còn thiếu. Bạn không tự làm việc kỹ thuật và không sửa tài liệu. "
    "Viết lệnh trong một khối, mỗi lượt một khối, rồi chờ kết quả:\n"
    "```lenh\n[{\"lenh\": \"nhac_giam_doc\", \"noi_dung\": \"Hoàn thiện các mục còn thiếu trong CHECKLIST\"}]\n```\n"
    "Các lệnh:\n"
    "- doc_tai_lieu: đọc TAI_LIEU_SONG trong zip mới nhất của Giám đốc (roadmap, checklist) và đếm mục chưa xong.\n"
    "- nhac_giam_doc {noi_dung}: nhắn Giám đốc, nêu rõ mục còn thiếu và yêu cầu cập nhật tài liệu sống. Nếu Giám đốc chia việc song song thì Người ship tự lo vận chuyển và build, bạn nhận lại phản hồi cuối.\n"
    "- bao_chu_tich {noi_dung}: báo Chủ tịch và kết thúc, khi checklist đã xong hoặc Giám đốc cần Chủ tịch quyết.\n"
    "Quy trình: doc_tai_lieu; còn mục chưa xong hoặc giai đoạn chưa làm thì nhac_giam_doc, rồi doc_tai_lieu lại; hết mục thì bao_chu_tich. "
    "Chưa có zip nào thì nhac_giam_doc yêu cầu Giám đốc tạo zip kèm TAI_LIEU_SONG."
)


def parse_controller_commands(text: str) -> list:
    """Đọc khối ```lenh``` (JSON) trong câu trả lời của agent điều khiển. Trả về danh sách lệnh hợp lệ."""
    commands = []
    bodies = [match.group(1).strip() for match in CONTROLLER_COMMAND_RE.finditer(text or "")]
    if not bodies:
        # Khối ``` không ghi nhãn: nhận khi JSON bên trong có khóa "lenh"
        bodies = [m.group(1).strip() for m in re.finditer(r"```[A-Za-z_]*[ \t]*\n(.*?)```", text or "", re.DOTALL) if '"lenh"' in m.group(1)]
    if not bodies:
        # Chữ đã vẽ từ giao diện web có thể mất dấu ```: nhãn "lenh" đứng riêng một dòng và JSON nằm ngay sau
        decoder = json.JSONDecoder()
        for match in _LENH_PLAIN_RE.finditer(text or ""):
            try:
                data, _ = decoder.raw_decode((text or "")[match.end():].lstrip())
            except ValueError:
                continue
            bodies.append(json.dumps(data))
    for body in bodies:
        try:
            data = json.loads(body)
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("lenh"):
                commands.append(item)
    return commands[:6]


def looks_like_contract_zip(path: str) -> bool:
    """Zip hợp đồng có các thư mục worker_A, worker_B... (công xưởng mở zip xem, không phải AI)."""
    try:
        with zipfile.ZipFile(path) as zf:
            return any(re.search(r"(^|/)worker[_ -]?[A-Za-z]/", n, re.IGNORECASE) for n in zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return False


def classify_ship_zips(paths: List[str]) -> Optional[tuple]:
    """Phân biệt zip full source và zip hợp đồng. Ưu tiên nhìn nội dung (zip có thư mục worker_X là hợp đồng), không rõ thì theo tên file. Trả về (source, handover) hoặc None."""
    def newest(items: List[str]) -> str:
        return max(items, key=lambda x: os.path.getmtime(x) if os.path.exists(x) else 0)  # nhiều zip mã nguồn thì lấy bản mới nhất

    zips = [p for p in paths if p.lower().endswith(".zip")]
    by_content = [p for p in zips if looks_like_contract_zip(p)]
    if len(by_content) == 1 and len(zips) >= 2:
        others = [p for p in zips if p != by_content[0]]
        named = [p for p in others if any(s in os.path.basename(p).lower() for s in _SOURCE_HINTS)]
        return newest(named or others), by_content[0]
    handover = [p for p in zips if any(h in os.path.basename(p).lower() for h in _HANDOVER_HINTS)]
    source = [p for p in zips if p not in handover and any(s in os.path.basename(p).lower() for s in _SOURCE_HINTS)]
    if handover and not source:
        source = [p for p in zips if p not in handover]
    if source and not handover:
        handover = [p for p in zips if p not in source]
    if not handover or not source:
        if len(zips) == 2:  # tên không rõ: file lớn hơn là mã nguồn gốc
            by_size = sorted(zips, key=lambda p: os.path.getsize(p) if os.path.exists(p) else 0, reverse=True)
            return by_size[0], by_size[1]
        return None
    return newest(source), newest(handover)


def alpha_paths(zip_path: str) -> set:
    """Các đường dẫn file trong một alpha (bỏ thư mục gốc chung nếu có, bỏ báo cáo), để so xem hai worker có cùng sửa một file không."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/") and "__MACOSX" not in n and not n.endswith(".DS_Store")]
    except (zipfile.BadZipFile, OSError):
        return set()
    tops = {n.split("/")[0] for n in names if "/" in n}
    if names and len(tops) == 1 and all("/" in n for n in names):
        names = [n.split("/", 1)[1] for n in names]
    cleaned = {clean_file_path(n) for n in names}
    return {n for n in cleaned if n and not os.path.basename(n).upper().startswith("BAO_CAO")}


def find_overlaps(alphas: Dict[str, str]) -> Dict[str, List[str]]:
    """File xuất hiện trong alpha của từ hai worker trở lên (hợp đồng bị lệch): {đường dẫn: [nhãn...]}."""
    owners: Dict[str, List[str]] = {}
    for label, path in alphas.items():
        for item in alpha_paths(path):
            owners.setdefault(item, []).append(label)
    return {item: labels for item, labels in sorted(owners.items()) if len(labels) > 1}


FILE_TAG_OPEN_RE = re.compile(r"""<file\s+path=["']([^"']+)["']\s*>""", re.IGNORECASE)
FILE_TAG_BLOCK_RE = re.compile(r"""<file\s+path=["']([^"']+)["']\s*>\n?(.*?)\n?</file>""", re.DOTALL | re.IGNORECASE)


def clean_file_path(raw_path: str) -> str:
    """Chuẩn hóa đường dẫn file trong câu trả lời; bỏ đường dẫn tuyệt đối và đường dẫn thoát khỏi thư mục dự án."""
    path = str(raw_path or "").strip().replace("\\", "/")
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def _strip_code_fence(body: str) -> str:
    """Bỏ dòng ```lang và ``` bao ngoài nếu model lỡ bọc mã trong khối markdown."""
    lines = body.strip("\n").split("\n")
    if len(lines) >= 2 and lines[0].lstrip().startswith("```") and lines[-1].strip() == "```":
        lines = lines[1:-1]
    return "\n".join(lines) + "\n"


def parse_file_tags(text: str) -> tuple:
    """Đọc các thẻ <file path="...">...</file> trong câu trả lời chữ. DeepSeek không tạo zip nên mã nguồn đi bằng thẻ này. Trả về (file đầy đủ, đường dẫn bị cắt giữa chừng)."""
    raw = text or ""
    files: Dict[str, str] = {}
    spans = []
    for match in FILE_TAG_BLOCK_RE.finditer(raw):
        spans.append(match.span())
        path = clean_file_path(match.group(1))
        if path:
            files[path] = _strip_code_fence(match.group(2))
    cut_off = []
    for match in FILE_TAG_OPEN_RE.finditer(raw):
        if not any(start <= match.start() < end for start, end in spans):
            cut_off.append(clean_file_path(match.group(1)) or match.group(1))
    return files, cut_off


# ---- Lệnh build chạy thẳng trên máy của Chủ tịch (chủ đích). Bộ lọc chỉ chặn những thứ rollback không cứu được ----
_PERSONAL_DIRS = {"Documents", "Desktop", "Downloads", "Pictures", "Movies", "Music", "Library", "Applications", "Public", ".ssh", ".aws", ".gnupg"}
_SYSTEM_DIRS = {"/usr/local", "/usr/bin", "/usr/lib", "/usr/sbin", "/opt/homebrew", "/System/Library", "/Library/Frameworks"}
_UNSAFE_BUILD_RE = re.compile(
    r"(?i)\bsudo\b"
    r"|\|\s*(?:ba|z|da|k)?sh\b"  # tải rồi chạy thẳng qua pipe
    r"|\bmkfs\b|\bdd\s[^|;&]*\bof=/dev/|>\s*/dev/r?disk|\bdiskutil\s+(?:erase|partition|reformat|secureerase|apfs\s+delete)"
    r"|\b(?:cat|cp|mv|tar|zip|base64|less|more|head|tail|strings|xxd)\b[^|;&]*(?:\.ssh/id_|\.aws/credentials|\.gnupg|Library/Keychains|login\.keychain)"
    r"|\bsecurity\s+(?:find-[\w-]*password|dump-keychain|export)"
)


def dangerous_path(path: str) -> bool:
    """Đường dẫn mà xóa hoặc ghi đè là không cứu được: gốc ổ đĩa, thư mục hệ thống, cả thư mục home, thư mục cá nhân, khóa."""
    text = (path or "").strip()
    if text.endswith("/*"):
        text = text[:-2] or "/"
    p = os.path.normpath(text) if text else ""
    if not p:
        return False
    if not os.path.isabs(p):
        return p == ".." or p.startswith("../")
    parts = [x for x in p.split("/") if x]
    home = os.path.normpath(os.path.expanduser("~"))
    if not parts or len(parts) == 1 or (len(parts) == 2 and parts[0] == "Users") or p == home or p in _SYSTEM_DIRS:
        return True
    return p.startswith(home + "/") and p[len(home) + 1:] in _PERSONAL_DIRS


def _rm_is_dangerous(tokens: List[str]) -> bool:
    for index, token in enumerate(tokens):
        if os.path.basename(token) != "rm":
            continue
        args = tokens[index + 1:]
        recursive = any((a.startswith("-") and not a.startswith("--") and "r" in a[1:].lower()) or a == "--recursive" for a in args)
        if recursive and any(not a.startswith("-") and dangerous_path(os.path.expandvars(os.path.expanduser(a))) for a in args):
            return True
    return False


def zip_has_code(path: str) -> bool:
    """Zip có file nào ngoài thư mục TAI_LIEU_SONG không (tức là có mã nguồn hoặc phiếu giao việc), hay chỉ có tài liệu."""
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                low = name.lower()
                if name.endswith("/") or "__macosx" in low or low.endswith(".ds_store"):
                    continue
                if "tai_lieu_song/" not in low:
                    return True
    except (zipfile.BadZipFile, OSError):
        return True  # không đọc được thì cứ coi là có mã để Người ship tự quyết
    return False


def zip_is_artifact(path: str) -> bool:
    """Zip sản phẩm cho Chủ tịch (gói theme, báo cáo, tài liệu rời...), không phải mã nguồn cần build: Giám đốc đặt tên có chữ 'artifact'."""
    return "artifact" in os.path.basename(path).lower()


def describe_zip(path: str) -> str:
    """Một dòng mô tả zip cho Người ship (việc mở zip là của công xưởng, không phải của AI)."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
        has_build = any(n.lower().endswith("tai_lieu_song/build.json") for n in names)
        kind = "sản phẩm (artifact) cho Chủ tịch, không cần build" if zip_is_artifact(path) else ("có mã nguồn" if zip_has_code(path) else "chỉ có tài liệu TAI_LIEU_SONG, không có mã nguồn")
        return f"{os.path.basename(path)}: {len(names)} file, {kind}" + (", có BUILD.json" if has_build else "")
    except (zipfile.BadZipFile, OSError):
        return f"{os.path.basename(path)}: không đọc được"


def load_rules_text() -> str:
    try:
        with open(RULES_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def context_files_for_zip() -> Dict[str, str]:
    """Luật sản phẩm và hướng dẫn khung mà factory bảo đảm có trong mọi zip gửi đi, để phiên nào cũng bắt đầu được ngay."""
    files = {"TAI_LIEU_SONG/HUONG_DAN_AI.md": HUONG_DAN_AI_STUB}
    rules = load_rules_text()
    if rules:
        files["TAI_LIEU_SONG/LUAT_SAN_PHAM.md"] = rules
    return files


def ensure_context_files(zip_path: str) -> List[str]:
    """Thêm vào zip những file ngữ cảnh còn thiếu (không ghi đè file Giám đốc đã viết). Trả về danh sách file vừa thêm."""
    added: List[str] = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = [n.lower() for n in zf.namelist()]
        missing = {n: c for n, c in context_files_for_zip().items() if not any(x.endswith(n.lower()) for x in names)}
        if missing:
            with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED) as zf:
                for name, content in missing.items():
                    zf.writestr(name, content)
                    added.append(name)
    except (zipfile.BadZipFile, OSError):
        pass
    return added


def ensure_handover_contracts(handover_path: str, labels: List[str], director_inbox_dir: str) -> List[str]:
    """
    Đảm bảo gói bàn giao ban_giao.zip có đầy đủ hợp đồng cho tất cả worker_X được yêu cầu trong labels.
    Nếu thiếu worker nào (ví dụ Giám đốc chỉ tạo A, B, C mà thiếu D, E),
    tự động lùng tìm trong các file zip hợp đồng trước đó của director_inbox để bù vào.
    Trả về danh sách các nhãn worker vừa được bổ sung tự động.
    """
    if not os.path.exists(handover_path) or not zipfile.is_zipfile(handover_path):
        return []
    try:
        with zipfile.ZipFile(handover_path, "r") as zf:
            existing_names = zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return []

    missing_labels = []
    for lbl in labels:
        lbl_upper = lbl.upper()
        has_contract = any(re.search(rf"(^|/)worker[_\s-]?{re.escape(lbl_upper)}/", n, re.IGNORECASE) for n in existing_names)
        if not has_contract:
            missing_labels.append(lbl_upper)

    if not missing_labels:
        return []

    inbox_zips = []
    if os.path.isdir(director_inbox_dir):
        for fname in os.listdir(director_inbox_dir):
            if fname.lower().endswith(".zip"):
                fpath = os.path.join(director_inbox_dir, fname)
                if fpath != handover_path and looks_like_contract_zip(fpath):
                    inbox_zips.append(fpath)
    inbox_zips.sort(key=lambda p: os.path.getmtime(p), reverse=True)

    recovered = []
    files_to_add = {}
    for lbl in missing_labels:
        found_for_lbl = False
        for old_zip in inbox_zips:
            try:
                with zipfile.ZipFile(old_zip, "r") as oz:
                    for name in oz.namelist():
                        if re.search(rf"(^|/)worker[_\s-]?{re.escape(lbl)}/", name, re.IGNORECASE) and not name.endswith("/"):
                            files_to_add[name] = oz.read(name)
                            found_for_lbl = True
            except Exception:
                continue
            if found_for_lbl:
                recovered.append(lbl)
                break

    if files_to_add:
        try:
            with zipfile.ZipFile(handover_path, "a", zipfile.ZIP_DEFLATED) as zf:
                for n, b in files_to_add.items():
                    zf.writestr(n, b)
        except Exception:
            pass

    return recovered


class CourierWorker:
    """
    Worker Giao liên: Chuyên thu nhận file .zip alpha hoặc gói mã nguồn từ
    các phiên Subagent và đóng gói thành các gói alpha chuẩn gửi về cho Giám đốc.
    """

    def __init__(self, project_dir: str, log_callback):
        self.project_dir = project_dir
        self.alpha_dir = os.path.join(project_dir, "alpha_packages")
        os.makedirs(self.alpha_dir, exist_ok=True)
        self.log = log_callback

    def collect_subagent_delivery(
        self, task_id: str, assigned_to: str, bridge_response: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Lấy file từ Subagent (cả file .zip tải trực tiếp lẫn khối code JSON) và đóng gói Alpha ZIP."""
        self.log(
            "courier",
            f"📦 [Giao liên] Đang tiếp nhận bàn giao task '{task_id}' từ {assigned_to}...",
        )
        extracted_files: Dict[str, str] = {}
        downloaded_zips = bridge_response.get("downloaded_files") or []

        for zip_path in downloaded_zips:
            if os.path.exists(zip_path) and zipfile.is_zipfile(zip_path):
                self.log("courier", f"✅ [Giao liên] Thu được file ZIP gốc từ ChatGPT: {os.path.basename(zip_path)}")
                with zipfile.ZipFile(zip_path, "r") as zf:
                    for member in zf.namelist():
                        if member.endswith("/") or "__MACOSX" in member:
                            continue
                        safe_member = clean_file_path(member)  # chặn "../" và đường dẫn tuyệt đối trong zip
                        if not safe_member:
                            self.log("courier", f"⚠️ [Giao liên] Bỏ qua mục zip có đường dẫn không an toàn: {member[:120]}")
                            continue
                        try:
                            extracted_files[safe_member] = zf.read(member).decode("utf-8", errors="replace")
                        except Exception:
                            pass

        content = (
            bridge_response.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        parsed = parse_json_safely(content)
        for file_obj in parsed.get("files", []):
            fpath = clean_file_path(file_obj.get("path") or "")
            fcontent = file_obj.get("content")
            if fpath and fcontent is not None:
                extracted_files[fpath] = fcontent
        # DeepSeek chỉ trả chữ: mã nguồn nằm trong các thẻ <file path="...">
        tagged_files, cut_off = parse_file_tags(content)
        extracted_files.update(tagged_files)
        if cut_off:
            self.log("courier", f"⚠️ [Giao liên] {len(cut_off)} file bị cắt giữa chừng, chưa nhận: {', '.join(cut_off[:10])}")

        alpha_zip_path = os.path.join(self.alpha_dir, f"alpha_{task_id}_{assigned_to}.zip")
        with zipfile.ZipFile(alpha_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel_path, file_code in extracted_files.items():
                zf.writestr(rel_path, file_code)

        self.log(
            "courier",
            f"📨 [Giao liên -> Giám đốc] Đã bàn giao gói '{os.path.basename(alpha_zip_path)}' ({len(extracted_files)} files).",
        )
        return {
            "task_id": task_id,
            "assigned_to": assigned_to,
            "alpha_zip_path": alpha_zip_path,
            "files": extracted_files,
            "summary": parsed.get("summary", f"Hoàn thành {len(extracted_files)} files"),
        }


# ============================================================================
# 4. WORKER THỰC THI & KIỂM THỬ (BUILDER & RUNNER WORKER)
# ============================================================================

class BuilderRunnerWorker:
    """
    Worker Thực thi: Chuyên nhận full_source.zip từ Giám đốc, giải nén vào môi trường
    build trên máy Mac, chạy lệnh cài đặt & kiểm thử, và gửi toàn bộ log về cho Giám đốc
    bất kể PASS hay FAIL.
    """

    def __init__(self, project_dir: str, log_callback):
        self.project_dir = project_dir
        self.build_workspace = os.path.join(project_dir, "build_workspace")
        self.log = log_callback

    def collect_artifacts(self, patterns: List[str], iteration: int) -> Dict[str, Any]:
        """Gom các file build tạo ra (ví dụ gói theme đã ký) khai trong BUILD.json khóa artifacts thành một zip để Chủ tịch tải về.
        Chỉ nhận mẫu đường dẫn tương đối trong thư mục build; tối đa 200 MB."""
        root = os.path.realpath(self.build_workspace)
        out_root = os.path.join(self.project_dir, "build_outputs")
        out_dir = os.path.join(out_root, f"iter_{iteration}")
        shutil.rmtree(out_dir, ignore_errors=True)
        files: List[Dict[str, Any]] = []
        total = 0
        for pattern in patterns[:20]:
            text = str(pattern or "").strip()
            if not text or os.path.isabs(text) or ".." in text.replace("\\", "/").split("/"):
                continue
            for found in sorted(glob.glob(os.path.join(root, text), recursive=True)):
                real = os.path.realpath(found)
                if not os.path.isfile(real) or os.path.commonpath([root, real]) != root:
                    continue
                rel = os.path.relpath(real, root).replace(os.sep, "/")
                size = os.path.getsize(real)
                if total + size > 200 * 1024 * 1024 or any(f["path"] == rel for f in files):
                    continue
                dest = os.path.join(out_dir, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(real, dest)
                with open(real, "rb") as fh:
                    digest = hashlib.sha256(fh.read()).hexdigest()
                files.append({"path": rel, "size": size, "sha256": digest})
                total += size
        zip_path = None
        if files:
            zip_path = os.path.join(out_root, f"build_{iteration}_artifact.zip")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for item in files:
                    zf.write(os.path.join(out_dir, item["path"]), item["path"])
        return {"files": files, "zip": zip_path}

    async def execute_and_report(
        self, full_zip_path: str, install_cmd: str, test_cmd: str, iteration: int,
        backup_cmd: str = "", deploy_cmd: str = "", rollback_cmd: str = "", target: str = "", artifacts: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Giải nén zip rồi chạy lần lượt backup, install, test, deploy. Bước nào lỗi hoặc quá giờ thì chạy rollback (nếu có)."""
        self.log(
            "builder",
            f"🔨 [Worker Thực thi] Nhận '{os.path.basename(full_zip_path)}' (Vòng kiểm thử #{iteration}). Đang giải nén...",
        )

        # Thử ủy quyền sang Remote Builder nếu cấu hình biến môi trường
        remote_url = os.environ.get("FACTORY_REMOTE_BUILD_URL")

        if remote_url:
            try:
                import base64, requests
                with open(full_zip_path, "rb") as zf:
                    b64_zip = base64.b64encode(zf.read()).decode("utf-8")
                payload = {
                    "project_id": os.path.basename(self.project_dir),
                    "iteration": iteration,
                    "install_cmd": install_cmd,
                    "test_cmd": test_cmd,
                    "deploy_cmd": deploy_cmd,
                    "backup_cmd": backup_cmd,
                    "rollback_cmd": rollback_cmd,
                    "target": target,
                    "artifacts": artifacts,
                    "zip_base64": b64_zip,
                    "auto_android": True,
                }
                self.log("builder", f"🚀 [Worker Thực thi] Ủy quyền build Flutter/Android sang máy Mac ({remote_url})...")
                resp = await asyncio.to_thread(requests.post, remote_url, json=payload, timeout=600)
                if resp.status_code == 200:
                    data = resp.json()
                    passed = data.get("passed", False)
                    exit_code = data.get("exit_code", 0)
                    failed_step = data.get("failed_step", "")
                    full_log_text = data.get("log", "")

                    log_file = os.path.join(self.project_dir, f"build_run_iter_{iteration}.log")
                    with open(log_file, "w", encoding="utf-8") as f:
                        f.write(full_log_text)

                    status_icon = "✅ PASS" if passed else "❌ FAIL"
                    if data.get("apk_installed"):
                        status_icon += f" · 📱 ĐÃ CÀI & MỞ TRÊN {data.get('device')}"
                    self.log(
                        "builder",
                        f"📋 [Worker Thực thi -> Giám đốc] Kết quả build trên Mac vòng #{iteration}: {status_icon} (Exit Code: {exit_code}). Đã gửi toàn bộ log cho Giám đốc!",
                    )
                    return {
                        "iteration": iteration,
                        "passed": passed,
                        "exit_code": exit_code,
                        "failed_step": failed_step,
                        "rolled_back": False,
                        "rollback_ok": None,
                        "artifacts": [],
                        "artifact_zip": None,
                        "log": full_log_text,
                        "log_file": log_file,
                    }
            except Exception as r_err:
                self.log("builder", f"⚠️ Remote Mac Builder không phản hồi ({r_err}), chuyển sang build cục bộ...")

        staging_dir = os.path.join(self.project_dir, "staging_build", f"iter_{iteration}")
        if os.path.exists(staging_dir):
            shutil.rmtree(staging_dir, ignore_errors=True)
        os.makedirs(staging_dir, exist_ok=True)

        with zipfile.ZipFile(full_zip_path, "r") as zf:
            zf.extractall(staging_dir)

        rollback_dir = os.path.join(self.project_dir, "rollback_store")  # ngoài thư mục build: còn nguyên giữa các vòng
        os.makedirs(rollback_dir, exist_ok=True)
        env_extra = {
            "FACTORY_BUILD_DIR": staging_dir,
            "FACTORY_MAIN_DIR": self.build_workspace,
            "FACTORY_ROLLBACK_DIR": rollback_dir,
            "FACTORY_ITERATION": str(iteration),
        }
        if target:
            env_extra["FACTORY_TARGET"] = target
            os.makedirs(os.path.dirname(target), exist_ok=True)  # thư mục cha của target có sẵn: lần deploy đầu không vấp ở bước đổi tên vào target

        steps = [
            ("BACKUP", backup_cmd, BUILD_INSTALL_TIMEOUT),
            ("INSTALL", install_cmd, BUILD_INSTALL_TIMEOUT),
            ("TEST/BUILD", test_cmd, BUILD_RUN_TIMEOUT),
        ]
        combined_logs = []
        exit_code = 0
        failed_step = ""
        for label, cmd, limit in steps:
            if not (cmd and cmd.strip()):
                continue
            self.log("builder", f"🧪 [Worker Thực thi] {label}: `{cmd}`")
            code, out = await asyncio.to_thread(self._run_shell, cmd, staging_dir, limit, env_extra)
            combined_logs.append(f"=== [{label}]: {cmd} (exit={code}) ===\n{out}")
            if code != 0:
                exit_code, failed_step = code, label
                break

        rolled_back = False
        rollback_ok: Optional[bool] = None

        if exit_code != 0:
            # Build FAIL ở thư mục tạm: Giữ nguyên build_workspace và merged_source 100%
            self.log(
                "builder",
                f"🛡️ [Staging Gate] Build FAIL ở thư mục tạm (staging_build/iter_{iteration}). Nhánh main (build_workspace) được giữ nguyên an toàn 100%!",
            )
            combined_logs.append(f"🛡️ [Staging Gate] Build FAIL ở bước {failed_step}. Main workspace không bị thay đổi.")
            if rollback_cmd and rollback_cmd.strip() and failed_step != "BACKUP":
                rolled_back = True
                self.log("builder", f"↩️ [Worker Thực thi] Bước {failed_step} lỗi: chạy rollback `{rollback_cmd}` trong staging")
                code_r, out_r = await asyncio.to_thread(self._run_shell, rollback_cmd, staging_dir, BUILD_INSTALL_TIMEOUT, env_extra)
                rollback_ok = code_r == 0
                combined_logs.append(f"=== [ROLLBACK]: {rollback_cmd} (exit={code_r}) ===\n{out_r}")
                combined_logs.append("ROLLBACK OK" if rollback_ok else "ROLLBACK FAILED")
        else:
            # Build PASS: Đồng bộ từ staging_dir đè lên self.build_workspace (main) và merged_source, bảo tồn .git
            def _sync_to_target(dst_dir: str):
                os.makedirs(dst_dir, exist_ok=True)
                for item in os.listdir(dst_dir):
                    if item == ".git":
                        continue
                    p = os.path.join(dst_dir, item)
                    if os.path.isdir(p) and not os.path.islink(p):
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                shutil.copytree(staging_dir, dst_dir, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))

            _sync_to_target(self.build_workspace)
            merged_dir = os.path.join(self.project_dir, "merged_source")
            _sync_to_target(merged_dir)

            # Git commit trên nhánh main nếu có git
            try:
                workspace_harness.git_init_workspace(self.build_workspace)
                workspace_harness.git_commit(self.build_workspace, f"Build #{iteration} PASSED", author_role="Worker Thực thi")
            except Exception as g_err:
                self.log("builder", f"⚠️ [Git Commit] Không tạo được commit: {g_err}")

            self.log(
                "builder",
                f"✅ [Staging Gate] Build PASS! Đã đồng bộ bản build #{iteration} từ thư mục tạm đè lên main (build_workspace).",
            )
            combined_logs.append(f"✅ [Staging Gate] Build PASS! Đã đồng bộ bản build #{iteration} lên main.")

            # Chạy DEPLOY nếu có
            if deploy_cmd and deploy_cmd.strip():
                self.log("builder", f"🧪 [Worker Thực thi] DEPLOY: `{deploy_cmd}`")
                deploy_env = dict(env_extra)
                deploy_env["FACTORY_BUILD_DIR"] = self.build_workspace
                code_dep, out_dep = await asyncio.to_thread(self._run_shell, deploy_cmd, self.build_workspace, BUILD_RUN_TIMEOUT, deploy_env)
                combined_logs.append(f"=== [DEPLOY]: {deploy_cmd} (exit={code_dep}) ===\n{out_dep}")
                if code_dep != 0:
                    exit_code, failed_step = code_dep, "DEPLOY"
                    if rollback_cmd and rollback_cmd.strip():
                        rolled_back = True
                        self.log("builder", f"↩️ [Worker Thực thi] Bước DEPLOY lỗi: chạy rollback `{rollback_cmd}` trên main")
                        code_r, out_r = await asyncio.to_thread(self._run_shell, rollback_cmd, self.build_workspace, BUILD_INSTALL_TIMEOUT, deploy_env)
                        rollback_ok = code_r == 0
                        combined_logs.append(f"=== [ROLLBACK]: {rollback_cmd} (exit={code_r}) ===\n{out_r}")
                        combined_logs.append("ROLLBACK OK" if rollback_ok else "ROLLBACK FAILED")

        passed = exit_code == 0
        collected: Dict[str, Any] = {"files": [], "zip": None}
        if passed and artifacts:
            collected = self.collect_artifacts(artifacts, iteration)
            listing = "\n".join(f"- {f['path']} ({f['size']} byte, sha256 {f['sha256'][:16]}…)" for f in collected["files"]) or "(không có file nào khớp)"
            combined_logs.append(f"=== [ARTIFACTS]: {', '.join(artifacts)} ===\n{listing}")
        full_log_text = "\n\n".join(combined_logs).strip() or "Không có output log."

        status_icon = "✅ PASS" if passed else "❌ FAIL"
        if rolled_back:
            status_icon += " · ROLLBACK OK" if rollback_ok else " · 🚨 ROLLBACK FAILED"
        self.log(
            "builder",
            f"📋 [Worker Thực thi -> Giám đốc] Kết quả chạy vòng #{iteration}: {status_icon} (Exit Code: {exit_code}). Đã gửi toàn bộ log cho Giám đốc!",
        )

        log_file = os.path.join(self.project_dir, f"build_run_iter_{iteration}.log")
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(full_log_text)

        return {
            "iteration": iteration,
            "passed": passed,
            "exit_code": exit_code,
            "failed_step": failed_step,
            "rolled_back": rolled_back,
            "rollback_ok": rollback_ok,
            "artifacts": collected["files"],
            "artifact_zip": collected["zip"],
            "log": full_log_text,
            "log_file": log_file,
        }

    @staticmethod
    def _run_shell(cmd: str, cwd: str, timeout: int = 120, env_extra: Optional[Dict[str, str]] = None) -> (int, str):
        try:
            env = os.environ.copy()
            candidate_paths = [
                "/home/thinh/.local/bin",
                "/usr/local/sbin",
                "/usr/local/bin",
                "/usr/sbin",
                "/usr/bin",
                "/sbin",
                "/bin",
                "/opt/homebrew/bin",
            ]
            extra_paths = ":".join(p for p in candidate_paths if os.path.exists(p))
            env["PATH"] = f"{extra_paths}:{env.get('PATH', '')}"
            env.update(env_extra or {})
            proc = subprocess.Popen(
                ["/bin/bash", "-c", cmd],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,  # quá giờ thì giết cả nhóm tiến trình, không để tiến trình con treo mãi
            )
            try:
                out, _ = proc.communicate(timeout=timeout)
                return proc.returncode, out
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                out, _ = proc.communicate()
                return 124, f"Timeout sau {timeout}s:\n{out or ''}"
        except Exception as e:
            return 1, f"Lỗi thực thi lệnh: {str(e)}"

# ============================================================================
# 5. BỘ ĐIỀU PHỐI DỰ ÁN (FACTORY PROJECT SESSION)
# ============================================================================

def _parse_dict(raw: str) -> Dict[str, Any]:
    """Đọc JSON từ phát biểu của model. Không đọc được thì trả về dict rỗng."""
    try:
        data = parse_json_safely(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def parse_delegations(text: str) -> tuple:
    """Tách khối giao_viec / mo_worker khỏi câu trả lời của Giám đốc. Trả về (phần còn lại, việc giao cho kỹ sư)."""
    raw = text or ""
    delegations = []
    cleaned = raw

    # 1. Bắt khối fenced code block ```giao_viec hoặc ```mo_worker
    match = DELEGATION_BLOCK_RE.search(raw)
    if match:
        cleaned = (raw[: match.start()] + raw[match.end():]).strip()
        body = match.group(1).strip()
        items = []
        try:
            items = json.loads(body)
        except Exception:
            try:
                import ast
                items = ast.literal_eval(body)
            except Exception:
                pass
        if isinstance(items, dict):
            items = [items]
        if isinstance(items, list):
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                to_val = str(item.get("to") or item.get("worker") or item.get("subagent") or f"subagent-{idx+1}").strip().lower()
                task = str(item.get("task") or item.get("nhiem_vu") or item.get("content") or "").strip()
                if not task:
                    continue
                m_sub = re.search(r"(\d+)", to_val)
                sub_num = int(m_sub.group(1)) if m_sub else (idx + 1)
                sub_num = min(max(sub_num, 1), 5)
                delegations.append({"to": f"subagent-{sub_num}", "task": task})

    # 2. Bắt khối plain-text giao_viec / mo_worker [...] (khi model quên bọc backticks)
    if not delegations:
        pt_match = re.search(r"(?:giao_viec|mo_worker|delegate)\s*(\[[^\[\]]*(?:\{[^{}]*\}[^\[\]]*)*\])", raw, re.DOTALL | re.IGNORECASE)
        if pt_match:
            cleaned = (raw[: pt_match.start()] + raw[pt_match.end():]).strip()
            body = pt_match.group(1).strip()
            items = []
            try:
                items = json.loads(body)
            except Exception:
                try:
                    import ast
                    items = ast.literal_eval(body)
                except Exception:
                    pass
            if isinstance(items, list):
                for idx, item in enumerate(items):
                    if not isinstance(item, dict):
                        continue
                    to_val = str(item.get("to") or item.get("worker") or item.get("subagent") or f"subagent-{idx+1}").strip().lower()
                    task = str(item.get("task") or item.get("nhiem_vu") or item.get("content") or "").strip()
                    if not task:
                        continue
                    m_sub = re.search(r"(\d+)", to_val)
                    sub_num = int(m_sub.group(1)) if m_sub else (idx + 1)
                    sub_num = min(max(sub_num, 1), 5)
                    delegations.append({"to": f"subagent-{sub_num}", "task": task})

    # 3. Bắt dạng inline [LENH: MO_WORKER: N] hoặc [LENH: GIAO_VIEC]
    inline_re = re.compile(r"\[(?:LENH|COMMAND):\s*(?:MO_WORKER|GIAO_VIEC)(?::\s*([0-9]+))?\]", re.IGNORECASE)
    m_inline = inline_re.search(cleaned)
    if m_inline and not delegations:
        cleaned = (cleaned[: m_inline.start()] + cleaned[m_inline.end():]).strip()
        num_str = m_inline.group(1)
        count = int(num_str) if num_str else 2
        count = min(max(count, 1), 5)
        for i in range(1, count + 1):
            delegations.append({
                "to": f"subagent-{i}",
                "task": f"Triển khai module chuyên trách #{i} theo kiến trúc Giám đốc đã phân tích.",
            })

    return cleaned, delegations[:5]


def parse_file_requests(text: str) -> tuple:
    """Tách khối xem_file/read_file (đường dẫn) và tim_ma/search_code (từ khóa) khỏi câu trả lời. Trả về (phần còn lại, đường dẫn, từ khóa)."""
    raw = text or ""
    paths: List[str] = []
    keywords: List[str] = []

    # 1. Bắt khối fenced code block ```xem_file ... ```
    for match in FILE_BLOCK_RE.finditer(raw):
        block_tag = match.group(1).lower()
        body = match.group(2).strip()
        items: List[str] = []
        if body.startswith("["):
            try:
                loaded = json.loads(body)
                items = [str(item) for item in loaded] if isinstance(loaded, list) else []
            except ValueError:
                items = []
        if not items:
            items = [line.strip().lstrip("-*•").strip().strip("`") for line in body.splitlines()]
        if block_tag in ("xem_file", "read_file", "view_file"):
            paths.extend(item for item in items if item)
        else:
            keywords.extend(item for item in items if item)

    # 2. Bắt dạng Plain text / text thuần mà AI không bọc backticks (do ChatGPT web render text/pre)
    plain_xem = re.finditer(
        r"(?:^|\n)\s*(?:Plain\s*text\s+)?(?:xem_file|read_file|view_file)\s*\n([a-zA-Z0-9_\-\./\\\s]+?)(?=\n\n|\n[A-Z#]|$)",
        raw,
        re.IGNORECASE
    )
    for m in plain_xem:
        for line in m.group(1).splitlines():
            line = line.strip().strip("`").strip("'").strip('"')
            if line and "." in line and not line.startswith("#"):
                paths.append(line)

    plain_tim = re.finditer(
        r"(?:^|\n)\s*(?:Plain\s*text\s+)?(?:tim_ma|search_code)\s*\n([^\n]+(?:\n[^\n]+)*?)(?=\n\n|\n[A-Z#]|$)",
        raw,
        re.IGNORECASE
    )
    for m in plain_tim:
        for line in m.group(1).splitlines():
            line = line.strip().strip("`").strip()
            if line and not line.startswith("#"):
                keywords.append(line)

    # 3. Bắt cú pháp inline: xem_file: apps/api/src/app.controller.ts
    inline_paths = re.findall(r"(?:xem_file|read_file|view_file):\s*([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9]+)", raw, re.IGNORECASE)
    paths.extend(inline_paths)

    inline_kw = re.findall(r"(?:tim_ma|search_code):\s*([^\s\n\r`]+)", raw, re.IGNORECASE)
    keywords.extend(inline_kw)

    # Làm sạch text khỏi các khối trên
    cleaned = FILE_BLOCK_RE.sub("", raw)
    cleaned = re.sub(r"(?:^|\n)\s*(?:Plain\s*text\s+)?(?:xem_file|read_file|view_file)\s*\n[a-zA-Z0-9_\-\./\\\s]+(?=\n\n|\n[A-Z#]|$)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(?:^|\n)\s*(?:Plain\s*text\s+)?(?:tim_ma|search_code)\s*\n[^\n]+(?:\n[^\n]+)*(?=\n\n|\n[A-Z#]|$)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(?:xem_file|read_file|view_file|tim_ma|search_code):\s*[^\s\n\r`]+", "", cleaned, flags=re.IGNORECASE).strip()

    return cleaned, list(dict.fromkeys(paths)), list(dict.fromkeys(keywords))


def _with_reasoning(entry: Dict[str, Any]) -> str:
    """Nội dung tin, kèm khối <reasoning> đã lưu nếu có, để model nhớ lại mạch suy nghĩ của chính nó."""
    if entry.get("reasoning"):
        return f"{entry['content']}\n\n<reasoning>{entry['reasoning']}</reasoning>"
    return entry["content"]


def load_saved_project_states() -> Dict[str, Dict[str, Any]]:
    """Đọc trạng thái đã lưu của mọi dự án trong thư mục projects/. Trả về {project_id: dữ liệu}."""
    states: Dict[str, Dict[str, Any]] = {}
    if not os.path.isdir(PROJECTS_DIR):
        return states
    for name in sorted(os.listdir(PROJECTS_DIR)):
        path = os.path.join(PROJECTS_DIR, name, PROJECT_STATE_FILE)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                states[name] = json.load(f)
        except (OSError, ValueError):
            continue
    return states


class FactoryProject:
    def __init__(self, project_id: str, title: str):
        self.project_id = project_id
        self.title = title
        self.created_at = int(time.time())
        self.status = "brainstorming"  # brainstorming -> secretary_spec -> executing -> completed / failed
        self.project_dir = os.path.join(PROJECTS_DIR, project_id)
        self.merged_dir = os.path.join(self.project_dir, "merged_source")
        os.makedirs(self.merged_dir, exist_ok=True)

        self.council_messages: List[Dict[str, Any]] = []
        self.council_thinking: Optional[str] = None
        self.active_speaker: Optional[str] = None
        self.meeting_minutes_md: str = ""
        self.tech_spec_md: str = ""
        self.install_command: str = ""
        self.test_command: str = ""
        self.run_command: str = ""
        self.roadmap: List[Dict[str, Any]] = []
        self.ship_state: Dict[str, Any] = {"status": "idle", "batch": None, "workers": []}
        self._shipping = False
        self._director_seen: set = set()
        self._pending_zips: List[str] = []
        self._last_director_text = ""
        self._last_shipment = None
        self._last_director_zips: List[str] = []
        self._last_build: Optional[Dict[str, Any]] = None
        self._build_iter = 0
        self._inbox_primed = False
        self.autopilot = False  # Chủ tịch bật: Trợ lý roadmap tự kiểm tra mỗi khi Giám đốc báo hoàn thành
        self.role_codes: Dict[str, str] = new_role_codes()
        self._nudges = 0

        self.subagent_states: Dict[str, Dict[str, Any]] = {
            f"subagent-{i}": {"id": f"subagent-{i}", "status": "idle", "current_task": None, "files_count": 0}
            for i in range(1, 6)
        }
        self.director_state: Dict[str, Any] = {"status": "idle", "current_action": "Chờ chốt dự án"}
        self.courier_state: Dict[str, Any] = {"status": "idle", "deliveries": []}
        self.builder_state: Dict[str, Any] = {"status": "idle", "last_exit_code": None, "last_log": "", "runs": []}

        self.merged_files: Dict[str, str] = {}
        self.sessions: List[Dict[str, Any]] = [
            {
                "session_id": "sess_1",
                "title": "Phiên #1",
                "created_at": self.created_at,
                "message_count": len(self.council_messages),
            }
        ]
        self.current_session_id: str = "sess_1"
        self._sessions_history: Dict[str, List[Dict[str, Any]]] = {
            "sess_1": list(self.council_messages)
        }
        self.full_zip_path: Optional[str] = None
        self.activity_logs: List[Dict[str, Any]] = []
        # Lịch sử riêng của từng vai trò: bản tóm tắt các tin cũ và các tin gần đây
        self.role_memory: Dict[str, Dict[str, Any]] = {}
        # Thread bền của từng vai trò trong bridge: số thế hệ thread, đã nạp lịch sử chưa, số ký tự đã dùng
        self.threads: Dict[str, Dict[str, Any]] = {}
        # Tên Project tương ứng trên ChatGPT: mọi thread của dự án này nằm trong Project đó
        self.chatgpt_project = f"{title} [{project_id}]"
        # Bản ghi quyết định của phiên lập biên bản, và trạng thái chờ Chủ tịch duyệt trước khi triển khai
        self.decision_record: Optional[Dict[str, Any]] = None
        self.awaiting_approval = False
        self.meeting: Optional[Dict[str, Any]] = None
        self._bridge_started_at: Optional[float] = None
        self._subagent_semaphore = asyncio.Semaphore(5)
        self.live_reasoning: Optional[str] = None
        self._event_listeners: List[asyncio.Queue] = []

    def notify_event(self, event_type: str, data: Any = None) -> None:
        """Gửi sự kiện tức thời (SSE) tới các trình duyệt đang kết nối để cập nhật UI thời gian thực."""
        evt = {"type": event_type, "data": data, "timestamp": time.time()}
        listeners = getattr(self, "_event_listeners", None)
        if listeners:
            for q in list(listeners):
                try:
                    q.put_nowait(evt)
                except Exception:
                    pass

    def to_saved_state(self) -> Dict[str, Any]:
        """Lịch sử và trạng thái cần giữ lại khi khởi động lại server: khung chat, lịch sử từng vai trò, thread bridge, phòng họp, quyết định."""
        return {
            "project_id": self.project_id,
            "title": self.title,
            "created_at": self.created_at,
            "status": self.status,
            "chatgpt_project": self.chatgpt_project,
            "council_messages": self.council_messages,
            "role_memory": self.role_memory,
            "threads": self.threads,
            "bridge_started_at": self._bridge_started_at,
            "activity_logs": self.activity_logs[-500:],
            "subagent_states": self.subagent_states,
            "decision_record": self.decision_record,
            "awaiting_approval": self.awaiting_approval,
            "meeting": self.meeting,
            "meeting_minutes_md": self.meeting_minutes_md,
            "tech_spec_md": self.tech_spec_md,
            "roadmap": self.roadmap,
            "install_command": self.install_command,
            "test_command": self.test_command,
            "run_command": self.run_command,
            "full_zip_path": self.full_zip_path,
            "courier_state": self.courier_state,
            "builder_state": {**self.builder_state, "runs": (self.builder_state.get("runs") or [])[-20:]},
            "director_state": self.director_state,
            "autopilot": self.autopilot,
            "role_codes": self.role_codes,
            "sessions": self.sessions_list(),
            "current_session_id": getattr(self, "current_session_id", "sess_1"),
            "_sessions_history": getattr(self, "_sessions_history", {}),
        }

    def save_state(self) -> None:
        """Ghi trạng thái dự án vào thư mục của dự án. Ghi qua file tạm để không hỏng file cũ nếu máy dừng giữa chừng."""
        os.makedirs(self.project_dir, exist_ok=True)
        path = os.path.join(self.project_dir, PROJECT_STATE_FILE)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.to_saved_state(), f, ensure_ascii=False, indent=1)
        os.replace(tmp_path, path)

    def load_saved_state(self, data: Dict[str, Any]) -> None:
        """Khôi phục lịch sử đã lưu. Việc đang chạy dở khi server dừng được đánh dấu là gián đoạn."""
        self.title = data.get("title", self.title)
        self.created_at = data.get("created_at", self.created_at)
        self.status = data.get("status", self.status)
        self.chatgpt_project = data.get("chatgpt_project", self.chatgpt_project)
        self.council_messages = data.get("council_messages", [])
        self.role_memory = data.get("role_memory", {})
        self.sessions = data.get("sessions") or [
            {
                "session_id": "sess_1",
                "title": "Phiên #1",
                "created_at": self.created_at,
                "message_count": len(self.council_messages),
            }
        ]
        self.current_session_id = data.get("current_session_id", "sess_1")
        self._sessions_history = data.get("_sessions_history") or {
            self.current_session_id: list(self.council_messages)
        }
        self.threads = data.get("threads", {})
        self._bridge_started_at = data.get("bridge_started_at")
        self.activity_logs = data.get("activity_logs", [])
        for role_id, state in (data.get("subagent_states") or {}).items():
            if role_id in self.subagent_states:
                self.subagent_states[role_id] = {**state, "status": "idle", "current_task": None}
        self.decision_record = data.get("decision_record")
        self.awaiting_approval = data.get("awaiting_approval", False)
        self.meeting = data.get("meeting")
        if self.meeting and self.meeting.get("status") == "dang_hop":
            self.meeting["status"] = "gian_doan"
        self.meeting_minutes_md = data.get("meeting_minutes_md", "")
        self.tech_spec_md = data.get("tech_spec_md", "")
        self.roadmap = data.get("roadmap", [])
        self.install_command = data.get("install_command", "")
        self.test_command = data.get("test_command", "")
        self.run_command = data.get("run_command", "")
        zip_path = data.get("full_zip_path")
        self.full_zip_path = zip_path if zip_path and os.path.exists(zip_path) else None
        self.courier_state = data.get("courier_state", self.courier_state)
        self.builder_state = data.get("builder_state", self.builder_state)
        self.director_state = {**data.get("director_state", self.director_state), "status": "idle"}
        self.autopilot = bool(data.get("autopilot", False))
        if os.path.isfile(os.path.join(self.project_dir, "director_upload", "project_source.zip")):
            self.threads.setdefault("director", {"gen": 1, "seeded": False, "chars": 0})["had_zip"] = True
        self._prime_inbox_seen()
        saved_codes = data.get("role_codes")
        if isinstance(saved_codes, dict) and all(isinstance(saved_codes.get(role), str) for role in ROLE_CALL_TAGS):
            self.role_codes = {role: saved_codes[role] for role in ROLE_CALL_TAGS}
        self.rebuild_merged_files()
        # Phiên là bền: giữ nguyên thế hệ thread và địa chỉ cuộc trò chuyện đã lưu, để lượt kế tiếp nói tiếp đúng cuộc trò chuyện ChatGPT đang mở
        # (bridge mở lại đúng chỗ theo địa chỉ; không mở lại được thì ask_role tự nạp lại gói khởi đầu).

    def rebuild_merged_files(self) -> None:
        """Đọc lại mã nguồn đã lưu trong merged_source (nguồn sự thật trên đĩa), để ngữ cảnh mã nguồn còn sau khi mở lại dự án."""
        self.merged_files = {}
        if not os.path.isdir(self.merged_dir):
            return
        for root, dirs, files in os.walk(self.merged_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__MACOSX"]
            for name in files:
                full = os.path.join(root, name)
                if name.startswith(".") or os.path.getsize(full) > 2_000_000:
                    continue
                try:
                    with open(full, "r", encoding="utf-8") as f:
                        content = f.read()
                except (OSError, UnicodeDecodeError):
                    continue
                self.merged_files[os.path.relpath(full, self.merged_dir).replace(os.sep, "/")] = content

    def add_log(self, actor: str, message: str):
        entry = {
            "timestamp": time.strftime("%H:%M:%S"),
            "actor": actor,
            "message": message,
        }
        self.activity_logs.append(entry)
        print(f"[{entry['timestamp']}] [{actor.upper()}] {message}")


    # ---- Người ship (mở phiên worker, mang alpha về) và trợ lý roadmap (nhắc Giám đốc làm tiếp) ----
    def director_inbox(self) -> str:
        path = os.path.join(self.project_dir, "director_inbox")
        os.makedirs(path, exist_ok=True)
        return path

    def new_director_zips(self) -> List[str]:
        """Zip Giám đốc vừa gửi ra (GPT tạo, bridge tải về thư mục inbox); mỗi file chỉ báo một lần."""
        inbox = self.director_inbox()
        found = []
        for name in sorted(os.listdir(inbox)):
            full = os.path.join(inbox, name)
            key = (full, os.stat(full).st_mtime_ns) if name.lower().endswith(".zip") else None
            if key and key not in self._director_seen:  # cùng tên nhưng ghi lại sau thì vẫn là zip mới
                self._director_seen.add(key)
                found.append(full)
        if found:
            self._last_director_zips = found
        return found

    async def director_turn(self, text: str, attachments: Optional[List[Dict[str, str]]] = None, allow_rotate: bool = False) -> tuple:
        """Gửi một tin vào thread Giám đốc. Trả về (lời Giám đốc, zip mới Giám đốc gửi ra). allow_rotate: lượt do Chủ tịch gửi, được phép đổi sang phiên Giám đốc mới (đủ 10 lần gửi zip)."""
        persona = COUNCIL_PERSONAS["director"]
        kwargs = role_llm_kwargs("director")
        if kwargs.get("backend") != "chatgpt":
            attachments = None  # DeepSeek chưa nhận file đính kèm
        extra: Dict[str, Any] = {}
        if kwargs.get("backend") == "chatgpt":
            extra = {"expect_zip": True, "download_dir": self.director_inbox()}
        system_prompts = [
            persona["system_prompt"],
            REASONING_INSTRUCTION,
            DISPATCH_INSTRUCTION,
            file_access_instruction(kwargs.get("backend")),
        ]
        has_zip = bool(
            self.threads.get("director", {}).get("had_zip")
            or self.latest_source_zip()
            or attachments
            or os.path.isfile(os.path.join(self.project_dir, "director_upload", "project_source.zip"))
        )
        if not (kwargs.get("backend") == "chatgpt" and has_zip):
            codebase_ctx = self.build_existing_codebase_context(max_chars=6000)
            if codebase_ctx:
                system_prompts.append(codebase_ctx)

        resp = await self.ask_with_files(
            "director",
            system_prompts,
            text,
            attachments=attachments,
            allow_rotate=allow_rotate,
            **kwargs,
            **extra,
        )
        msg = resp.get("choices", [{}])[0].get("message", {})
        reply = msg.get("content") or ""
        reasoning = msg.get("reasoning_content")

        # XỬ LÝ GIAO VIỆC CHO KỸ SƯ NẾU CÓ
        reply, delegations = parse_delegations(reply)
        if delegations:
            reply, reasoning = await self.report_after_delegation(reply, delegations)

        shown = strip_role_codes(reply, self.role_codes)  # Chủ tịch không cần thấy mã gọi vai; nơi gọi tiếp vẫn nhận bản gốc để đọc mã
        self.remember("director", "assistant", shown, reasoning)
        self.council_messages.append({
            "speaker": "director", "name": persona["name"], "avatar": persona["avatar"],
            "role_title": persona["role_title"], "content": shown, "timestamp": time.strftime("%H:%M:%S"),
        })

        # TỰ ĐỘNG LÀM SẠCH VÀ VÁ MÃ NGUỒN VÀO ĐĨA (NATIVE AI API BEHAVIOR)
        ws = getattr(self, "build_workspace", os.path.join(self.project_dir, "build_workspace"))
        if not os.path.exists(ws):
            ws = os.path.join(self.project_dir, "merged_source")
        if os.path.exists(ws) and shown:
            try:
                h_res = workspace_harness.extract_and_apply_harness_edits(ws, shown)
                if h_res.get("applied_count", 0) > 0:
                    applied_files = [x.get("file") for x in h_res.get("applied", []) if x.get("file")]
                    files_str = ", ".join(applied_files[:4])
                    if len(applied_files) > 4:
                        files_str += f" và {len(applied_files) - 4} file khác"
                    self.add_log("harness", f"⚡ [Tự động vá đĩa] Đã áp dụng {h_res['applied_count']} thay đổi vào: {files_str}")
                    self.council_messages.append({
                        "speaker": "system",
                        "name": "Local Workspace Harness",
                        "avatar": "🌳",
                        "role_title": "Tự động cập nhật mã nguồn",
                        "content": f"⚡ **[Harness Auto-Apply]** Đã tự động bóc tách và áp dụng thành công **{h_res['applied_count']}** bản vá vào workspace trên đĩa:\n" + "\n".join(f"- 📄 `{f}`" for f in applied_files),
                        "timestamp": time.strftime("%H:%M:%S"),
                    })
            except Exception as e:
                self.add_log("harness", f"⚠️ [Harness Auto-Apply] Lỗi: {e}")

        applied_cnt = h_res.get("applied_count", 0) if "h_res" in locals() and isinstance(h_res, dict) else 0
        self.note_director_turn(edited_files_count=applied_cnt)
        self._persist()
        return reply, self.new_director_zips()

    def ship_worker_kwargs(self, index: int) -> tuple:
        role = f"subagent-{index % 5 + 1}"
        kwargs = role_llm_kwargs(role)
        if kwargs.get("backend") != "chatgpt":  # zip chỉ đi được với GPT
            kwargs = {"backend": "chatgpt", "model": LLM_BACKEND_DEFAULT_MODEL["chatgpt"], "reasoning_effort": "none"}
        return role, kwargs

    async def ship_workers(self, labels: List[str], zips: List[str]) -> Dict[str, Any]:
        """Mở mỗi worker một phiên mới, gửi hai zip (không mở zip), nhắc làm đúng phần của nhãn, chờ alpha. Lệnh mo_worker gọi hàm này."""
        pair = classify_ship_zips(zips)
        if not pair:
            inbox_zips = [os.path.join(self.director_inbox(), f) for f in os.listdir(self.director_inbox()) if f.lower().endswith(".zip")]
            pair = classify_ship_zips(inbox_zips)
        if not pair:
            raise ValueError("Chưa có đủ hai zip (mã nguồn gốc và bản bàn giao) của Giám đốc.")
        batch = f"b{int(time.time())}"
        folder = os.path.join(self.project_dir, "shipments", batch)
        os.makedirs(folder, exist_ok=True)
        source = os.path.join(folder, "source_goc.zip")
        handover = os.path.join(folder, "ban_giao.zip")
        shutil.copy2(pair[0], source)
        shutil.copy2(pair[1], handover)
        contract_added = ensure_handover_contracts(handover, labels, self.director_inbox())
        if contract_added:
            self.add_log("courier", f"🧾 [Người ship] Đã tự động bù hợp đồng còn thiếu từ các phiên trước cho worker: {', '.join(contract_added)}")
        added = ensure_context_files(source)  # zip của Giám đốc thiếu luật hoặc hướng dẫn thì factory bổ sung bản khung
        if added:
            self.add_log("courier", f"🧾 [Người ship] Bổ sung vào zip gửi worker: {', '.join(os.path.basename(x) for x in added)}")
        attachments = [{"path": source, "name": "source_goc.zip"}, {"path": handover, "name": "ban_giao.zip"}]
        self.ship_state = {"status": "shipping", "batch": batch, "workers": labels}
        self.add_log("courier", f"🚚 [Người ship] Mở {len(labels)} phiên worker ({', '.join(labels)})...")

        async def run_worker(index: int, label: str) -> tuple:
            role, kwargs = self.ship_worker_kwargs(index)
            state = self.subagent_states.setdefault(role, {"id": role, "status": "idle", "current_task": "", "files_count": 0})
            state.update({"status": "working", "current_task": f"Ship: worker {label}"})
            token = new_ack_token()
            session_id = f"{self.project_id}-ship-{batch}-{label}"
            download_dir = os.path.join(folder, f"download_{label}")
            alphas: List[str] = []
            try:
                for attempt in range(WORKER_MAX_REASK + 1):
                    if attempt == 0:
                        messages, sent = [{"role": "user", "content": SHIP_WORKER_PROMPT.format(label=label, token=token)}], attachments
                    else:
                        # Ép trả lời: worker chưa gửi file thì nhắc trong cùng phiên, kèm lại chuỗi xác nhận
                        self.add_log("courier", f"🔁 [Người ship] worker {label} chưa trả alpha, nhắc lần {attempt}")
                        messages, sent = [{"role": "user", "content": SHIP_WORKER_REASK.format(label=label, token=token)}], None
                    resp = await call_bridge_llm(
                        messages,
                        session_id=session_id,
                        stateless=False,
                        expect_zip=True,
                        download_dir=download_dir,
                        timeout=SHIP_WORKER_TIMEOUT,
                        attachments=sent,
                        **kwargs,
                    )
                    alphas += [p for p in (resp.get("downloaded_files") or []) if p.lower().endswith(".zip") and os.path.exists(p) and p not in alphas]
                    if alphas:
                        break
            except Exception as err:
                return label, None, str(err)
            finally:
                state.update({"status": "idle", "current_task": ""})
                # Phiên worker chỉ dùng một đợt: xong thì đóng tab và xóa cuộc trò chuyện của nó (alpha đã tải về máy)
                cleaned = await close_bridge_session(session_id, delete_conversation=True)
                if cleaned.get("error") or (cleaned.get("deleted") or {}).get("ok") is False:
                    self.add_log("courier", f"🧹 [Người ship] worker {label}: đã đóng phiên nhưng chưa xóa được cuộc trò chuyện ({str(cleaned.get('error') or cleaned.get('deleted'))[:120]})")
            if not alphas:
                return label, None, "worker không trả zip alpha"
            dest = os.path.join(folder, f"alpha_{label}.zip")
            shutil.copy2(max(alphas, key=os.path.getmtime), dest)
            return label, dest, ""

        results = await asyncio.gather(*(run_worker(i, label) for i, label in enumerate(labels)))
        alphas = {label: path for label, path, _ in results if path}
        failed = {label: reason for label, path, reason in results if not path}
        bundle = None
        if alphas:
            bundle = os.path.join(folder, "alpha_bundle.zip")
            with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
                for label, path in alphas.items():
                    zf.write(path, f"alpha_{label}.zip")
        for label, reason in failed.items():
            self.add_log("courier", f"⚠️ [Người ship] worker {label} chưa trả alpha: {reason}")
        self.add_log("courier", f"📨 [Người ship] Alpha về: {', '.join(alphas) or 'không có'}; thiếu: {', '.join(failed) or 'không'}.")
        self.ship_state = {"status": "idle", "batch": batch, "workers": labels}
        overlaps = find_overlaps(alphas)
        if overlaps:
            self.add_log("courier", "⚠️ [Người ship] Có file do nhiều worker cùng sửa (hợp đồng bị lệch): "
                         + "; ".join(f"{p} ({', '.join(ls)})" for p, ls in list(overlaps.items())[:8]))
        self._last_shipment = {"batch": batch, "alphas": alphas, "failed": failed, "bundle": bundle, "overlaps": overlaps}
        return self._last_shipment

    def get_or_recover_shipment(self) -> Optional[Dict[str, Any]]:
        """Lấy shipment gần nhất hoặc tự động phục hồi từ thư mục shipments trên đĩa nếu server vừa khởi động lại."""
        if self._last_shipment and self._last_shipment.get("bundle") and os.path.exists(self._last_shipment["bundle"]):
            return self._last_shipment
        ship_root = os.path.join(self.project_dir, "shipments")
        if os.path.isdir(ship_root):
            batches = sorted(
                [os.path.join(ship_root, b) for b in os.listdir(ship_root) if os.path.isdir(os.path.join(ship_root, b))],
                key=lambda p: os.path.getmtime(p),
                reverse=True
            )
            for b_dir in batches:
                bundle = os.path.join(b_dir, "alpha_bundle.zip")
                if os.path.isfile(bundle) and os.path.getsize(bundle) > 0:
                    alphas = {}
                    for f in os.listdir(b_dir):
                        if f.startswith("alpha_") and f.endswith(".zip") and f != "alpha_bundle.zip":
                            lbl = f.replace("alpha_", "").replace(".zip", "")
                            alphas[lbl] = os.path.join(b_dir, f)
                    recovered = {
                        "batch": os.path.basename(b_dir),
                        "alphas": alphas,
                        "failed": {},
                        "bundle": bundle,
                        "overlaps": find_overlaps(alphas)
                    }
                    self._last_shipment = recovered
                    self.add_log("courier", f"📦 [Người ship] Tự động phục hồi gói alpha từ {os.path.basename(b_dir)} ({', '.join(alphas.keys())})")
                    return recovered
        return None

    def latest_source_zip(self) -> Optional[str]:
        """Zip mã nguồn mới nhất Giám đốc gửi ra (phân biệt theo tên, không mở zip để đọc)."""
        zips = self._pending_zips or self._last_director_zips
        pair = classify_ship_zips(zips) if len(zips) >= 2 else None
        if pair:
            return pair[0]
        inbox = self.director_inbox()
        if os.path.isdir(inbox):
            inbox_zips = [os.path.join(inbox, f) for f in os.listdir(inbox) if f.lower().endswith(".zip")]
            pair = classify_ship_zips(inbox_zips)
            if pair:
                return pair[0]
        return zips[0] if len(zips) == 1 else None

    def latest_contract_zip(self) -> Optional[str]:
        """Zip hợp đồng mới nhất Giám đốc gửi ra."""
        zips = self._pending_zips or self._last_director_zips
        pair = classify_ship_zips(zips) if len(zips) >= 2 else None
        if pair:
            return pair[1]
        inbox = self.director_inbox()
        if os.path.isdir(inbox):
            inbox_zips = [os.path.join(inbox, f) for f in os.listdir(inbox) if f.lower().endswith(".zip")]
            pair = classify_ship_zips(inbox_zips)
            if pair:
                return pair[1]
        return None

    def read_living_docs(self) -> str:
        """Trợ lý roadmap xem TAI_LIEU_SONG trong zip mới nhất: roadmap, checklist và số mục chưa xong."""
        candidates = [z for z in (self._pending_zips + self._last_director_zips) if os.path.exists(z)]
        for zip_path in candidates:
            try:
                with zipfile.ZipFile(zip_path) as zf:
                    docs = [n for n in zf.namelist() if "tai_lieu_song/" in n.lower() and not n.endswith("/") and n.lower().endswith((".md", ".txt", ".json"))]
                    if not docs:
                        continue
                    parts, open_items = [], 0
                    for name in sorted(docs):
                        text = zf.read(name).decode("utf-8", errors="replace")
                        open_items += len(re.findall(r"^\s*[-*]\s*\[ \]", text, re.MULTILINE))
                        parts.append(f"--- {name} ---\n{text[:6000]}")
                    return f"Zip: {os.path.basename(zip_path)}. Mục chưa xong trong checklist: {open_items}.\n\n" + "\n\n".join(parts)
            except zipfile.BadZipFile:
                continue
        return "Chưa thấy thư mục TAI_LIEU_SONG trong zip nào của Giám đốc."

    @staticmethod
    def unsafe_build_command(cmd: str) -> bool:
        """Lệnh build do GPT viết chạy thẳng trên máy Mac của Chủ tịch (chủ đích). Chỉ chặn những thứ rollback không cứu được: sudo, xóa gốc/thư mục hệ thống/home/thư mục cá nhân,
        tải script rồi chạy thẳng qua pipe, ghi thẳng lên ổ đĩa, đọc khóa và mật khẩu. Ngoài ra lệnh được phép."""
        text = cmd or ""
        if _UNSAFE_BUILD_RE.search(text):
            return True
        for segment in re.split(r"\s*(?:&&|\|\||;|\||\n)\s*", text):
            if not segment.strip():
                continue
            try:
                tokens = shlex.split(segment)
            except ValueError:
                tokens = segment.split()
            if _rm_is_dangerous(tokens):
                return True
        return False

    @staticmethod
    def resolve_build_target(raw: Any) -> str:
        """Thư mục bản đang chạy khai trong BUILD.json: phải là đường dẫn đầy đủ và không phải chỗ nguy hiểm (gốc, hệ thống, home, thư mục cá nhân)."""
        text = str(raw or "").strip()
        if not text:
            return ""
        path = os.path.normpath(os.path.expandvars(os.path.expanduser(text)))
        if not os.path.isabs(path) or dangerous_path(path):
            raise ValueError(f"target không hợp lệ hoặc quá nguy hiểm: {text}")
        return path

    def scan_build_scripts(self, zip_path: str, commands: List[str]) -> None:
        """Các script .sh mà BUILD.json gọi (bash scripts/backup.sh...) cũng chạy trên máy: kiểm từng dòng bằng cùng bộ lọc."""
        wanted = set()
        for cmd in commands:
            for match in re.finditer(r"(?:\b(?:bash|sh|zsh|source)\s+|\./)([\w./\-]+\.sh)\b", cmd or ""):
                wanted.add(match.group(1).lstrip("./").lower())
        if not wanted:
            return
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                lowered = name.lower()
                if not any(lowered == w or lowered.endswith("/" + w) for w in wanted):
                    continue
                for line in zf.read(name).decode("utf-8", errors="replace").splitlines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith(("#", "echo ", "printf ")):
                        continue
                    if self.unsafe_build_command(stripped):
                        raise ValueError(f"script {name} có lệnh bị chặn vì nguy hiểm: {stripped[:120]}")

    def _record_build_problem(self, problems: List[str]) -> str:
        """BUILD.json hoặc script có vấn đề nên chưa chạy được gì: ghi như một vòng build lỗi để gui_log_giam_doc gửi cho Giám đốc sửa."""
        self._build_iter += 1
        text = "KHÔNG CHẠY ĐƯỢC BUILD vì BUILD.json hoặc script có vấn đề:\n- " + "\n- ".join(problems)
        log_file = os.path.join(self.project_dir, f"build_run_iter_{self._build_iter}.log")
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(text)
        self._last_build = {
            "iteration": self._build_iter, "passed": False, "exit_code": 2, "failed_step": "KIỂM TRA BUILD.json",
            "rolled_back": False, "rollback_ok": None, "log": text, "log_file": log_file,
        }
        self.add_log("builder", f"📋 [Worker Thực thi] Vòng #{self._build_iter}: ❌ FAIL, chưa chạy được: {problems[0][:140]}")
        return f"FAIL (chưa chạy được), log:\n{text}\nHãy dùng gui_log_giam_doc để Giám đốc sửa BUILD.json."

    async def build_latest_source(self) -> str:
        """Người ship build app trên máy Mac: giải nén zip mã nguồn mới nhất rồi chạy BUILD.json theo thứ tự backup, install, test, deploy;
        lỗi thì tự rollback. Lệnh lấy từ TAI_LIEU_SONG/BUILD.json trong zip hoặc cấu hình dự án. Lưu log."""
        zip_path = self.latest_source_zip()
        if not zip_path:
            raise ValueError("chưa có zip mã nguồn của Giám đốc để build.")
        install_cmd, test_cmd = self.install_command, self.test_command
        backup_cmd = deploy_cmd = rollback_cmd = target_raw = ""
        artifact_patterns: List[str] = []
        try:
            with zipfile.ZipFile(zip_path) as zf:
                for name in zf.namelist():
                    if name.lower().endswith("tai_lieu_song/build.json"):
                        data = json.loads(zf.read(name).decode("utf-8"))
                        install_cmd, test_cmd = data.get("install") or install_cmd, data.get("test") or test_cmd
                        backup_cmd, deploy_cmd = str(data.get("backup") or ""), str(data.get("deploy") or "")
                        rollback_cmd, target_raw = str(data.get("rollback") or ""), data.get("target")
                        raw_artifacts = data.get("artifacts") or []
                        artifact_patterns = [str(x) for x in (raw_artifacts if isinstance(raw_artifacts, list) else [raw_artifacts])]
        except (zipfile.BadZipFile, ValueError):
            pass
        problems: List[str] = []  # lỗi khai báo của Giám đốc: ghi thành một vòng build lỗi để Người ship gửi log về cho Giám đốc sửa
        warnings: List[str] = []
        commands = [backup_cmd, install_cmd, test_cmd, deploy_cmd, rollback_cmd]
        for cmd in commands:
            if self.unsafe_build_command(cmd):
                problems.append(f"lệnh build bị chặn vì nguy hiểm: {cmd}")
        try:
            target = self.resolve_build_target(target_raw)
        except ValueError as err:
            target = ""
            problems.append(str(err))
        if not problems and not target and (backup_cmd or deploy_cmd or rollback_cmd):
            warnings.append("BUILD.json có backup, deploy hoặc rollback nhưng không có target (thư mục bản đang chạy), nên các bước đó bị bỏ qua. "
                            "Chỉ khai backup, deploy, rollback khi có target; chưa có thư mục chạy thật thì BUILD.json chỉ cần install và test.")
            backup_cmd = deploy_cmd = rollback_cmd = ""
            commands = [backup_cmd, install_cmd, test_cmd, deploy_cmd, rollback_cmd]
        if not problems:
            try:
                self.scan_build_scripts(zip_path, commands)
            except ValueError as err:
                problems.append(str(err))
        if not problems and not (install_cmd or test_cmd or deploy_cmd):
            problems.append("chưa có lệnh build: Giám đốc cần ghi BUILD.json trong TAI_LIEU_SONG.")
        if problems:
            return self._record_build_problem(problems)
        self._build_iter += 1
        result = await BuilderRunnerWorker(self.project_dir, self.add_log).execute_and_report(
            zip_path, install_cmd, test_cmd, self._build_iter,
            backup_cmd=backup_cmd, deploy_cmd=deploy_cmd, rollback_cmd=rollback_cmd, target=target, artifacts=artifact_patterns,
        )
        if warnings:
            result["log"] = "\n".join(f"⚠️ {w}" for w in warnings) + "\n\n" + result["log"]
            with open(result["log_file"], "w", encoding="utf-8") as f:
                f.write(result["log"])
        self._last_build = result
        head = f"{'PASS' if result['passed'] else 'FAIL'} (exit {result['exit_code']})"
        if result["rolled_back"]:
            head += f"; ROLLBACK {'OK' if result['rollback_ok'] else 'FAILED'}"
        if result.get("artifact_zip"):
            head += f"; sản phẩm build đã gom thành {os.path.basename(result['artifact_zip'])} ({len(result['artifacts'])} file) để Chủ tịch tải ở mục 'Zip của Giám đốc'"
        if result["rolled_back"] and not result["rollback_ok"]:
            label, avatar = CONTROLLER_ROLES["shipper"]
            self.council_messages.append({
                "speaker": "shipper", "name": label, "avatar": avatar, "role_title": label, "timestamp": time.strftime("%H:%M:%S"),
                "content": f"🚨 ROLLBACK FAILED ở vòng build #{result['iteration']} (bước {result['failed_step']}). Bản đang chạy có thể đang hỏng: anh kiểm tra thư mục {target} và {os.path.join(self.project_dir, 'rollback_store')}.",
            })
            head += "\n🚨 ROLLBACK FAILED: hãy dùng bao_chu_tich báo Chủ tịch ngay và không build lại."
        return f"{head}, log cuối:\n{result['log'][-3000:]}"

    async def run_controller_commands(self, role_id: str, commands: List[Dict[str, Any]]) -> tuple:
        """Chạy các lệnh agent điều khiển viết ra. Trả về (kết quả gửi lại cho agent, có kết thúc chưa)."""
        lines: List[str] = []
        finished = False
        allowed = {
            "shipper": {"xem_tin_giam_doc", "mo_worker", "mang_ve_giam_doc", "build_app", "gui_log_giam_doc", "bao_chu_tich", "xem_cay", "doc_code", "tim_code", "va_code", "tao_file"},
            "roadmap": {"doc_tai_lieu", "nhac_giam_doc", "bao_chu_tich", "xem_cay", "doc_code", "tim_code"},
        }[role_id]
        for cmd in commands:
            name = str(cmd.get("lenh"))
            if name not in allowed:
                lines.append(f"{name}: lệnh không có hoặc không thuộc vai của bạn.")
                continue
            try:
                if name == "xem_cay":
                    ws = getattr(self, "build_workspace", os.path.join(self.project_dir, "build_workspace"))
                    if not os.path.exists(ws):
                        ws = os.path.join(self.project_dir, "merged_source")
                    tree_str, _ = workspace_harness.get_workspace_tree(ws)
                    lines.append(f"xem_cay:\n{tree_str}")
                elif name == "doc_code":
                    ws = getattr(self, "build_workspace", os.path.join(self.project_dir, "build_workspace"))
                    if not os.path.exists(ws):
                        ws = os.path.join(self.project_dir, "merged_source")
                    path = str(cmd.get("path") or "").strip()
                    start = int(cmd.get("dong") or cmd.get("start") or 1)
                    count = int(cmd.get("so_dong") or cmd.get("count") or 100)
                    res = workspace_harness.read_file_slice(ws, path, start_line=start, line_count=count)
                    if "error" in res:
                        lines.append(f"doc_code: LỖI - {res['error']}")
                    else:
                        lines.append(f"doc_code {path} (dòng {res['start_line']} - {res['end_line']} / tổng {res['total_lines']} dòng):\n{res['content']}")
                elif name == "tim_code":
                    ws = getattr(self, "build_workspace", os.path.join(self.project_dir, "build_workspace"))
                    if not os.path.exists(ws):
                        ws = os.path.join(self.project_dir, "merged_source")
                    query = str(cmd.get("tu_khoa") or cmd.get("query") or "").strip()
                    matches = workspace_harness.search_code(ws, query)
                    if not matches:
                        lines.append(f"tim_code: Không tìm thấy kết quả nào cho '{query}'.")
                    else:
                        match_lines = [f"{m['file']}:{m['line']}: {m['text']}" for m in matches[:15]]
                        lines.append(f"tim_code: Tìm thấy {len(matches)} kết quả cho '{query}':\n" + "\n".join(match_lines))
                elif name == "va_code":
                    ws = getattr(self, "build_workspace", os.path.join(self.project_dir, "build_workspace"))
                    if not os.path.exists(ws):
                        ws = os.path.join(self.project_dir, "merged_source")
                    path = str(cmd.get("path") or "").strip()
                    tim = str(cmd.get("tim") or cmd.get("search") or "")
                    thay = str(cmd.get("thay") or cmd.get("replace") or "")
                    diff_text = str(cmd.get("diff") or cmd.get("noi_dung") or "")
                    if path and tim:
                        res = workspace_harness.apply_search_replace(ws, path, tim, thay)
                        if res.get("success"):
                            lines.append(f"va_code: Thành công vá file {path} (chiến lược: {res.get('strategy')}).")
                            self.add_log("harness", f"🛠️ [Harness] Đã vá file {path} trực tiếp trên đĩa.")
                        else:
                            lines.append(f"va_code: THẤT BẠI - {res.get('error')}")
                    elif diff_text:
                        res = workspace_harness.extract_and_apply_harness_edits(ws, diff_text)
                        lines.append(f"va_code: Đã áp dụng {res['applied_count']} thao tác; lỗi: {res['error_count']}.")
                        self.add_log("harness", f"🛠️ [Harness] Đã áp dụng {res['applied_count']} thay đổi vào mã nguồn.")
                    else:
                        raise ValueError("thiếu path và tim/thay hoặc diff.")
                elif name == "tao_file":
                    ws = getattr(self, "build_workspace", os.path.join(self.project_dir, "build_workspace"))
                    if not os.path.exists(ws):
                        ws = os.path.join(self.project_dir, "merged_source")
                    path = str(cmd.get("path") or "").strip()
                    content = str(cmd.get("noi_dung") or cmd.get("content") or "")
                    res = workspace_harness.write_file(ws, path, content)
                    lines.append(f"tao_file: Thành công tạo/ghi đè {path} ({res['lines']} dòng).")
                    self.add_log("harness", f"📝 [Harness] Đã tạo file {path} ({res['lines']} dòng).")
                elif name == "xem_tin_giam_doc":
                    names = [os.path.basename(z) for z in self._pending_zips]
                    director_msg = self._last_director_text or next(
                        (m.get("content", "") for m in reversed(self.council_messages) if m.get("speaker") == "director"), "(chưa có)")
                    shipment = self.get_or_recover_shipment()
                    bundle_info = f"\nCó sẵn alpha_bundle.zip từ các worker: {', '.join(shipment.get('alphas', {}).keys())}." if shipment and shipment.get("bundle") else ""
                    lines.append(f"xem_tin_giam_doc: zip đã tải về: {', '.join(names) or 'chưa có'}.{bundle_info}\nTin của Giám đốc:\n{director_msg}")
                elif name == "mo_worker":
                    labels = [str(x).strip().upper()[:1] for x in (cmd.get("nhan") or []) if str(x).strip()]
                    labels = list(dict.fromkeys(labels))[:SHIP_MAX_WORKERS]
                    if len(labels) < 1:
                        raise ValueError("thiếu danh sách nhãn worker.")
                    result = await self.ship_workers(labels, self._pending_zips)
                    lines.append(f"mo_worker: alpha về từ {', '.join(result['alphas']) or 'không worker nào'}; thiếu: {', '.join(result['failed']) or 'không'}."
                                 + (f" CẢNH BÁO: {len(result['overlaps'])} file do nhiều worker cùng sửa, sẽ báo Giám đốc khi mang về." if result.get("overlaps") else ""))
                elif name == "mang_ve_giam_doc":
                    shipment = self.get_or_recover_shipment()
                    if not shipment or not shipment.get("bundle"):
                        raise ValueError("chưa có alpha nào để mang về.")
                    note = (
                        f"Người ship mang alpha về từ các phiên worker: {', '.join(shipment['alphas'])}. "
                        + (f"Chưa có alpha từ: {', '.join(shipment['failed'])}. " if shipment["failed"] else "")
                        + (("CẢNH BÁO hợp đồng bị lệch, các file này nằm trong alpha của nhiều worker: "
                            + "; ".join(f"{p} (worker {', '.join(ls)})" for p, ls in list(shipment["overlaps"].items())[:12])
                            + ". Hãy đối chiếu kỹ với hợp đồng khi merge. ") if shipment.get("overlaps") else "")
                        + "Gói alpha_bundle.zip đính kèm (mỗi alpha là một zip bên trong). Hãy merge, kiểm tra rồi báo cáo; "
                        "nếu còn việc song song thì gửi hai zip mới và nói chia làm mấy phiên."
                    )
                    attachments = [{"path": shipment["bundle"], "name": "alpha_bundle.zip"}]
                    latest_src = self.latest_source_zip()
                    if latest_src and os.path.exists(latest_src) and latest_src not in [a["path"] for a in attachments]:
                        attachments.append({"path": latest_src, "name": os.path.basename(latest_src)})
                    latest_contract = self.latest_contract_zip()
                    if latest_contract and os.path.exists(latest_contract) and latest_contract not in [a["path"] for a in attachments]:
                        attachments.append({"path": latest_contract, "name": os.path.basename(latest_contract)})
                    reply, zips = await self.director_turn(note, attachments)
                    self._pending_zips = zips
                    self._last_shipment = None
                    lines.append(f"mang_ve_giam_doc: Giám đốc trả lời:\n{reply[:2000]}\nZip mới Giám đốc gửi ra: {', '.join(os.path.basename(z) for z in zips) or 'không'}.")
                    self._last_shipment = None
                    lines.append(f"mang_ve_giam_doc: Giám đốc trả lời:\n{reply[:2000]}\nZip mới Giám đốc gửi ra: {', '.join(os.path.basename(z) for z in zips) or 'không'}.")
                elif name == "doc_tai_lieu":
                    lines.append("doc_tai_lieu:\n" + self.read_living_docs())
                elif name == "build_app":
                    lines.append("build_app: " + await self.build_latest_source())
                elif name == "gui_log_giam_doc":
                    if not self._last_build:
                        raise ValueError("chưa build lần nào.")
                    build = self._last_build
                    log_zip = os.path.join(self.project_dir, "shipments", f"build_log_{build['iteration']}.zip")
                    os.makedirs(os.path.dirname(log_zip), exist_ok=True)
                    with zipfile.ZipFile(log_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                        zf.write(build["log_file"], os.path.basename(build["log_file"]))
                    note = (
                        f"Người ship build app (vòng #{build['iteration']}): {'PASS' if build['passed'] else 'FAIL'} (exit {build['exit_code']}). "
                        f"Phần cuối của log:\n{build['log'][-5000:]}\n\n"
                        + ("Hãy xác nhận và cập nhật tài liệu sống." if build["passed"] else "Hãy sửa lỗi, cập nhật tài liệu sống và gửi zip mã nguồn mới để build lại.")
                    )
                    reply, zips = await self.director_turn(note, [{"path": log_zip, "name": os.path.basename(log_zip)}])
                    self._pending_zips = zips
                    lines.append(f"gui_log_giam_doc: Giám đốc trả lời:\n{reply[:2000]}\nZip mới Giám đốc gửi ra: {', '.join(os.path.basename(z) for z in zips) or 'không'}.")
                elif name == "nhac_giam_doc":
                    self._nudges += 1
                    if self._nudges > SHIP_MAX_ROUNDS:
                        raise ValueError(f"đã nhắc Giám đốc quá {SHIP_MAX_ROUNDS} lần trong một vòng; hãy bao_chu_tich để Chủ tịch quyết.")
                    message = str(cmd.get("noi_dung", ""))
                    label, avatar = CONTROLLER_ROLES[role_id]
                    self.council_messages.append({  # Chủ tịch thấy tin app chuyển sang cho Giám đốc
                        "speaker": role_id, "name": label, "avatar": avatar, "role_title": f"{label} → Giám đốc",
                        "content": f"→ Giám đốc: {message}", "timestamp": time.strftime("%H:%M:%S"),
                    })
                    reply, zips = await self.director_turn(f"Trợ lý roadmap: {message}")
                    reply = strip_role_codes(reply, self.role_codes)
                    self._last_director_text, self._pending_zips = reply, zips
                    if zips:  # Giám đốc chia việc song song: Người ship tự lo vận chuyển
                        await self.run_controller("shipper", self.shipper_trigger(reply, zips))
                    lines.append(f"nhac_giam_doc: phản hồi cuối của Giám đốc:\n{(self._last_director_text or '')[:2000]}")
                elif name == "bao_chu_tich":
                    label, avatar = CONTROLLER_ROLES[role_id]
                    self.council_messages.append({
                        "speaker": role_id, "name": label, "avatar": avatar, "role_title": label,
                        "content": str(cmd.get("noi_dung", "")), "timestamp": time.strftime("%H:%M:%S"),
                    })
                    lines.append("bao_chu_tich: đã báo Chủ tịch.")
                    finished = True
            except Exception as err:
                lines.append(f"{name}: lỗi: {err}")
        return "\n\n".join(lines), finished

    def shipper_trigger(self, director_text: str, zips: List[str]) -> str:
        shipment = self.get_or_recover_shipment()
        shipment_note = ""
        if shipment and shipment.get("bundle") and os.path.exists(shipment["bundle"]):
            alphas_str = ", ".join(shipment.get("alphas", {}).keys())
            shipment_note = (
                f"\n📦 CÔNG XƯỞNG ĐANG CÓ GÓI ALPHA BUNDLE (từ các worker: {alphas_str}) "
                f"đã đóng gói sẵn tại {os.path.basename(shipment['bundle'])}. "
                "Nếu Giám đốc đang chờ alpha hoặc cần merge alpha để ra bản source tiếp theo, hãy phát lệnh `mang_ve_giam_doc` ngay để bàn giao cho Giám đốc!\n"
            )
        return (
            f"⚡ TÍN HIỆU CÔNG XƯỞNG: Giám đốc có {len(zips)} zip liên quan. Công xưởng đã mở xem giúp bạn:\n"
            + "\n".join(f"- {describe_zip(z)}" for z in zips)
            + f"{shipment_note}"
            + f"\nTin mới nhất của Giám đốc:\n{director_text}\n\nHãy xử lý theo vai của bạn (dùng khối lenh: mang_ve_giam_doc, mo_worker, build_app, ...)."
        )

    async def ask_controller(self, role_id: str, system: str, text: str) -> tuple:
        """Gửi một tin cho agent điều khiển kèm chuỗi xác nhận. Câu trả lời chưa có chuỗi đó thì nhắc lại (ép trả lời) trước khi dùng.
        Trả về (phản hồi thô, nội dung trả lời, mã đã dùng)."""
        token = new_ack_token()
        line = ack_text(ACK_REPLY, token)
        hint = ""
        if role_id == "roadmap":
            hint = f" Để nhắn Giám đốc, có thể viết một dòng `{self.role_codes['director']}` rồi nội dung tin ở các dòng sau; factory sẽ chuyển tin đó vào thread Giám đốc."
        prompt = f"{text}\n\n[Dòng đầu tiên của câu trả lời phải là đúng `{line}`; sau đó mới tới khối lenh hoặc câu trả lời của bạn.{hint}]"
        resp: Dict[str, Any] = {}
        reply = ""
        for attempt in range(CONTROLLER_MAX_REASK + 1):
            resp = await self.ask_role(role_id, [system], prompt, **role_llm_kwargs(role_id))
            reply = resp.get("choices", [{}])[0].get("message", {}).get("content") or ""
            if has_ack(reply, ACK_REPLY, token):
                break
            if attempt < CONTROLLER_MAX_REASK:
                self.add_log(role_id, f"🔁 Chưa thấy {line} ở câu trả lời, nhắc lại")
                prompt = f"Chưa thấy dòng `{line}` ở đầu câu trả lời trước. Hãy trả lời lại: dòng đầu là đúng `{line}`, sau đó tới khối lenh hoặc câu trả lời của bạn."
            else:
                self.add_log(role_id, f"⚠️ {CONTROLLER_ROLES[role_id][0]} không gửi chuỗi xác nhận {line}; vẫn dùng câu trả lời cuối.")
        return resp, reply, token

    async def run_controller(self, role_id: str, trigger: str) -> None:
        """Agent điều khiển (Người ship hoặc Trợ lý roadmap): đọc tin, viết lệnh, factory chạy lệnh rồi trả kết quả, lặp đến khi xong."""
        system = SHIPPER_PROMPT if role_id == "shipper" else ROADMAP_PROMPT
        text = trigger
        for _ in range(CONTROLLER_MAX_STEPS):
            resp, reply, token = await self.ask_controller(role_id, system, text)
            msg = resp.get("choices", [{}])[0].get("message", {})
            self.remember(role_id, "assistant", reply, msg.get("reasoning_content"))
            commands = parse_controller_commands(reply)
            if role_id == "roadmap" and not any(c.get("lenh") == "nhac_giam_doc" for c in commands):
                for called_role, called_text in find_role_calls(reply, self.role_codes):
                    if called_role == "director" and called_text:  # gọi Giám đốc bằng mã: app chuyển tin sang thread Giám đốc
                        commands.append({"lenh": "nhac_giam_doc", "noi_dung": called_text})
            if not commands:
                self.add_log(role_id, f"{CONTROLLER_ROLES[role_id][1]} {strip_ack(reply, ACK_REPLY, token)[:160]}")
                return
            self.add_log(role_id, f"{CONTROLLER_ROLES[role_id][1]} Lệnh: {', '.join(str(c.get('lenh')) for c in commands)}")
            results, finished = await self.run_controller_commands(role_id, commands)
            if finished:
                return
            text = f"Kết quả lệnh:\n{results}"
        self.add_log(role_id, f"⚠️ {CONTROLLER_ROLES[role_id][0]} dừng vì quá {CONTROLLER_MAX_STEPS} bước.")

    async def run_cycle(self, steps: List[tuple]) -> None:
        """Chạy lần lượt các agent điều khiển (Người ship rồi Trợ lý roadmap) trong một vòng, mỗi lần chỉ một vòng ngoài cùng.
        Mỗi bước là (vai, trigger); trigger là chuỗi hoặc hàm trả chuỗi, tính vào lúc tới lượt."""
        if self._shipping:
            return
        self._shipping = True
        self._nudges = 0
        try:
            for role_id, trigger in steps:
                await self.run_controller(role_id, trigger() if callable(trigger) else trigger)
        except Exception as err:
            self.add_log("courier", f"⚠️ [Điều hành] dừng vì lỗi: {err}")
        finally:
            self._shipping = False
            self.ship_state = {"status": "idle", "batch": self.ship_state.get("batch"), "workers": []}
            self._persist()

    async def run_agents(self, role_id: str, trigger: str) -> None:
        await self.run_cycle([(role_id, trigger)])

    def roadmap_trigger(self, reason: str = "") -> str:
        return (
            f"⚡ TÍN HIỆU: {reason or 'Giám đốc báo đã hoàn thành'}. Tin cuối của Giám đốc:\n{(self._last_director_text or '(chưa có)')[:2000]}\n\n"
            "Hãy kiểm tra tài liệu sống bằng doc_tai_lieu; còn thiếu thì nhắn Giám đốc, đã xong thì bao_chu_tich."
        )

    def set_autopilot(self, on: bool) -> None:
        self.autopilot = bool(on)

    async def run_autopilot(self) -> None:
        """Chủ tịch bật Trợ lý roadmap: từ giờ mỗi lần Giám đốc báo xong thì trợ lý kiểm tra; lần này kiểm tra ngay."""
        self.autopilot = True
        await self.run_cycle([("roadmap", lambda: self.roadmap_trigger("Chủ tịch vừa bật Trợ lý roadmap"))])

    def trigger_roadmap(self, reason: str, force: bool = False) -> bool:
        """Gọi Trợ lý roadmap: chạy khi chế độ trợ lý đang bật, hoặc khi Chủ tịch tự gọi (force)."""
        if self._shipping:
            return False
        if not (self.autopilot or force):
            self.add_log("roadmap", "Có tín hiệu gọi Trợ lý roadmap nhưng chế độ trợ lý đang tắt, bỏ qua.")
            return False
        self._ship_task = asyncio.create_task(self.run_cycle([("roadmap", lambda: self.roadmap_trigger(reason))]))
        return True

    def maybe_start_ship(self, director_text: str, zips: Optional[List[str]] = None) -> None:
        """Điểm vào chung sau mỗi lượt của Giám đốc. Hai loại tín hiệu: zip mới tải về (download) và mã gọi vai trong tin.
        Có zip mới thì đánh thức Người ship; chế độ Trợ lý roadmap đang bật và Giám đốc báo xong (có zip mới hoặc gọi mã roadmap) thì
        Trợ lý roadmap kiểm tra sau khi Người ship làm xong.
        zips: các zip mới mà lượt gọi Giám đốc đã nhận sẵn (director_turn đã đánh dấu đã thấy); bỏ trống thì tự quét hộp thư."""
        calls = find_role_calls(director_text, self.role_codes)
        call_ship = any(role == "shipper" for role, _ in calls)
        call_roadmap = any(role == "roadmap" for role, _ in calls)
        zips = zips if zips is not None else self.new_director_zips()
        text = strip_role_codes(director_text, self.role_codes)
        if call_ship and not zips:
            inbox_zips = [os.path.join(self.director_inbox(), f) for f in os.listdir(self.director_inbox()) if f.lower().endswith(".zip")]
            if inbox_zips:
                zips = inbox_zips
        if call_ship and not zips and not self.get_or_recover_shipment():
            self.add_log("courier", "Giám đốc gọi Người ship nhưng factory chưa nhận được zip nào: kiểm tra link tải trong tin của Giám đốc.")
        steps: List[tuple] = []
        code_zips = [z for z in zips if zip_has_code(z) and not zip_is_artifact(z)]
        if not code_zips and self.get_or_recover_shipment():
            code_zips = [z for z in zips if z.lower().endswith(".zip")] or [self.latest_source_zip() or ""]
            code_zips = [z for z in code_zips if z and os.path.exists(z)]
        if zips and not code_zips and not self.get_or_recover_shipment():
            self.add_log("courier", "Giám đốc gửi zip chỉ có tài liệu hoặc là sản phẩm (artifact), không phải mã nguồn: không cần build, Người ship không chạy. Chủ tịch tải zip ở mục 'Zip của Giám đốc'.")
        if code_zips or (call_ship and self.get_or_recover_shipment()):
            steps.append(("shipper", lambda: self.shipper_trigger(text, code_zips)))
        if call_roadmap and not self.autopilot:
            self.add_log("roadmap", "Giám đốc gọi Trợ lý roadmap nhưng chế độ trợ lý đang tắt, bỏ qua.")
        elif self.autopilot and (zips or call_roadmap):
            steps.append(("roadmap", lambda: self.roadmap_trigger("Giám đốc báo đã xong" + (" và gửi zip mới" if zips else ""))))
        if not steps or self._shipping:
            return
        if code_zips:
            self._pending_zips = code_zips
        self._last_director_text = text
        self._ship_task = asyncio.create_task(self.run_cycle(steps))

    def handle_president_calls(self, user_idea: str) -> bool:
        """Chủ tịch tự gọi vai bằng mã gõ vào chat. Trả về True nếu tin này chỉ là lời gọi Người ship hoặc Trợ lý roadmap (không gửi tiếp cho Giám đốc)."""
        roles = {role for role, _ in find_role_calls(user_idea, self.role_codes)}
        if not roles & {"shipper", "roadmap"}:
            return False
        names = {"shipper": "Người ship", "roadmap": "Trợ lý roadmap"}
        called = [names[role] for role in ("shipper", "roadmap") if role in roles]
        self.council_messages.append({
            "speaker": "president", "name": "Chủ tịch (Anh)", "avatar": "👑", "role_title": "Chủ tịch Hội đồng",
            "content": strip_role_codes(user_idea, self.role_codes) or f"(gọi {', '.join(called)})", "timestamp": time.strftime("%H:%M:%S"),
        })
        self.add_log("council", f"👑 Chủ tịch gọi vai bằng mã: {', '.join(called)}")
        steps: List[tuple] = []
        if "shipper" in roles:
            self._director_seen.clear()  # zip nằm sẵn trong hộp thư coi như mới với lời gọi này
            zips = self.new_director_zips()
            if zips:
                self._pending_zips = zips
                steps.append(("shipper", lambda: self.shipper_trigger(self._last_director_text or "(chưa có tin của Giám đốc)", zips)))
            else:
                self.add_log("courier", "Chủ tịch gọi Người ship nhưng hộp thư của Giám đốc chưa có zip nào.")
        if "roadmap" in roles:
            steps.append(("roadmap", lambda: self.roadmap_trigger("Chủ tịch gọi Trợ lý roadmap")))
        if steps and not self._shipping:
            self._ship_task = asyncio.create_task(self.run_cycle(steps))
        elif steps:
            self.add_log("courier", "Người ship hoặc Trợ lý roadmap đang chạy, chưa nhận lời gọi mới.")
        return True

    def start_ship_from_inbox(self) -> bool:
        """Bấm tay: đưa các zip đang nằm trong hộp thư của Giám đốc cho Người ship (khi tự động chưa chạy, ví dụ sau khi khởi động lại)."""
        if self._shipping:
            return False
        self._director_seen.clear()
        director_text = self._last_director_text or next(
            (m.get("content", "") for m in reversed(self.council_messages) if m.get("speaker") == "director"), "")
        zips = self.new_director_zips()
        if not zips:
            inbox_zips = [os.path.join(self.director_inbox(), f) for f in os.listdir(self.director_inbox()) if f.lower().endswith(".zip")]
            if inbox_zips:
                zips = inbox_zips
        if not zips:
            return False
        self.maybe_start_ship(director_text, zips)
        return True

    def check_inbox_signal(self) -> bool:
        """Tín hiệu download mới: zip vừa nằm vào hộp thư của Giám đốc, đã ghi xong mà chưa ai xử lý thì đánh thức Người ship.
        Đường chính là lượt gọi Giám đốc tự báo zip; đường này bắt phần còn lại (zip về trễ, thả tay vào hộp thư, đường chính bị sót).
        Zip có sẵn từ trước khi bắt đầu theo dõi không phải tín hiệu mới. Trả về True nếu vừa đánh thức."""
        inbox = self.director_inbox()
        names = sorted(n for n in os.listdir(inbox) if n.lower().endswith(".zip"))
        if not self._inbox_primed:
            self._inbox_primed = True
            for name in names:
                full = os.path.join(inbox, name)
                self._director_seen.add((full, os.stat(full).st_mtime_ns))
            return False
        if self._shipping or self.active_speaker or self.council_thinking:
            return False  # đang có lượt Giám đốc hoặc Người ship chạy: đường chính sẽ lo
        fresh = []
        for name in names:
            full = os.path.join(inbox, name)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            if (full, stat.st_mtime_ns) in self._director_seen:
                continue
            if time.time() - stat.st_mtime < INBOX_SETTLE_SECONDS:
                return False  # còn đang ghi hoặc vừa về: chờ nhịp sau để đủ cả cặp zip
            fresh.append(full)
        if not fresh:
            return False
        director_text = self._last_director_text or next(
            (m.get("content", "") for m in reversed(self.council_messages) if m.get("speaker") == "director"), "")
        zips = self.new_director_zips()
        self.add_log("courier", f"📥 Tín hiệu download mới trong hộp thư Giám đốc: {', '.join(os.path.basename(z) for z in zips)}")
        self.maybe_start_ship(director_text, zips)
        return True

    async def send_zip_to_director(self, note: str = "", ship: bool = True) -> None:
        """Chủ tịch có sẵn ứng dụng cần phát triển thêm: đóng gói mã nguồn đã nạp (kèm tài liệu yêu cầu) và gửi vào thread Giám đốc, rồi chạy theo quy trình như thường."""
        if not self.merged_files:
            raise ValueError("Kho mã nguồn đang trống. Hãy nạp ZIP hoặc thư mục trước.")
        if role_llm_kwargs("director").get("backend") != "chatgpt":
            raise ValueError("Giám đốc cần dùng ChatGPT để nhận file zip.")
        original = os.path.join(self.project_dir, "original_source.zip")
        if os.path.isfile(original) and zipfile.is_zipfile(original):
            outbox = os.path.join(self.project_dir, "director_upload")  # bản sao để bổ sung luật và hướng dẫn mà không đụng vào zip gốc
            os.makedirs(outbox, exist_ok=True)
            zip_path = os.path.join(outbox, "project_source.zip")
            shutil.copy2(original, zip_path)
            ensure_context_files(zip_path)
            zip_name = "project_source.zip"
        else:
            zip_path = self.write_project_snapshot_zip()
            zip_name = "project_snapshot.zip"
        text = (
            "Chủ tịch gửi zip ứng dụng hiện có cần phát triển thêm; toàn bộ mã nguồn và yêu cầu nằm trong zip, bạn tự mở trong môi trường của bạn. "
            "Hãy đọc tài liệu, lập hoặc cập nhật bộ TAI_LIEU_SONG theo luật sản phẩm rồi phát triển tiếp theo quy trình. "
            + (f"Lưu ý của Chủ tịch: {note}" if note else "")
        )
        self.add_log("director", f"📤 Gửi zip ứng dụng ({len(self.merged_files)} file) cho Giám đốc")
        self.active_speaker = "director"
        try:
            reply, zips = await self.director_turn(text, [{"path": zip_path, "name": zip_name}], allow_rotate=True)
            t = self.thread_of("director")
            t["had_zip"], t["zip_gen"] = True, t["gen"]  # từ giờ thread Giám đốc nào mở mới cũng được đính kèm lại zip
        except Exception as err:
            persona = COUNCIL_PERSONAS["director"]
            self.add_log("director", f"⚠️ Gửi zip cho Giám đốc không xong: {str(err)[:200]}")
            self.council_messages.append({
                "speaker": "director", "name": persona["name"], "avatar": persona["avatar"], "role_title": persona["role_title"],
                "content": f"⚠️ [Thông báo kết nối Bridge :{BRIDGE_PORT}]: {err}", "timestamp": time.strftime("%H:%M:%S"),
            })
            return
        finally:
            self.active_speaker = None
        if ship:
            self.maybe_start_ship(reply, zips)
        else:
            self.add_log("courier", "Gửi zip cho Giám đốc nhưng chưa cho Người ship chạy (theo yêu cầu). Bấm '🚚 Người ship' khi muốn xử lý các zip Giám đốc vừa gửi ra.")

    async def courier_handoff_to_director(self, task_id: str, assigned: str, delivery: Dict[str, Any]) -> None:
        """Giao liên mang gói kết quả từ thread kỹ sư sang thread Giám đốc. GPT nhận kèm zip alpha; DeepSeek nhận danh sách file, nội dung nằm trong kho để Giám đốc xem bằng xem_file."""
        director_kwargs = role_llm_kwargs("director")
        files = list(delivery["files"].keys())
        note = (
            f"Giao liên chuyển kết quả {task_id} từ {assigned}: {delivery.get('summary', '')}. "
            f"Gồm {len(files)} file: {', '.join(files[:40])}{' ...' if len(files) > 40 else ''}."
        )
        attachments = None
        if director_kwargs.get("backend") == "chatgpt":
            attachments = [{"path": delivery["alpha_zip_path"], "name": os.path.basename(delivery["alpha_zip_path"])}]
            note += " Gói alpha đính kèm. Anh kiểm tra và xác nhận đã nhận."
        else:
            note += " Nội dung đã nằm trong kho; anh xem từng file bằng xem_file khi cần, rồi xác nhận đã nhận."
        try:
            resp = await self.ask_with_files(
                "director",
                [COUNCIL_PERSONAS["director"]["system_prompt"], FILE_REQUEST_INSTRUCTION],
                note,
                attachments=attachments,
                **director_kwargs,
            )
        except Exception as err:
            self.add_log("courier", f"⚠️ [Giao liên -> Giám đốc] chưa chuyển được {task_id}: {err}")
            return
        msg = resp.get("choices", [{}])[0].get("message", {})
        self.remember("director", "assistant", msg.get("content") or "", msg.get("reasoning_content"))
        mode = "zip đính kèm" if attachments else "danh sách file, nội dung trong kho"
        self.add_log("courier", f"📨 [Giao liên -> Giám đốc] Đã chuyển {task_id} sang thread Giám đốc ({mode}).")

    def write_project_snapshot_zip(self) -> str:
        """Đóng gói mã nguồn hiện có thành project_snapshot.zip để gửi kèm GPT trong lượt làm việc."""
        os.makedirs(self.project_dir, exist_ok=True)
        zip_path = os.path.join(self.project_dir, "project_snapshot.zip")
        ignore_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__", ".idea", ".vscode", "build", "dist", ".dart_tool"}
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(self.merged_dir):
                dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith(".")]
                for file in files:
                    if file.startswith("."):
                        continue
                    full_p = os.path.join(root, file)
                    zf.write(full_p, os.path.relpath(full_p, self.merged_dir))
        ensure_context_files(zip_path)  # luật sản phẩm và hướng dẫn khung đi kèm mọi zip gửi cho GPT
        return zip_path

    def import_zip_bytes(self, zip_filename: str, raw_bytes: bytes) -> Dict[str, Any]:
        """Nạp file .zip dự án có sẵn của anh vào Công xưởng để tiếp tục phát triển."""
        os.makedirs(self.project_dir, exist_ok=True)
        os.makedirs(self.merged_dir, exist_ok=True)
        uploaded_zip_path = os.path.join(self.project_dir, f"uploaded_{zip_filename}")
        with open(uploaded_zip_path, "wb") as f:
            f.write(raw_bytes)
        return self.import_local_path(uploaded_zip_path, display_name=zip_filename)

    def import_local_path(self, source_path: str, display_name: Optional[str] = None) -> Dict[str, Any]:
        """Nạp từ file .zip hoặc thư mục có sẵn trên máy Mac vào kho mã nguồn của dự án."""
        source_path = os.path.expanduser(source_path.strip())
        if not os.path.exists(source_path):
            raise ValueError(f"Không tìm thấy đường dẫn: {source_path}")
        if os.path.abspath(source_path) in {os.path.abspath(os.path.expanduser("~")), os.path.abspath(os.sep)}:
            raise ValueError("Đừng chọn cả thư mục home hoặc ổ gốc; hãy chọn đúng thư mục dự án.")

        label = display_name or os.path.basename(source_path)
        os.makedirs(self.merged_dir, exist_ok=True)

        ignore_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__", ".idea", ".vscode", "build", "dist", ".dart_tool"}
        ignore_exts = {".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mov", ".icns", ".DS_Store"}

        imported_count = 0
        skipped_count = 0
        if os.path.isfile(source_path) and zipfile.is_zipfile(source_path):
            # Giữ nguyên zip gốc để gửi cho Giám đốc: kho đã nạp bỏ ảnh, video và thư mục build nên không đủ để dựng lại app
            original = os.path.join(self.project_dir, "original_source.zip")
            if os.path.abspath(source_path) != os.path.abspath(original):
                shutil.copy2(source_path, original)

            # Đảm bảo director_upload/project_source.zip luôn sẵn sàng cho Whole-ZIP mode
            outbox = os.path.join(self.project_dir, "director_upload")
            os.makedirs(outbox, exist_ok=True)
            director_zip = os.path.join(outbox, "project_source.zip")
            shutil.copy2(original, director_zip)
            ensure_context_files(director_zip)
            self.threads.setdefault("director", {"gen": 1, "seeded": False, "chars": 0})["had_zip"] = True
            with zipfile.ZipFile(source_path, "r") as zf:
                # Phát hiện nếu tất cả file nằm trong 1 thư mục gốc duy nhất thì lược bỏ prefix đó
                members = [m for m in zf.namelist() if not m.endswith("/") and "__MACOSX" not in m]
                common_root = None
                if members:
                    first_parts = {m.split("/")[0] for m in members if "/" in m}
                    if len(first_parts) == 1 and all("/" in m for m in members):
                        common_root = list(first_parts)[0] + "/"

                for member in members:
                    rel_path = member[len(common_root):] if (common_root and member.startswith(common_root)) else member
                    rel_path = clean_file_path(rel_path)  # chặn "../" và đường dẫn tuyệt đối trong zip
                    if not rel_path:
                        continue
                    parts = rel_path.split("/")
                    if any(p in ignore_dirs for p in parts):
                        continue
                    _, ext = os.path.splitext(rel_path)
                    if ext.lower() in ignore_exts or os.path.basename(rel_path) == ".DS_Store":
                        continue

                    raw_data = zf.read(member)
                    abs_dest = os.path.join(self.merged_dir, rel_path)
                    os.makedirs(os.path.dirname(abs_dest), exist_ok=True)
                    with open(abs_dest, "wb") as out_f:
                        out_f.write(raw_data)

                    try:
                        text_content = raw_data.decode("utf-8")
                        self.merged_files[rel_path] = text_content
                        imported_count += 1
                    except Exception:
                        pass
        elif os.path.isdir(source_path):
            # Bỏ qua file và thư mục ẩn (.ssh, .env, .npmrc...) và liên kết. Không giới hạn số file.
            candidates: List[str] = []
            for root, dirs, files in os.walk(source_path):
                dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith(".")]
                for file in files:
                    full_p = os.path.join(root, file)
                    if file.startswith(".") or os.path.splitext(file)[1].lower() in ignore_exts or os.path.islink(full_p):
                        continue
                    candidates.append(full_p)

            for full_p in candidates:
                rel_p = os.path.relpath(full_p, source_path)
                abs_dest = os.path.join(self.merged_dir, rel_p)
                try:
                    os.makedirs(os.path.dirname(abs_dest), exist_ok=True)
                    shutil.copy2(full_p, abs_dest)
                except OSError:
                    skipped_count += 1
                    continue
                try:
                    with open(full_p, "r", encoding="utf-8") as in_f:
                        self.merged_files[rel_p] = in_f.read()
                        imported_count += 1
                except (OSError, UnicodeDecodeError):
                    pass
        elif os.path.isfile(source_path):
            # Một file lẻ do Chủ tịch chọn (tài liệu yêu cầu, mã nguồn...): chép vào kho, giữ nguyên tên
            dest = os.path.join(self.merged_dir, os.path.basename(source_path))
            shutil.copy2(source_path, dest)
            try:
                with open(source_path, "r", encoding="utf-8") as in_f:
                    self.merged_files[os.path.basename(source_path)] = in_f.read()
                    imported_count += 1
            except (OSError, UnicodeDecodeError):
                pass
        else:
            raise ValueError("Không tìm thấy file hoặc thư mục này.")

        # Đồng bộ toàn bộ file sang build_workspace trên đĩa để Harness thao tác
        ws = getattr(self, "build_workspace", os.path.join(self.project_dir, "build_workspace"))
        os.makedirs(ws, exist_ok=True)
        for rel_p, content in self.merged_files.items():
            target_f = os.path.join(ws, rel_p)
            os.makedirs(os.path.dirname(target_f), exist_ok=True)
            try:
                with open(target_f, "w", encoding="utf-8") as out_f:
                    out_f.write(content)
            except Exception:
                pass

        tree_text, _ = workspace_harness.get_workspace_tree(ws)

        summary_lines = [
            f"📦 Đã nạp thành công **{label}**: {len(self.merged_files)} file mã nguồn vào workspace đĩa.",
            "🌳 **Cây thư mục mã nguồn tự động:**",
            f"```\n{tree_text}\n```",
            "👉 Giám đốc Kỹ thuật và các kỹ sư AI đã có thể định vị, đọc lát cắt và tự động vá code trực tiếp trên đĩa mà không cần nén ZIP."
        ]
        summary_msg = "\n".join(summary_lines)
        self.council_messages.append({
            "speaker": "arbiter",
            "name": "Hệ thống Kho Mã Nguồn (Codebase Loader)",
            "avatar": "📂",
            "role_title": f"Đã nạp {imported_count} files từ {label}",
            "content": summary_msg,
            "timestamp": time.strftime("%H:%M:%S"),
        })
        self.add_log("director", f"📂 Đã nạp dự án có sẵn '{label}' gồm {imported_count} file mã nguồn vào kho Merge & Workspace.")
        return self.to_dict()

    def build_existing_codebase_context(self, max_chars: int = 12000) -> str:
        """Tạo cây thư mục chuẩn Workspace Harness và hướng dẫn đọc/sửa mã trên đĩa cho AI."""
        ws = getattr(self, "build_workspace", os.path.join(self.project_dir, "build_workspace"))
        has_ws = bool(ws and os.path.exists(ws))
        if not has_ws and not self.merged_files:
            return ""

        tree_text = ""
        total_files = len(self.merged_files)
        if has_ws:
            try:
                ascii_tree, json_tree = workspace_harness.get_workspace_tree(ws, max_depth=4, max_entries=140)
                tree_text = ascii_tree
                file_cnt = len([x for x in json_tree if x.get("type") == "file"])
                if file_cnt > 0:
                    total_files = file_cnt
            except Exception:
                tree_text = ""

        if not tree_text and self.merged_files:
            paths = sorted(self.merged_files.keys())
            tree_text = "\n".join(f" - {p}" for p in paths[:120])
            if len(paths) > 120:
                tree_text += f"\n ... và {len(paths) - 120} file khác"

        lines = [
            f"=== CÂY THƯ MỤC LOCAL WORKSPACE HARNESS TRÊN ĐĨA ({total_files} FILES) ===",
            tree_text or "(Thư mục trống hoặc chưa có code)",
            "",
            "=== QUY TẮC ĐỌC VÀ SỬA MÃ NGUỒN TRÊN ĐĨA ===",
            "1. ĐỌC FILE: Ghi `xem_file: <đường/dẫn/file>` để hệ thống trích xuất nội dung file kèm số dòng.",
            "2. TÌM KIẾM MÃ: Ghi `tim_ma: \"<từ_khóa>\"` để tìm các dòng chứa hàm, biến hoặc chuỗi trong toàn bộ dự án trên đĩa.",
            "3. SỬA CODE TRỰC TIẾP TRÊN ĐĨA: Khi đưa ra code chỉnh sửa, sử dụng khối SEARCH/REPLACE:",
            "<<<<<<< SEARCH",
            "// đoạn code cũ chính xác trong file",
            "=======",
            "// đoạn code mới thay thế",
            ">>>>>>>",
            "Hoặc khối ghi đè toàn bộ file:",
            "```ngôn_ngữ file=\"đường/dẫn/file\"",
            "// nội dung file đầy đủ",
            "```",
            "Bộ Local Workspace Harness sẽ tự động vá mã vào đĩa thực tế ngay lập tức.",
        ]
        return "\n".join(lines)

    
    def get_workspace_dir(self) -> str:
        ws = getattr(self, "build_workspace", os.path.join(self.project_dir, "build_workspace"))
        if not os.path.exists(ws):
            ws = os.path.join(self.project_dir, "merged_source")
        os.makedirs(ws, exist_ok=True)
        return ws

    def sessions_list(self) -> List[Dict[str, Any]]:
        if not hasattr(self, "sessions") or not self.sessions:
            self.sessions = [
                {
                    "session_id": "sess_1",
                    "title": "Phiên #1",
                    "created_at": self.created_at,
                    "message_count": len(self.council_messages),
                }
            ]
            self.current_session_id = "sess_1"
        for s in self.sessions:
            if s.get("session_id") == getattr(self, "current_session_id", "sess_1"):
                s["message_count"] = len(self.council_messages)
                break
        return self.sessions

    def create_new_session(self, title: Optional[str] = None) -> Dict[str, Any]:
        """Tạo một phiên làm việc mới cho dự án, lưu phiên cũ và mở phiên chat sạch."""
        if not hasattr(self, "_sessions_history"):
            self._sessions_history = {}
        if not hasattr(self, "sessions") or not self.sessions:
            self.sessions_list()

        cur_id = getattr(self, "current_session_id", "sess_1")
        self._sessions_history[cur_id] = list(self.council_messages)
        for s in self.sessions:
            if s.get("session_id") == cur_id:
                s["message_count"] = len(self.council_messages)
                break

        sess_num = len(self.sessions) + 1
        new_id = f"sess_{int(time.time())}_{uuid.uuid4().hex[:4]}"
        sess_title = title.strip() if title and title.strip() else f"Phiên #{sess_num}"

        new_entry = {
            "session_id": new_id,
            "title": sess_title,
            "created_at": int(time.time()),
            "message_count": 1,
        }
        self.sessions.append(new_entry)
        self.current_session_id = new_id

        self.council_messages = [
            {
                "speaker": "assistant",
                "name": "Trợ lý Điều phối",
                "avatar": "✦",
                "role_title": f"Phiên làm việc mới ({sess_title}) • Kế thừa mã nguồn dự án",
                "content": f"Em đã khởi tạo {sess_title} cho dự án {self.title}. Toàn bộ mã nguồn trên đĩa vẫn được giữ nguyên và sẵn sàng. Mời Chủ tịch giao việc hoặc chỉ đạo nội dung mới!",
                "timestamp": time.strftime("%H:%M:%S"),
            }
        ]
        self._sessions_history[new_id] = list(self.council_messages)
        self.active_speaker = "assistant"
        self.council_thinking = None
        self.notify_event("status_changed", {"session_id": new_id, "active_speaker": "assistant"})
        self.save_state()
        return new_entry

    def switch_session(self, session_id: str) -> bool:
        """Chuyển đổi sang một phiên làm việc khác trong dự án."""
        if not hasattr(self, "_sessions_history"):
            self._sessions_history = {}
        cur_id = getattr(self, "current_session_id", "sess_1")
        self._sessions_history[cur_id] = list(self.council_messages)
        for s in getattr(self, "sessions", []):
            if s.get("session_id") == cur_id:
                s["message_count"] = len(self.council_messages)
                break

        if session_id in self._sessions_history:
            self.current_session_id = session_id
            self.council_messages = list(self._sessions_history[session_id])
            self.active_speaker = "assistant"
            self.council_thinking = None
            self.notify_event("status_changed", {"session_id": session_id})
            self.save_state()
            return True
        return False

    def _detect_responsible_role_from_log(self, error_log: str):
        """Phân tích nội dung log lỗi để tự động điều phối kỹ sư chuyên trách."""
        log_lower = (error_log or "").lower()
        if any(k in log_lower for k in [".css", ".scss", ".html", "widget", "renderflex", "overflowed by", "flutter", "react", "jsx", "tsx", "style", "color", "layout", "flex", "component"]):
            return "subagent-2", "Sub2 - Kỹ sư Frontend & Giao diện"
        if any(k in log_lower for k in ["modulenotfounderror", "importerror", "syntaxerror", "typeerror", "referenceerror", "nullpointer", "undefined is not", "fastapi", "flask", "django", "sql", "sqlite", "database", "api", "route", "token", "auth"]):
            return "subagent-1", "Sub1 - Kỹ sư Backend & Xử lý logic"
        return "director", "Giám đốc Kỹ thuật"

    async def _heal_workspace_with_role(self, role_id: str, ws: str, cmd: str, error_log: str, exit_code: int, attempt: int):
        """Kỹ sư hoặc Giám đốc đọc log lỗi và áp dụng bản vá trực tiếp vào đĩa workspace dev."""
        candidates = []
        for line in error_log.splitlines()[-40:]:
            m = re.search(r'["\']?([a-zA-Z0-9_\-./]+\.[a-zA-Z0-9]+)["\']?', line)
            if m:
                p = m.group(1).replace("\\", "/").lstrip("/")
                if os.path.exists(os.path.join(ws, p)) and p not in candidates:
                    candidates.append(p)

        file_contexts = []
        for p in candidates[:3]:
            res = workspace_harness.read_file_slice(ws, p, start_line=1, line_count=120)
            if res.get("content"):
                file_contexts.append(f"--- FILE: {p} ---\n{res.get('content')}")

        role_info = COUNCIL_PERSONAS.get(role_id, COUNCIL_PERSONAS["director"])
        prompt = (
            f"Bạn là {role_info.get('name')} ({role_info.get('title')}).\n"
            f"Lệnh kiểm thử build `{cmd}` thất bại (Exit code {exit_code}) tại vòng tự sửa #{attempt}.\n\n"
            f"ĐOẠN LOG LỖI:\n```text\n{error_log[-3500:]}\n```\n\n"
            + ("NỘI DUNG FILE ĐANG BỊ LỖI TRÊN ĐĨA:\n" + "\n\n".join(file_contexts) if file_contexts else "") +
            "\n\nHãy phân tích nguyên nhân lỗi và đưa ra bản vá mã nguồn chính xác để sửa triệt để lỗi trên.\n"
            "Dùng định dạng bản vá chuẩn SEARCH/REPLACE hoặc khối ghi đè file để hệ thống áp dụng tự động vào đĩa workspace `dev` ngay lập tức."
        )

        resp = await self.ask_role(
            role_id,
            [f"Bạn là {role_info.get('name')}. Hãy khắc phục lỗi mã nguồn được giao."],
            prompt,
            **role_llm_kwargs(role_id),
        )
        msg = resp.get("choices", [{}])[0].get("message", {})
        reply = msg.get("content", "")

        if reply:
            h_res = workspace_harness.extract_and_apply_harness_edits(ws, reply)
            applied_cnt = h_res.get("applied_count", 0)
            if applied_cnt > 0:
                files_str = ", ".join(x.get("file", "") for x in h_res.get("applied", []))
                self.add_log(role_id, f"🛠️ [{role_info.get('name')}] Đã vá thành công {applied_cnt} file trên đĩa Dev: {files_str}")
            else:
                self.add_log(role_id, f"ℹ️ [{role_info.get('name')}] Đã đề xuất phương án: {reply[:120]}...")

    async def run_self_healing_loop(self, cmd: str, initial_log: str, initial_code: int, max_rounds: int = 5):
        """Vòng lặp Tự sửa lỗi (Dev Workspace & Self-Healing Pipeline):
        1. Tạo/chuyển sang môi trường `dev` để cách ly hoàn toàn, bảo vệ `main`.
        2. Tự động điều phối AI theo chuyên môn (Sub1 Backend / Sub2 Frontend / Director).
        3. Vá lỗi trực tiếp trong thư mục dev và kiểm thử lại.
        4. Khi PASS 100%: Tự động thay thế/Safe Merge vào `main`, đóng gói full_source.zip hoàn thiện và cập nhật Live Preview.
        """
        self.builder_state["status"] = "healing"
        self.active_speaker = "tester"
        err_snippet = (initial_log or "")[-1200:]
        ws = self.get_workspace_dir()

        # [BƯỚC 1] KHỞI TẠO HOẶC CHUYỂN SANG NHÁNH / THƯ MỤC DEV
        try:
            workspace_harness.git_init_workspace(ws)
            workspace_harness.git_create_branch(ws, "dev", checkout=True)
            self.add_log("git", "🌿 [Dev Workspace] Đã tạo & chuyển sang nhánh 'dev' cách ly. Mọi hoạt động vá lỗi và thử nghiệm sẽ diễn ra tại đây để bảo vệ nhánh 'main'.")
        except Exception as e:
            self.add_log("git", f"⚠️ Không thể khởi tạo nhánh dev: {e}")

        self.council_messages.append({
            "speaker": "tester",
            "name": "Kỹ sư Kiểm thử",
            "avatar": "✓",
            "role_title": "Báo cáo Kiểm thử • Kích hoạt Môi trường Dev & Tự sửa lỗi",
            "content": f"⚠️ **[Kiểm thử] Lệnh `{cmd}` thất bại (Exit code {initial_code})!**\n\n```text\n{err_snippet}\n```\n\n🌿 **Đã chuyển toàn bộ môi trường sang thư mục/nhánh `dev` cách ly**.\n🔧 Hệ thống đang tự động điều phối Kỹ sư chuyên trách vào sửa lỗi và kiểm thử lại cho đến khi hoàn thiện...",
            "timestamp": time.strftime("%H:%M:%S"),
        })
        self.notify_event("new_message", {"speaker": "tester"})

        cur_log = initial_log
        cur_code = initial_code

        for attempt in range(1, max_rounds + 1):
            # [BƯỚC 2] THUẬT TOÁN ĐIỀU PHỐI AI THEO CHUYÊN MÔN (SMART DISPATCH)
            assigned_role, role_title = self._detect_responsible_role_from_log(cur_log)
            self.active_speaker = assigned_role
            self.council_thinking = f"◈ {role_title} đang phân tích log lỗi và tạo bản vá (Vòng tự sửa #{attempt}/{max_rounds} trong Dev)..."
            self.notify_event("status_changed", {"active_speaker": assigned_role, "council_thinking": self.council_thinking})
            self.add_log(assigned_role, f"🩺 [Tự sửa lỗi #{attempt}] {role_title} tiếp nhận xử lý lỗi từ `{cmd}`. Đang phân tích mã nguồn...")

            try:
                await self._heal_workspace_with_role(assigned_role, ws, cmd, cur_log, cur_code, attempt)
            except Exception as e:
                self.add_log(assigned_role, f"⚠️ [Tự sửa lỗi #{attempt}] Lỗi khi tạo bản vá: {e}")

            # Lưu Checkpoint trên nhánh Dev
            try:
                workspace_harness.git_commit(ws, f"fix: Bản vá tự động vòng #{attempt} ({role_title})", author_role=role_title)
            except Exception:
                pass

            # [BƯỚC 3] TÁI KIỂM THỬ TRONG MÔI TRƯỜNG DEV
            self.active_speaker = "tester"
            self.council_thinking = f"✓ Kiểm thử đang chạy lại lệnh `{cmd}` để xác thực bản vá #{attempt} trong Dev..."
            self.notify_event("status_changed", {"active_speaker": "tester", "council_thinking": self.council_thinking})

            t0 = time.time()
            code, out = await asyncio.to_thread(BuilderRunnerWorker._run_shell, cmd, ws, 300)
            duration = time.time() - t0

            passed = (code == 0)
            self.builder_state["last_exit_code"] = code
            self.builder_state["last_log"] = out
            self.builder_state.setdefault("runs", []).append({
                "iteration": len(self.builder_state.get("runs", [])) + 1,
                "command": cmd,
                "exit_code": code,
                "passed": passed,
                "log": out,
                "duration_sec": round(duration, 2),
                "timestamp": time.strftime("%H:%M:%S")
            })

            # [BƯỚC 4] KHI BUILD/TEST THÀNH CÔNG: TỰ ĐỘNG THAY THẾ MAIN & ĐÓNG GÓI BẢN HOÀN THIỆN
            if passed:
                self.builder_state["status"] = "passed"
                self.active_speaker = "assistant"
                self.council_thinking = None

                # Thực hiện Safe Merge dev -> main
                try:
                    merge_res = workspace_harness.git_merge(ws, "dev", "main")
                    workspace_harness.git_switch_branch(ws, "main")
                    self.add_log("git", f"🚀 [Safe Merge] Đã thay thế thành công bản hoàn thiện từ `dev` vào nhánh chính `main` (Commit: {merge_res.get('commit', 'ok')}).")
                except Exception as m_err:
                    self.add_log("git", f"⚠️ Cảnh báo hợp nhất main: {m_err}")

                # Đóng gói bản hoàn thiện full_source.zip
                self.rebuild_merged_files()
                full_zip = self._package_full_source_zip()

                self.council_messages.append({
                    "speaker": "tester",
                    "name": "Kỹ sư Kiểm thử",
                    "avatar": "✓",
                    "role_title": "Báo cáo Kiểm thử • Đã Hoàn Thiện & Thay Thế Main",
                    "content": (
                        f"🎉 **[BẢN BUILD HOÀN THIỆN ĐÃ SẴN SÀNG]**\n\n"
                        f"Sau **{attempt}** vòng tự động điều phối AI khắc phục lỗi trong môi trường **Dev**:\n"
                        f"- ✅ **Lệnh kiểm thử:** `{cmd}` ➔ **PASS 100%** (Exit code 0, thời gian: {round(duration, 1)}s).\n"
                        f"- 🌿 **Thay thế Main:** Toàn bộ mã nguồn đã kiểm thử thành công đã được **tự động thay thế an toàn vào nhánh `main`**.\n"
                        f"- 📦 **Gói xuất xưởng:** Đã tạo bản nén hoàn chỉnh `{os.path.basename(full_zip)}` ({len(self.merged_files)} files) sẵn sàng cho Chủ tịch tải về và vận hành.\n"
                        f"- 📱 **Live Preview:** Giao diện xem trước đã được cập nhật sang bản chạy mượt mà nhất!"
                    ),
                    "timestamp": time.strftime("%H:%M:%S"),
                })
                self.notify_event("new_message", {"speaker": "tester"})
                self.add_log("tester", f"🏆 [Kiểm thử PASS] Lệnh `{cmd}` đã vượt qua sau {attempt} vòng tự sửa lỗi. Đã thay thế main.")
                self.save_state()
                return

            cur_log = out
            cur_code = code

        # Nếu hết số vòng mà vẫn chưa pass
        self.builder_state["status"] = "failed"
        self.active_speaker = "assistant"
        self.council_thinking = None
        self.council_messages.append({
            "speaker": "tester",
            "name": "Kỹ sư Kiểm thử",
            "avatar": "✓",
            "role_title": "Báo cáo Kiểm thử • Giữ nguyên Main an toàn",
            "content": f"⚠️ Đã thực hiện **{max_rounds}** vòng tự sửa trong môi trường **Dev** nhưng lệnh `{cmd}` vẫn gặp lỗi (Exit code {cur_code}).\n\n🛡️ **Nhánh chính `main` vẫn được giữ an toàn tuyệt đối, không bị lỗi dở dang**. Kính mời Chủ tịch kiểm tra log và đưa thêm định hướng chỉ đạo.",
            "timestamp": time.strftime("%H:%M:%S"),
        })
        self.notify_event("new_message", {"speaker": "tester"})
        self.save_state()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "title": self.title,
            "created_at": self.created_at,
            "status": self.status,
            "project_dir": self.project_dir,
            "council_messages": self.council_messages,
            "council_thinking": self.council_thinking,
            "active_speaker": self.active_speaker,
            "live_reasoning": self.live_reasoning,
            "meeting_minutes_md": self.meeting_minutes_md,
            "tech_spec_md": self.tech_spec_md,
            "install_command": self.install_command,
            "test_command": self.test_command,
            "run_command": self.run_command,
            "roadmap": self.roadmap,
            "director_state": self.director_state,
            "subagent_states": self.subagent_states,
            "courier_state": self.courier_state,
            "builder_state": self.builder_state,
            "merged_files": list(self.merged_files.keys()),
            "full_zip_path": self.full_zip_path,
            "activity_logs": self.activity_logs[-120:],
            "role_memory": self.role_memory,
            "decision_record": self.decision_record,
            "awaiting_approval": self.awaiting_approval,
            "meeting": self._meeting_public(),
            "ship_state": self.ship_state,
            "shipping": self._shipping,
            "autopilot": self.autopilot,
            "role_codes": self.role_codes,
            "director_zips": self.director_zip_list(),
            "director_session": self.director_session_info(),
            "sessions": self.sessions_list(),
            "current_session_id": getattr(self, "current_session_id", "sess_1"),
        }

    # ---- Bộ nhớ riêng của từng vai trò ----
    def memory_of(self, role_id: str) -> Dict[str, Any]:
        return self.role_memory.setdefault(role_id, {"summary": "", "messages": []})

    def remember(self, role_id: str, role: str, content: str, reasoning: Optional[str] = None, room: bool = False) -> None:
        """Ghi một tin vào lịch sử riêng của vai trò. role: 'user' (Chủ tịch) hoặc 'assistant' (vai trò trả lời).
        reasoning: tóm tắt lập luận do chính model viết (khối <reasoning>), giữ lại để đưa vào các lượt sau."""
        entry = {"role": role, "content": content, "timestamp": time.strftime("%H:%M:%S")}
        if reasoning:
            entry["reasoning"] = reasoning
        if room:
            entry["room"] = True
        self.memory_of(role_id)["messages"].append(entry)

    def memory_chars(self, role_id: str) -> int:
        m = self.memory_of(role_id)
        return len(m["summary"]) + sum(len(_with_reasoning(x)) for x in m["messages"])

    def memory_for_llm(self, role_id: str) -> List[Dict[str, str]]:
        """Lịch sử riêng của vai trò để đưa vào prompt: bản tóm tắt cũ (nếu có) rồi đến các tin."""
        m = self.memory_of(role_id)
        out: List[Dict[str, str]] = []
        if m["summary"]:
            out.append({"role": "system", "content": f"Tóm tắt lịch sử trước đó của bạn:\n{m['summary']}"})
        out.extend({"role": x["role"], "content": _with_reasoning(x)} for x in m["messages"])
        return out

    async def compact_memory(self, role_id: str, force: bool = False) -> None:
        """Khi lịch sử gần đầy ngữ cảnh hoặc khi đổi phiên sạch: tóm tắt phần cũ bằng chính model của vai trò, giữ nguyên các tin gần nhất."""
        m = self.memory_of(role_id)
        if not force:
            if self.memory_chars(role_id) <= MEMORY_CHAR_BUDGET or len(m["messages"]) <= MEMORY_KEEP_RECENT:
                return
            old, recent = m["messages"][:-MEMORY_KEEP_RECENT], m["messages"][-MEMORY_KEEP_RECENT:]
        else:
            if not m["messages"] and not m.get("summary"):
                return
            keep = min(2, len(m["messages"]))
            old = m["messages"][:-keep] if keep > 0 else list(m["messages"])
            recent = m["messages"][-keep:] if keep > 0 else []
            if not old and m.get("summary"):
                return

        transcript = "\n\n".join(f"[{x['role']} {x['timestamp']}] {_with_reasoning(x)}" for x in old)
        messages = [
            {"role": "system", "content": (
                "Bạn tóm tắt lịch sử làm việc cho chính người đã tham gia. Giữ đầy đủ quyết định, yêu cầu, "
                "ràng buộc, số liệu, danh sách các file đã kiểm tra hoặc sửa đổi và việc còn dở. Viết bằng tiếng Việt, gọn, tối đa 2000 từ."
            )},
            {"role": "user", "content": f"Tóm tắt trước đó:\n{m.get('summary') or '(chưa có)'}\n\nCác tin cần gộp vào tóm tắt:\n{transcript}"},
        ]
        summary = ""
        try:
            resp = await call_bridge_llm(messages, session_id=f"summary-{role_id}", stateless=True, timeout=90, **role_llm_kwargs(role_id))
            summary = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as err:
            self.add_log("system", f"⚠️ Tóm tắt qua bridge thất bại ({err}), sử dụng bản tóm tắt nội bộ")

        if not summary:
            summary = f"Dự án hiện có {len(getattr(self, 'merged_files', {}))} file mã nguồn trên đĩa. Các trao đổi gần nhất: " + " | ".join(
                f"[{x['role']}]: {x['content'][:120]}" for x in old[-6:]
            )

        m["summary"] = summary
        m["messages"] = recent
        self.add_log("system", f"🧠 Đã tóm tắt thành công {len(old)} tin làm việc trong lịch sử của {role_id}")
        self._persist()

    # ---- Thread bền của từng vai trò trong bridge ----
    def director_rules_note(self) -> str:
        """Luật sản phẩm và chuẩn Harness Native, báo cho Giám đốc một lần mỗi thread (và mỗi khi RULES_VERSION tăng)."""
        return (
            f"{PRODUCT_RULES_INSTRUCTION}\n\n"
            "Chế độ làm việc: Toàn bộ mã nguồn đã có trên ổ đĩa máy chủ (build_workspace). "
            "Khi cần đọc file, hãy xuất khối xem_file. Khi sửa code, hãy xuất khối SEARCH/REPLACE. "
            "Công xưởng sẽ tự động đồng bộ trực tiếp vào đĩa."
        )

    def director_zip_list(self, limit: int = 30) -> List[Dict[str, Any]]:
        """Các zip Chủ tịch có thể tải về, mới nhất trước: zip Giám đốc đã gửi ra và zip sản phẩm do build tạo ra (kind 'build')."""
        items = []
        for folder, kind in ((os.path.join(self.project_dir, "director_inbox"), "director"), (os.path.join(self.project_dir, "build_outputs"), "build")):
            if not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                full = os.path.join(folder, name)
                if name.lower().endswith(".zip") and os.path.isfile(full):
                    stat = os.stat(full)
                    items.append({"name": name, "size": stat.st_size, "mtime": int(stat.st_mtime), "kind": kind})
        return sorted(items, key=lambda x: x["mtime"], reverse=True)[:limit]

    def _prime_inbox_seen(self) -> None:
        """Zip đã nằm sẵn trong hộp thư khi nạp lại dự án là việc của lần chạy trước, không phải tín hiệu mới (không thì lượt Giám đốc đầu tiên sau khi khởi động lại sẽ ship lại zip cũ)."""
        inbox = os.path.join(self.project_dir, "director_inbox")
        if os.path.isdir(inbox):
            for name in os.listdir(inbox):
                if name.lower().endswith(".zip"):
                    full = os.path.join(inbox, name)
                    self._director_seen.add((full, os.stat(full).st_mtime_ns))
        self._inbox_primed = True

    def director_has_zip(self) -> bool:
        """Máy ảo của thread Giám đốc hiện tại có zip mã nguồn không. Mở thread mới hoặc bridge khởi động lại thì máy ảo mất zip."""
        t = self.thread_of("director")
        return bool(t.get("seeded")) and t.get("zip_gen") == t.get("gen")

    def director_restore_attachments(self) -> List[Dict[str, str]]:
        """Thread Giám đốc mới (sau khi đầy ngữ cảnh, khởi động lại bridge hay server) không còn zip trong máy ảo: đính kèm lại bản gốc và các zip mới nhất của chính Giám đốc."""
        files: List[Dict[str, str]] = []
        base = os.path.join(self.project_dir, "director_upload", "project_source.zip")
        if os.path.isfile(base):
            files.append({"path": base, "name": "project_source.zip"})
        inbox = self.director_inbox()
        recent = [
            os.path.join(inbox, n) for n in os.listdir(inbox)
            if n.lower().endswith(".zip") and not any(x in n.lower() for x in ("alpha", "build_log", "ban_giao", "bangiao"))
        ]
        recent.sort(key=os.path.getmtime, reverse=True)
        for path in recent[:2]:
            files.append({"path": path, "name": os.path.basename(path)})
        return [f for f in files if os.path.getsize(f["path"]) < 150 * 1024 * 1024]

    def thread_of(self, role_id: str) -> Dict[str, Any]:
        return self.threads.setdefault(role_id, {"gen": 1, "seeded": False, "chars": 0})

    def thread_session_id(self, role_id: str) -> str:
        return f"{self.project_id}-{role_id}-g{self.thread_of(role_id)['gen']}"

    def _persist(self) -> None:
        """Ghi trạng thái dự án ngay (phiên, tin nhắn) để khởi động lại giữa chừng không làm mất phiên đang dùng. Lỗi ghi không làm dừng việc đang chạy."""
        try:
            self.save_state()
        except Exception as err:
            print(f"⚠️ Không lưu được trạng thái dự án {self.project_id}: {err}")

    def _close_session_later(self, session_id: str) -> None:
        """Đóng tab của phiên đã bỏ trong bridge (không xóa cuộc trò chuyện: Chủ tịch vẫn xem lại được trong lịch sử ChatGPT)."""
        try:
            task = asyncio.get_running_loop().create_task(close_bridge_session(session_id))
        except RuntimeError:
            return
        tasks = self.__dict__.setdefault("_bg_tasks", set())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def rotate_thread(self, role_id: str) -> Dict[str, Any]:
        """Mở thread mới cho vai trò (cuộc trò chuyện mới trong ChatGPT): lần hỏi kế tiếp gửi lại gói khởi đầu. Phiên cũ được đóng tab, cuộc trò chuyện cũ còn trong lịch sử ChatGPT."""
        t = self.thread_of(role_id)
        retired = self.thread_session_id(role_id)
        t.update({
            "gen": t["gen"] + 1,
            "seeded": False,
            "chars": 0,
            "turns": 0,
            "edits": 0,
            "rules_v": 0,
            "url": None,
            "deliveries": 0,
            "rotate_pending": False,
        })
        self._close_session_later(retired)
        self._persist()
        return t

    def check_bridge_restart(self) -> None:
        """Ghi nhận bridge khởi động lại. Phiên không mất: bridge nhớ địa chỉ cuộc trò chuyện và mở lại đúng chỗ; chỉ khi không mở lại được
        (bridge trả conversation_lost) factory mới nạp lại gói khởi đầu."""
        started = _bridge_started_at()
        if started is None:
            return
        if self._bridge_started_at is not None and started != self._bridge_started_at:
            self.add_log("system", "🔄 Bridge vừa khởi động lại: các phiên sẽ được mở lại đúng cuộc trò chuyện đã lưu")
        self._bridge_started_at = started

    def note_director_turn(self, edited_files_count: int = 0) -> None:
        """Ghi nhận một lượt làm việc của Giám đốc. Nếu đạt giới hạn 20-30 lượt tương tác hoặc sửa file code (mặc định DIRECTOR_ROTATE_EVERY=25),
        đánh dấu tự động tóm tắt và mở phiên mới tinh ở lượt kế tiếp để chống tràn ngữ cảnh và lag tab."""
        t = self.thread_of("director")
        t["turns"] = int(t.get("turns", 0)) + 1
        if edited_files_count > 0:
            t["edits"] = int(t.get("edits", 0)) + edited_files_count
        
        limit = DIRECTOR_ROTATE_EVERY
        should_rotate = (
            t["turns"] >= limit
            or int(t.get("edits", 0)) >= limit
        )
        if should_rotate and not t.get("rotate_pending"):
            t["rotate_pending"] = True
            reason = f"{t['turns']} lượt tương tác" if t["turns"] >= limit else f"{t.get('edits')} lần sửa code"
            self.add_log("system", f"🧵 [Quản lý Phiên] Phiên hiện tại đã đạt {reason}. Lượt tới của Chủ tịch sẽ tự động tóm tắt và mở phiên mới tinh (Thế hệ {t['gen'] + 1}) để chống lag và nuốt tin nhắn.")
        self._persist()

    def note_director_delivery(self) -> None:
        """Tương thích ngược: Giám đốc xuất mã nguồn."""
        self.note_director_turn(edited_files_count=1)

    def director_session_info(self) -> Dict[str, Any]:
        """Tình trạng phiên Giám đốc cho giao diện: số thế hệ, số lượt tương tác, số lần sửa code, bao giờ đổi phiên."""
        t = self.threads.get("director") or {}
        raw_gen = int(t.get("gen", 1))
        display_gen = raw_gen if raw_gen < 100_000 else max(1, raw_gen - int(self.created_at) + 1)
        return {
            "gen": display_gen,
            "raw_gen": raw_gen,
            "turns": int(t.get("turns", 0)),
            "edits": int(t.get("edits", 0)),
            "chars": int(t.get("chars", 0)),
            "rotate_every": DIRECTOR_ROTATE_EVERY,
            "rotate_pending": bool(t.get("rotate_pending")),
            "seeded": bool(t.get("seeded")),
            "has_url": bool(t.get("url")),
        }

    async def ask_role(
        self,
        role_id: str,
        system_messages: List[str],
        user_text: str,
        *,
        context: str = "",
        remember_user: bool = True,
        allow_rotate: bool = False,
        **call_kwargs: Any,
    ) -> Dict[str, Any]:
        """Hỏi một vai trò theo nguồn đã chọn của vai trò đó.
        ChatGPT qua bridge: mỗi vai trò giữ MỘT phiên bền. Cuộc trò chuyện trên web tự giữ lịch sử, nên chỉ lượt đầu của phiên mới gửi kèm chỉ dẫn;
        Giám đốc không bao giờ được gửi lại lịch sử (phiên mới đọc tài liệu sống trong zip), các vai khác nạp lại lịch sử riêng nếu phiên mất.
        Khởi động lại factory hay bridge không mở phiên mới: bridge nhớ địa chỉ cuộc trò chuyện và mở lại đúng chỗ; không mở lại được (conversation_lost)
        thì mới nạp lại gói khởi đầu vào phiên mới. Giám đốc: cứ DIRECTOR_ROTATE_EVERY lần gửi zip ra thì mở phiên mới; allow_rotate=True cho phép đổi phiên ở lượt này
        (lượt do Chủ tịch gửi; các lượt tự động của vòng ship không đổi phiên giữa chừng).
        DeepSeek API (không dùng): mỗi lượt gửi lại chỉ dẫn và toàn bộ lịch sử riêng, nén khi quá dài.
        remember_user: có ghi tin này vào lịch sử riêng không (caller tự ghi nếu False)."""
        backend = call_kwargs.get("backend", "chatgpt")
        t = self.thread_of(role_id)
        if backend != "deepseek_api" and t.get("backend") not in (None, backend):
            # Đổi nguồn: thread cũ không có các lượt của nguồn mới, mở thread mới và nạp lại lịch sử đầy đủ
            self.rotate_thread(role_id)
            self.add_log("system", f"🔀 {role_id} đổi nguồn sang {backend}: mở thread mới và nạp lại lịch sử")
        t["backend"] = backend
        persistent_director = role_id == "director" and backend == "chatgpt"
        if backend == "deepseek_api":
            await self.compact_memory(role_id)
        else:
            if persistent_director:
                should_rotate = allow_rotate and (
                    t.get("rotate_pending")
                    or int(t.get("turns", 0)) >= DIRECTOR_ROTATE_EVERY
                    or int(t.get("edits", 0)) >= DIRECTOR_ROTATE_EVERY
                )
                if should_rotate:
                    s_old = self.director_session_info()
                    old_gen = s_old.get("gen", 1)
                    await self.compact_memory(role_id, force=True)
                    t = self.rotate_thread(role_id)
                    s_new = self.director_session_info()
                    gen_display = s_new.get("gen", 1)
                    self.add_log("system", f"🧵 [Đổi Phiên Sạch] Đã tự động tóm tắt ngữ cảnh và mở phiên Giám đốc mới (thế hệ {gen_display}) để bảo đảm không bị đơ trình duyệt và không sót tin nhắn.")
                    self.council_messages.append({
                        "speaker": "system",
                        "name": "Local Workspace Harness",
                        "avatar": "🔄",
                        "role_title": "Tự động làm mới phiên",
                        "content": f"🔄 **[Tự Động Làm Mới Phiên - Thế Hệ {gen_display}]**\nĐã hoàn thành chu kỳ làm việc của Thế hệ {old_gen}. Hệ thống đã tóm tắt toàn bộ tiến trình kỹ thuật và khởi tạo một phiên ChatGPT sạch mới.\n- Mã nguồn trên đĩa giữ nguyên 100%.\n- Ngữ cảnh được nạp lại súc tích, hoàn toàn không lo lag tab hay nuốt tin nhắn.",
                        "timestamp": time.strftime("%H:%M:%S"),
                    })
            elif t["chars"] > MEMORY_CHAR_BUDGET:
                await self.compact_memory(role_id, force=True)
                t = self.rotate_thread(role_id)
                self.add_log("system", f"🧵 Thread của {role_id} đã đầy: đã tóm tắt và mở thread mới (thế hệ {t['gen']})")
            await asyncio.to_thread(self.check_bridge_restart)

        if remember_user:
            self.remember(role_id, "user", user_text)
        restore: List[Dict[str, str]] = []
        for attempt in (0, 1):
            short = bool(t["seeded"]) and backend != "deepseek_api"  # phiên đã có sẵn: chỉ gửi tin mới
            restore = []
            refreshed = False
            if persistent_director and (t.get("had_zip") or bool(self.latest_source_zip())):
                idle_long = short and bool(t.get("last_at")) and time.time() - float(t["last_at"]) > DIRECTOR_REFRESH_IDLE_S
                if not short or idle_long:
                    restore = self.director_restore_attachments()
                    refreshed = bool(restore) and short
            kwargs = dict(call_kwargs)
            if restore:
                have = {a.get("path") for a in (kwargs.get("attachments") or [])}
                kwargs["attachments"] = list(kwargs.get("attachments") or []) + [a for a in restore if a["path"] not in have]
            need_rules = role_id == "director" and backend != "deepseek_api" and t.get("rules_v") != RULES_VERSION
            if short:
                body = f"{context}\n\n{user_text}" if context else user_text
                if need_rules:  # thread cũ đã nạp chỉ dẫn từ trước khi có luật sản phẩm và mã gọi vai: báo một lần
                    body = f"{self.director_rules_note()}\n\n{body}"
                if refreshed:
                    body = f"{DIRECTOR_REFRESH_NOTE}\n\n{body}"
                messages = [{"role": "user", "content": body}]
            else:
                messages = [{"role": "system", "content": text} for text in system_messages]
                # Đưa bản tóm tắt tiến trình vào đầu phiên mới:
                m = self.memory_of(role_id)
                if m.get("summary"):
                    messages.append({
                        "role": "system",
                        "content": f"BẢN TÓM TẮT TIẾN TRÌNH & CÁC QUYẾT ĐỊNH KỸ THUẬT TỪ CÁC PHIÊN TRƯỚC:\n{m['summary']}\n(Bạn tiếp tục làm việc dựa trên tóm tắt này và mã nguồn hiện có trên đĩa; không hỏi lại những điều Chủ tịch đã quyết định)."
                    })
                if need_rules:
                    messages.append({"role": "system", "content": self.director_rules_note()})
                if context:
                    messages.append({"role": "system", "content": context})
                if persistent_director:
                    messages.append({"role": "user", "content": user_text})  # lịch sử do ChatGPT giữ: không nạp lại
                else:
                    messages.extend(self.memory_for_llm(role_id))
                    if not remember_user:
                        messages.append({"role": "user", "content": user_text})

            sent_chars = sum(len(m["content"]) for m in messages)
            try:
                resp = await call_bridge_llm(
                    messages,
                    session_id=self.thread_session_id(role_id),
                    stateless=False,
                    chatgpt_project=self.chatgpt_project if backend == "chatgpt" else None,
                    persist_session=backend == "chatgpt",
                    resume_expected=backend == "chatgpt" and short,
                    **kwargs,
                )
            except BridgeConversationLost as err:
                if attempt == 0 and short:
                    self.add_log("system", f"🔌 {role_id}: không mở lại được cuộc trò chuyện cũ ({str(err)[:140]}); nạp lại gói khởi đầu vào cuộc trò chuyện mới")
                    t.update({"seeded": False, "rules_v": 0, "url": None})
                    self._persist()
                    continue
                raise
            break
        t["seeded"] = True
        t["last_at"] = time.time()
        if resp.get("conversation_url"):
            t["url"] = resp["conversation_url"]
        if role_id == "director":
            t["rules_v"] = RULES_VERSION
            if restore:
                t["zip_gen"] = t["gen"]
        reply = resp.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        t["chars"] += sent_chars + len(reply)
        self._persist()
        return resp

    def find_project_file(self, requested: str) -> Optional[str]:
        """Tìm file theo đường dẫn đầy đủ hoặc tương đối trong build_workspace hoặc merged_files."""
        ws = getattr(self, "build_workspace", os.path.join(self.project_dir, "build_workspace"))
        name = requested.strip().replace("\\", "/")
        if name.startswith("./"):
            name = name[2:]
        if ws and os.path.isfile(os.path.join(ws, name)):
            return name
        if name in self.merged_files:
            return name
        matches = [path for path in self.merged_files if path.endswith("/" + name)]
        if len(matches) == 1:
            return matches[0]
        if ws and os.path.isdir(ws):
            disk_matches = []
            for root, _, files in os.walk(ws):
                for f in files:
                    full_p = os.path.join(root, f)
                    rel_p = os.path.relpath(full_p, ws).replace(os.sep, "/")
                    if rel_p == name or rel_p.endswith("/" + name):
                        disk_matches.append(rel_p)
            if len(disk_matches) == 1:
                return disk_matches[0]
        return matches[0] if matches else None

    def read_requested_files(self, paths: List[str], keywords: List[str]) -> tuple:
        """Đọc file và tìm mã theo yêu cầu, trong ngân sách của một lượt. Trả về (nội dung gửi lại, tóm tắt để ghi nhật ký và lịch sử)."""
        sections: List[str] = []
        done: List[str] = []
        missing: List[str] = []
        deferred: List[str] = list(paths[FILE_REQUEST_MAX_PATHS:]) + [f'tìm "{kw}"' for kw in keywords[FILE_REQUEST_MAX_KEYWORDS:]]
        used = 0
        ws = getattr(self, "build_workspace", os.path.join(self.project_dir, "build_workspace"))
        for requested in paths[:FILE_REQUEST_MAX_PATHS]:
            real = self.find_project_file(requested)
            if real is None:
                missing.append(requested)
                continue
            content = ""
            disk_path = os.path.join(ws, real) if ws else None
            if disk_path and os.path.isfile(disk_path):
                try:
                    with open(disk_path, "r", encoding="utf-8") as f:
                        content = f.read()
                except Exception:
                    content = self.merged_files.get(real, "")
            else:
                content = self.merged_files.get(real, "")
            lines = content.splitlines()
            numbered = [f"L{i+1:04d}: {l}" for i, l in enumerate(lines)]
            numbered_text = "\n".join(numbered)
            body = numbered_text[:FILE_CHUNK_MAX_CHARS]
            if len(numbered_text) > FILE_CHUNK_MAX_CHARS:
                body += f"\n... (cắt bớt: file dài {len(numbered_text)} ký tự, chỉ gửi {FILE_CHUNK_MAX_CHARS} ký tự đầu)"
            section = f"--- FILE: {real} ({len(lines)} dòng) ---\n{body}"
            if used + len(section) > FILE_ROUND_MAX_CHARS:
                deferred.append(requested)
                continue
            sections.append(section)
            used += len(section)
            done.append(f"{real} ({len(lines)} dòng)")
        for keyword in keywords[:FILE_REQUEST_MAX_KEYWORDS]:
            if len(keyword) < 2:
                missing.append(f'tìm "{keyword}" (từ khóa quá ngắn)')
                continue
            needle = keyword.lower()
            total = 0
            shown: List[str] = []

            # 1. Quét tìm trực tiếp trên đĩa build_workspace qua workspace_harness
            if ws and os.path.exists(ws):
                try:
                    disk_hits = workspace_harness.search_code(ws, keyword, max_results=SEARCH_MAX_HITS)
                    total = len(disk_hits)
                    for hit in disk_hits:
                        content_snippet = hit.get("text", hit.get("content", ""))[:160]
                        shown.append(f"{hit['file']}:{hit['line']}: {content_snippet}")
                except Exception:
                    pass

            # 2. Nếu đĩa chưa có hoặc không tìm thấy, tìm trong memory self.merged_files
            if not shown and self.merged_files:
                for path in sorted(self.merged_files):
                    if needle in path.lower():
                        total += 1
                        if len(shown) < SEARCH_MAX_HITS:
                            shown.append(f"{path} (tên file khớp)")
                    content = self.merged_files[path]
                    if needle not in content.lower():
                        continue
                    for number, line in enumerate(content.splitlines(), 1):
                        if needle in line.lower():
                            total += 1
                            if len(shown) < SEARCH_MAX_HITS:
                                shown.append(f"{path}:{number}: {line.strip()[:160]}")
            block = [f'--- TÌM "{keyword}": {total} kết quả ---', *shown]
            if total > len(shown):
                block.append(f"... còn {total - len(shown)} kết quả, hãy thu hẹp từ khóa")
            section = "\n".join(block)
            if used + len(section) > FILE_ROUND_MAX_CHARS:
                deferred.append(f'tìm "{keyword}"')
                continue
            sections.append(section)
            used += len(section)
            done.append(f'tìm "{keyword}" ({total} kết quả)')
        parts = ["Công xưởng gửi lại kết quả xem mã nguồn từ ổ đĩa theo yêu cầu của bạn (đã đánh số dòng L0001:):", *sections]
        if missing:
            parts.append("Không tìm thấy: " + ", ".join(missing))
        if deferred:
            parts.append("Chưa gửi vì vượt ngân sách của lượt này, xin lại sau nếu cần: " + ", ".join(deferred))
        parts.append("Nếu còn cần thêm file khác, tiếp tục viết khối xem_file. Nếu đã đủ thông tin, hãy xuất câu trả lời kèm bản vá theo chuẩn SEARCH/REPLACE.")
        summary = "; ".join(done + ([f"không có: {', '.join(missing)}"] if missing else [])) or "không có nội dung nào"
        return "\n\n".join(parts), summary

    async def ask_with_files(self, role_id: str, system_messages: List[str], user_text: str, *, context: str = "", **call_kwargs: Any) -> Dict[str, Any]:
        """Như ask_role. Nếu câu trả lời xin xem file hoặc tìm mã thì gửi nội dung ở lượt kế tiếp, tối đa FILE_REQUEST_MAX_ROUNDS lượt.
        Lịch sử riêng chỉ ghi ghi chú về những gì đã xem, không ghi nguyên nội dung file."""
        attachments = call_kwargs.pop("attachments", None)
        allow_rotate = call_kwargs.pop("allow_rotate", False)
        # Toàn bộ các model (ChatGPT, DeepSeek, Dola) đều dùng vòng xem_file tự động từ đĩa
        use_file_requests = True
        resp = await self.ask_role(role_id, system_messages, user_text, context=context, attachments=attachments, allow_rotate=allow_rotate, **call_kwargs)
        msg = resp.get("choices", [{}])[0].get("message", {})
        for _ in range(FILE_REQUEST_MAX_ROUNDS if use_file_requests else 0):
            text = msg.get("content") or ""
            _, paths, keywords = parse_file_requests(text)
            if not paths and not keywords:
                break
            self.remember(role_id, "assistant", text, msg.get("reasoning_content"))
            follow_up, summary = self.read_requested_files(paths, keywords)
            self.add_log(role_id, f"📂 Xem mã nguồn: {summary}")
            resp = await self.ask_role(role_id, system_messages, follow_up, remember_user=False, timeout=FILE_FOLLOWUP_TIMEOUT, **call_kwargs)
            self.remember(role_id, "user", f"[Công xưởng gửi kết quả xem mã nguồn] {summary}")
            msg = resp.get("choices", [{}])[0].get("message", {})
        failures = resp.get("download_failures") or []
        for attempt in range(DOWNLOAD_REASK_MAX):
            if not failures:
                break
            names = ", ".join(failures)
            self.add_log(role_id, f"🔁 Chưa tải được {names}: nhờ {role_id} gửi lại với tên mới (lần {attempt + 1})")
            again = await self.ask_role(role_id, system_messages, DOWNLOAD_REASK.format(names=names), **call_kwargs)
            resp["downloaded_files"] = list(dict.fromkeys((resp.get("downloaded_files") or []) + (again.get("downloaded_files") or [])))
            failures = again.get("download_failures") or []
        if failures:
            self.add_log(role_id, f"⚠️ Vẫn chưa tải được {', '.join(failures)} sau {DOWNLOAD_REASK_MAX} lần nhờ gửi lại.")
        content = msg.get("content") or ""
        if use_file_requests and FILE_BLOCK_RE.search(content):
            cleaned, paths, keywords = parse_file_requests(content)
            if paths or keywords:
                cleaned = f"{cleaned}\n\n⚠️ Giám đốc còn muốn xem thêm file; anh nhắn tiếp để tiếp tục.".strip()
            msg["content"] = cleaned
        if role_id == "director" and resp.get("downloaded_files"):
            self.note_director_delivery()
        return resp

    # ---- Phòng họp: các cố vấn tranh luận trong phòng chung, bỏ phiếu theo phương án đang xét ----
    def begin_meeting(self, topic: str, max_rounds: Optional[int] = None) -> None:
        """Mở phòng họp (chưa gọi model). Phần tranh luận chạy ở run_meeting_rounds."""
        if self.meeting and self.meeting.get("status") == "dang_hop":
            raise ValueError("Phòng họp đang diễn ra.")
        rounds = max(MEETING_MIN_ROUNDS, min(int(max_rounds or MEETING_DEFAULT_ROUNDS), MEETING_MAX_ROUNDS))
        topic = topic.strip()
        self.meeting = {
            "topic": topic,
            "max_rounds": rounds,
            "round": 0,
            "status": "dang_hop",
            "candidate": "",
            "open_points": [],
            "score": 0.0,
            "need": len(MEETING_VOTERS) * MEETING_CONSENSUS_RATIO,
            "votes_history": [],
            "room": [],
            "room_summary": "",
            "summarized_upto": 0,
        }
        self.council_messages.append({
            "speaker": "president",
            "name": "Chủ tịch (Anh)",
            "avatar": "👑",
            "role_title": "Chủ tịch Hội đồng",
            "content": topic,
            "target": "all",
            "timestamp": time.strftime("%H:%M:%S"),
        })
        for role_id in ("director",) + MEETING_VOTERS:
            self.remember(role_id, "user", f"Chủ tịch đặt vấn đề trong phòng họp: {topic}", room=True)
        self.add_log("council", f"🏛️ Phòng họp bắt đầu: tối đa {rounds} vòng. Vấn đề: {topic[:80]}")

    def _meeting_public(self) -> Optional[Dict[str, Any]]:
        if not self.meeting:
            return None
        public = {k: v for k, v in self.meeting.items() if k != "room"}
        public["room_count"] = len(self.meeting["room"])
        return public

    def _room_text(self) -> str:
        meeting = self.meeting or {}
        lines: List[str] = []
        if meeting.get("room_summary"):
            lines.append(f"[Tóm tắt các vòng trước]\n{meeting['room_summary']}")
        for e in meeting.get("room", [])[meeting.get("summarized_upto", 0):]:
            label = f" · phiếu: {e['vote_label']}" if e.get("vote_label") else ""
            lines.append(f"[Vòng {e['round']} · {e['name']}{label}] {e['content']}")
        return "\n\n".join(lines) if lines else "(Phòng họp chưa có phát biểu.)"

    def _memory_brief(self, role_id: str, max_chars: int = MEETING_OWN_MEMORY_LIMIT) -> str:
        """Lịch sử riêng của vai trò cho một lượt tranh luận: tóm tắt cũ và các tin gần nhất vừa khung."""
        m = self.memory_of(role_id)
        parts: List[str] = []
        used = len(m["summary"])
        if m["summary"]:
            parts.append(f"[Tóm tắt trước đó]\n{m['summary']}")
        recent: List[str] = []
        for x in reversed(m["messages"]):
            text = _with_reasoning(x)
            if used + len(text) > max_chars:
                break
            used += len(text)
            who = "Chủ tịch" if x["role"] == "user" else "Bạn"
            recent.append(f"[{who}] {text}")
        parts.extend(reversed(recent))
        return "\n\n".join(parts)

    def _room_entry(self, round_no: int, role_id: str, kind: str, vote: Optional[str], content: str) -> None:
        persona = COUNCIL_PERSONAS[role_id]
        timestamp = time.strftime("%H:%M:%S")
        label = MEETING_VOTE_LABELS.get(vote, "") if vote else ""
        self.meeting["room"].append({
            "round": round_no,
            "role": role_id,
            "name": persona["name"],
            "kind": kind,
            "vote": vote,
            "vote_label": label,
            "content": content,
            "timestamp": timestamp,
        })
        shown = f"[Phiếu: {label}] {content}" if label else content
        self.council_messages.append({
            "speaker": role_id,
            "name": persona["name"],
            "avatar": persona["avatar"],
            "role_title": persona["role_title"],
            "content": shown,
            "timestamp": timestamp,
        })

    async def _compact_room(self) -> None:
        """Phòng họp dài quá thì tóm tắt các vòng cũ, giữ nguyên hai vòng gần nhất."""
        meeting = self.meeting
        if len(self._room_text()) <= MEETING_ROOM_LIMIT:
            return
        room = meeting["room"]
        cut = len(room)
        for i, e in enumerate(room):
            if e["round"] >= meeting["round"] - 1:
                cut = i
                break
        old = room[meeting["summarized_upto"]:cut]
        if not old:
            return
        transcript = "\n".join(f"[Vòng {e['round']} · {e['name']}] {e['content']}" for e in old)
        messages = [
            {"role": "system", "content": (
                "Bạn tóm tắt diễn biến phòng họp: các phương án đã đưa ra, lập luận chính, ai đồng ý hoặc "
                "phản đối và vì sao, điểm nào còn bất đồng. Viết tiếng Việt, tối đa 1200 từ."
            )},
            {"role": "user", "content": f"Tóm tắt trước đó:\n{meeting['room_summary'] or '(chưa có)'}\n\nDiễn biến mới cần gộp:\n{transcript}"},
        ]
        resp = await call_bridge_llm(messages, stateless=True, timeout=MEETING_CALL_TIMEOUT, **role_llm_kwargs("director"))
        summary = (resp.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
        if not summary:
            raise RuntimeError("không nhận được bản tóm tắt phòng họp")
        meeting["room_summary"] = summary
        meeting["summarized_upto"] = cut
        self.add_log("council", f"🧠 Đã tóm tắt {len(old)} lượt phát biểu cũ trong phòng họp")

    async def _coordinator_turn(self, round_no: int) -> None:
        """Giám đốc điều phối: nêu phương án đang xét từ diễn biến phòng họp. Không bỏ phiếu."""
        meeting = self.meeting
        persona = COUNCIL_PERSONAS["director"]
        self.active_speaker = "director"
        self.council_thinking = f"{persona['avatar']} {persona['name']} đang cập nhật phương án..."
        prev = meeting["candidate"]
        messages = [{"role": "system", "content": persona["system_prompt"] + "\n\n" + MEETING_COORDINATOR_RULES}]
        brief = self._memory_brief("director")
        if brief:
            messages.append({"role": "system", "content": f"Lịch sử riêng của bạn:\n{brief}"})
        intro = f"Vấn đề: {meeting['topic']}\n"
        if prev:
            intro += f"Phương án đang xét trước đó: {prev}\n"
        messages.append({"role": "user", "content": (
            f"{intro}\nPhòng họp tới hiện tại:\n{self._room_text()}\n\n"
            "Hãy đề xuất phương án cập nhật theo JSON như đã hướng dẫn."
        )})
        resp = await call_bridge_llm(messages, stateless=True, json_mode=True, timeout=MEETING_CALL_TIMEOUT, **role_llm_kwargs("director"))
        raw = resp.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        data = _parse_dict(raw)
        candidate = str(data.get("phuong_an") or prev or meeting["topic"]).strip()
        points = data.get("diem_chua_dong_thuan")
        open_points = [str(x).strip() for x in points if str(x).strip()] if isinstance(points, list) else []
        meeting["candidate"] = candidate
        meeting["open_points"] = open_points
        text = f"Phương án đang xét: {candidate}"
        if open_points:
            text += "\nĐiểm còn bất đồng: " + "; ".join(open_points)
        self._room_entry(round_no, "director", "dieu_phoi", None, text)
        self.remember("director", "assistant", text, room=True)
        self.active_speaker = None
        self.council_thinking = None

    async def _vote_turn(self, role_id: str, round_no: int, snapshot: str) -> Optional[str]:
        """Một cố vấn phát biểu và bỏ phiếu theo phương án đang xét. Mọi cố vấn trong vòng nhìn cùng một diễn biến."""
        meeting = self.meeting
        persona = COUNCIL_PERSONAS[role_id]
        self.active_speaker = role_id
        self.council_thinking = f"{persona['avatar']} {persona['name']} đang phát biểu và bỏ phiếu..."
        messages = [{"role": "system", "content": persona["system_prompt"] + "\n\n" + MEETING_RULES}]
        brief = self._memory_brief(role_id)
        if brief:
            messages.append({"role": "system", "content": f"Lịch sử riêng của bạn:\n{brief}"})
        messages.append({"role": "user", "content": (
            f"Vấn đề: {meeting['topic']}\n"
            f"Phương án đang xét (vòng {round_no}): {meeting['candidate']}\n\n"
            f"Phòng họp tới hiện tại:\n{snapshot}\n\n"
            "Hãy bỏ phiếu cho phương án đang xét và phát biểu theo JSON như đã hướng dẫn."
        )})
        resp = await call_bridge_llm(messages, stateless=True, json_mode=True, timeout=MEETING_CALL_TIMEOUT, **role_llm_kwargs(role_id))
        raw = resp.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        data = _parse_dict(raw)
        vote = data.get("phieu")
        vote = vote if isinstance(vote, str) and vote in MEETING_VOTE_WEIGHT else None
        content = str(data.get("noi_dung") or "").strip() or raw.strip()[:1500]
        if data.get("dieu_kien"):
            content += f"\nĐiều kiện: {str(data['dieu_kien']).strip()}"
        reasoning = str(data.get("tom_tat_lap_luan") or "").strip() or None
        self._room_entry(round_no, role_id, "phieu", vote, content)
        self.remember(role_id, "assistant", content, reasoning, room=True)
        self.active_speaker = None
        self.council_thinking = None
        return vote

    def _finish_meeting(self, status: str) -> None:
        meeting = self.meeting
        meeting["status"] = status
        label = MEETING_STATUS_LABELS[status]
        text = f"🏛️ Phòng họp kết thúc: {label}.\nPhương án đang xét: {meeting['candidate'] or '(chưa có)'}"
        if meeting["open_points"]:
            text += "\nĐiểm còn bất đồng: " + "; ".join(meeting["open_points"])
        if status == "gian_doan":
            text += "\nCác vòng đã diễn ra vẫn được giữ lại trong biên bản."
        else:
            text += "\nAnh viết chỉ đạo chốt vào ô chat rồi bấm Chốt phương án."
        persona = COUNCIL_PERSONAS["director"]
        self.council_messages.append({
            "speaker": "director",
            "name": persona["name"],
            "avatar": persona["avatar"],
            "role_title": persona["role_title"],
            "content": text,
            "timestamp": time.strftime("%H:%M:%S"),
        })
        self.add_log("council", f"🏛️ Phòng họp kết thúc: {label}")

    def advisor_views_text(self) -> str:
        """Ý kiến gần nhất của từng cố vấn trong phòng họp. Chỉ để Giám đốc tham khảo: không gửi lại cho cố vấn và không phải quyết định."""
        latest: Dict[str, str] = {}
        for entry in (self.meeting or {}).get("room", []):
            if entry.get("role") in ("advocate", "critic", "arbiter") and entry.get("content"):
                label = f" ({entry['vote_label']})" if entry.get("vote_label") else ""
                latest[entry["role"]] = f"{entry['content']}{label}"
        if not latest:
            return "Cố vấn chưa có ý kiến nào trong phòng họp."
        return "\n".join(f"- {COUNCIL_PERSONAS[r]['name']}: {text}" for r, text in latest.items())

    async def director_meeting_opinion(self) -> None:
        """Sau phòng họp, Giám đốc nêu ý kiến chốt trong khung chính. Ý kiến xét đủ các góc nhìn của cố vấn để tránh một chiều; cố vấn không nhận lại phần này."""
        persona = COUNCIL_PERSONAS["director"]
        candidate = (self.meeting or {}).get("candidate") or "(chưa có)"
        prompt = (
            f"Phòng họp vừa kết thúc. Phương án đang xét: {candidate}\n\n"
            f"Góc nhìn riêng của từng cố vấn (để anh tham khảo, không phải quyết định):\n{self.advisor_views_text()}\n\n"
            "Với từng cố vấn (Ủng hộ, Phản biện, Trung lập), ghi rõ anh ĐỒNG THUẬN hay PHẢN BIỆN, kèm lý do ngắn gọn; không bỏ qua phản biện. "
            "Sau đó ghi mục 'Ý kiến cuối cùng (chờ Chủ tịch duyệt)': phương án nên chọn, rủi ro, và điều còn chưa chắc. "
            "Trả lời bằng tiếng Việt, gọn, không quả quyết khi chưa đủ căn cứ."
        )
        self.active_speaker = "director"
        try:
            resp = await self.ask_role("director", [persona["system_prompt"]], prompt, **role_llm_kwargs("director"))
            msg = resp.get("choices", [{}])[0].get("message", {})
            text = msg.get("content") or "Không có ý kiến."
            self.remember("director", "assistant", text, msg.get("reasoning_content"))
        except Exception as err:
            text = f"⚠️ [Thông báo kết nối Bridge :{BRIDGE_PORT}]: {err}"
        finally:
            self.active_speaker = None
        self.council_messages.append({
            "speaker": "director",
            "name": persona["name"],
            "avatar": persona["avatar"],
            "role_title": persona["role_title"],
            "content": text,
            "timestamp": time.strftime("%H:%M:%S"),
        })

    async def run_meeting_and_opinion(self) -> None:
        """Chạy phòng họp, rồi Giám đốc nêu ý kiến chốt trong khung chính."""
        await self.run_meeting_rounds()
        await self.director_meeting_opinion()

    async def run_meeting_rounds(self) -> None:
        """Chạy phòng họp. Vòng 0: đề xuất ban đầu. Mỗi vòng: các cố vấn bỏ phiếu, rồi Giám đốc cập nhật phương án."""
        meeting = self.meeting
        try:
            await self._coordinator_turn(0)
            for k in range(1, meeting["max_rounds"] + 1):
                meeting["round"] = k
                await self._compact_room()
                snapshot = self._room_text()
                votes: List[Optional[str]] = []
                for role_id in MEETING_VOTERS:
                    votes.append(await self._vote_turn(role_id, k, snapshot))
                if all(v is None for v in votes):
                    raise RuntimeError("các cố vấn không trả về phiếu hợp lệ")
                meeting["votes_history"].append(votes)
                score = sum(MEETING_VOTE_WEIGHT.get(v, 0.0) for v in votes if v)
                meeting["score"] = score
                self.add_log("council", f"🗳️ Vòng {k}: đồng thuận {score:g}/{len(MEETING_VOTERS)} (cần {meeting['need']:.1f})")
                if score >= meeting["need"]:
                    self._finish_meeting("dong_thuan")
                    return
                hist = meeting["votes_history"]
                if len(hist) >= 3 and hist[-1] == hist[-2] == hist[-3]:
                    self._finish_meeting("bi_te")
                    return
                if k == meeting["max_rounds"]:
                    self._finish_meeting("het_vong")
                    return
                await self._coordinator_turn(k)
        except Exception as err:
            self.add_log("system", f"⚠️ Phòng họp bị gián đoạn: {err}")
            self._finish_meeting("gian_doan")
        finally:
            self.active_speaker = None
            self.council_thinking = None

    def pending_decision_context(self) -> str:
        """Biên bản đang chờ Chủ tịch duyệt, đưa cho Giám đốc khi Chủ tịch nhắn riêng để điều chỉnh."""
        if not (self.awaiting_approval and self.decision_record):
            return ""
        rec = self.decision_record
        decisions = "\n".join(f"- {d['text']} ({d['source']})" for d in rec["decisions"])
        rejected = "\n".join(f"- {x.get('option', '')} (đề xuất: {x.get('proposed_by', '')}; lý do: {x.get('reason', '')})" for x in rec["rejected_options"]) or "- (không có)"
        return (
            "Biên bản đang chờ Chủ tịch duyệt (đã chốt, chưa triển khai):\n"
            f"Quyết định:\n{decisions}\n"
            f"Phương án bị loại:\n{rejected}\n"
            "Chủ tịch đang nhắn riêng để điều chỉnh. Nếu đồng ý điều chỉnh, hãy nói rõ nội dung cần đổi "
            "để Chủ tịch chốt lại với chỉ đạo mới."
        )

    async def run_council_round(self, user_idea: str, target_agent: str = "assistant") -> List[Dict[str, Any]]:
        """Kênh trao đổi chính của Chủ tịch với Trợ lý Điều phối (hoặc Hội đồng/Giám đốc)."""
        if self.handle_president_calls(user_idea):
            return []
        user_idea = strip_role_codes(user_idea, self.role_codes) or user_idea  # mã gọi Giám đốc thì bỏ mã, gửi phần nội dung như tin thường
        self.council_messages.append({
            "speaker": "president",
            "name": "Chủ tịch (Anh)",
            "avatar": "👑",
            "role_title": "Chủ tịch Hội đồng",
            "content": user_idea,
            "target": target_agent,
            "timestamp": time.strftime("%H:%M:%S"),
        })
        self.add_log("council", f"👑 Chủ tịch đưa ra ý kiến: {user_idea[:80]}...")

        if target_agent == "all":
            speakers = ["advocate", "critic", "arbiter"]
        elif target_agent in COUNCIL_PERSONAS:
            speakers = [target_agent]
        else:
            speakers = ["assistant"]

        new_replies = []
        director_reply = ""
        try:
            for persona_key in speakers:
                persona = COUNCIL_PERSONAS[persona_key]
                self.active_speaker = persona_key
                self.council_thinking = f"{persona['avatar']} {persona['name']} đang suy nghĩ và chuẩn bị phản hồi..."
                self.notify_event("status_changed", {
                    "active_speaker": self.active_speaker,
                    "council_thinking": self.council_thinking,
                    "director_state": self.director_state,
                    "subagent_states": self.subagent_states,
                })
                self.add_log("council", f"{persona['avatar']} {persona['name']} đang phát biểu...")

                code_ctx = self.build_existing_codebase_context(max_chars=6000)
                if code_ctx and persona_key == "director":
                    code_ctx += "\n(Toàn bộ file trên đã có sẵn trên đĩa máy chủ. Khi cần đọc nội dung chi tiết file nào, hãy xuất khối xem_file chứa đường dẫn file; Công xưởng sẽ đọc từ đĩa và gửi lại kèm số dòng L0001: ngay lập tức.)"
                reply_text = None
                reasoning_text = None
                try:
                    director_turn = persona_key == "director"
                    assistant_turn = persona_key == "assistant"
                    ask = self.ask_with_files if director_turn else self.ask_role
                    turn_kwargs = role_llm_kwargs(persona_key)
                    if director_turn and turn_kwargs.get("backend") == "chatgpt":
                        turn_kwargs.update({"allow_rotate": True})

                    system_prompts = [persona["system_prompt"], REASONING_INSTRUCTION]
                    if assistant_turn:
                        system_prompts.append(ASSISTANT_INSTRUCTION)
                    elif director_turn:
                        system_prompts.extend([DELEGATION_INSTRUCTION, DISPATCH_INSTRUCTION, file_access_instruction(turn_kwargs.get("backend"))])

                    resp = await ask(
                        persona_key,
                        system_prompts,
                        f"Chủ tịch nói: {user_idea}",
                        context="\n\n".join(
                            part for part in (
                                f"Mã nguồn hiện có của dự án:\n{code_ctx}" if code_ctx and director_turn else "",
                                self.pending_decision_context() if director_turn else "",
                            ) if part
                        ),
                        **turn_kwargs,
                    )
                    reply_msg = resp.get("choices", [{}])[0].get("message", {})
                    reply_text = reply_msg.get("content", "Không có phản hồi.")
                    if director_turn:
                        director_reply = reply_text
                    reasoning_text = reply_msg.get("reasoning_content")

                    if persona_key == "assistant":
                        clean_reply, cmd = parse_assistant_command(reply_text)
                        if cmd:
                            action = cmd.get("action")
                            if action == "bat_hoi_dong":
                                topic = cmd.get("topic") or user_idea
                                self.add_log("assistant", f"🏛️ Trợ lý triệu tập Hội đồng 3 Cố vấn họp về: {topic[:80]}")
                                self.council_messages.append({
                                    "speaker": "system",
                                    "name": "Điều phối Hệ thống",
                                    "avatar": "🏛️",
                                    "role_title": "Triệu tập Hội đồng theo lệnh của Trợ lý",
                                    "content": f"🏛️ **[Hội đồng 3 Cố vấn]** đã được Trợ lý triệu tập theo chỉ đạo của Chủ tịch để mổ xẻ chủ đề: **{topic}**",
                                    "timestamp": time.strftime("%H:%M:%S"),
                                })
                                for adv in ["advocate", "critic", "arbiter"]:
                                    adv_persona = COUNCIL_PERSONAS[adv]
                                    self.active_speaker = adv
                                    self.council_thinking = f"{adv_persona['avatar']} {adv_persona['name']} đang phát biểu..."
                                    self.notify_event("status_changed", {"active_speaker": adv, "council_thinking": self.council_thinking})
                                    adv_resp = await self.ask_role(adv, [adv_persona["system_prompt"], REASONING_INSTRUCTION], f"Chủ tịch và Trợ lý xin ý kiến cố vấn về: {topic}")
                                    adv_msg = adv_resp.get("choices", [{}])[0].get("message", {})
                                    adv_text = adv_msg.get("content", "")
                                    if adv_text:
                                        adv_obj = {
                                            "speaker": adv,
                                            "name": adv_persona["name"],
                                            "avatar": adv_persona["avatar"],
                                            "role_title": adv_persona["role_title"],
                                            "content": adv_text,
                                            "timestamp": time.strftime("%H:%M:%S"),
                                        }
                                        if adv_msg.get("reasoning_content"):
                                            adv_obj["reasoning"] = adv_msg.get("reasoning_content")
                                        self.council_messages.append(adv_obj)
                                        self.notify_event("message", adv_obj)
                                reply_text = clean_reply or f"Dạ thưa Chủ tịch, em đã triệu tập Hội đồng 3 Cố vấn mổ xẻ về chủ đề '{topic}' ở trên. Mời Chủ tịch xem xét các góc nhìn phản biện ạ."
                            elif action == "giao_giam_doc":
                                brief = cmd.get("brief") or cmd.get("task") or user_idea
                                self.add_log("assistant", "👩‍💼 Trợ lý chuyển giao yêu cầu sang Giám đốc Kỹ thuật")
                                if clean_reply:
                                    init_obj = {
                                        "speaker": "assistant",
                                        "name": persona["name"],
                                        "avatar": persona["avatar"],
                                        "role_title": persona["role_title"],
                                        "content": clean_reply,
                                        "timestamp": time.strftime("%H:%M:%S"),
                                    }
                                    if reasoning_text:
                                        init_obj["reasoning"] = reasoning_text
                                    self.council_messages.append(init_obj)
                                    self.notify_event("message", init_obj)

                                bridge_msg = {
                                    "speaker": "system",
                                    "name": "Cầu Nối Liên Phiên (Session Bridge)",
                                    "avatar": "🌉",
                                    "role_title": "Chuyển giao Ngữ cảnh: Trợ lý ➔ Giám đốc",
                                    "content": f"🌉 **[Cầu Nối Liên Phiên]** Đã chuyển giao bản đặc tả từ Trợ lý sang phiên làm việc của Giám đốc Kỹ thuật:\n> *{brief}*",
                                    "timestamp": time.strftime("%H:%M:%S"),
                                }
                                self.council_messages.append(bridge_msg)
                                self.notify_event("message", bridge_msg)

                                director_replies = await self.execute_director_brief(brief)
                                director_summary = director_replies[-1].get("content", "") if director_replies else ""
                                self.active_speaker = "assistant"
                                self.council_thinking = "👩‍💼 Trợ lý đang tổng hợp báo cáo điều hành trình Chủ tịch..."
                                self.notify_event("status_changed", {"active_speaker": "assistant", "council_thinking": self.council_thinking})
                                followup_resp = await self.ask_role(
                                    "assistant",
                                    [persona["system_prompt"], REASONING_INSTRUCTION],
                                    f"Giám đốc Kỹ thuật vừa hoàn thành triển khai và báo cáo:\n{director_summary}\n\nHãy viết một bản báo cáo điều hành ngắn gọn, lịch thiệp trình Chủ tịch."
                                )
                                f_msg = followup_resp.get("choices", [{}])[0].get("message", {})
                                exec_summary = f_msg.get("content", "")
                                reply_text = f"📋 **Báo cáo Điều phối từ Trợ lý:**\n{exec_summary}" if exec_summary else "Dạ thưa Chủ tịch, Giám đốc Kỹ thuật và Kỹ sư đã triển khai xong yêu cầu trên đĩa ạ."
                                reasoning_text = f_msg.get("reasoning_content")
                        else:
                            reply_text = clean_reply
                    elif persona_key == "director":
                        reply_text, delegations = parse_delegations(reply_text)
                        if delegations:
                            reply_text, reasoning_text = await self.report_after_delegation(reply_text, delegations)
                        reply_text = strip_role_codes(reply_text, self.role_codes)
                        # TỰ ĐỘNG LÀM SẠCH VÀ VÁ MÃ NGUỒN VÀO ĐĨA (NATIVE AI HARNESS)
                        ws = getattr(self, "build_workspace", os.path.join(self.project_dir, "build_workspace"))
                        if not os.path.exists(ws):
                            ws = os.path.join(self.project_dir, "merged_source")
                        if os.path.exists(ws) and reply_text:
                            try:
                                h_res = workspace_harness.extract_and_apply_harness_edits(ws, reply_text)
                                if h_res.get("applied_count", 0) > 0:
                                    applied_files = [x.get("file") for x in h_res.get("applied", []) if x.get("file")]
                                    files_str = ", ".join(applied_files[:4])
                                    if len(applied_files) > 4:
                                        files_str += f" và {len(applied_files) - 4} file"
                                    self.add_log("harness", f"⚡ [Tự động vá đĩa] Đã áp dụng {h_res['applied_count']} thay đổi vào: {files_str}")
                                    self.rebuild_merged_files()
                                    self.council_messages.append({
                                        "speaker": "system",
                                        "name": "Local Workspace Harness",
                                        "avatar": "🌳",
                                        "role_title": "Tự động cập nhật mã nguồn",
                                        "content": f"⚡ **[Harness Auto-Apply]** Đã tự động bóc tách và áp dụng thành công **{h_res['applied_count']}** bản vá vào workspace trên đĩa:\n" + "\n".join(f"- 📄 `{f}`" for f in applied_files),
                                        "timestamp": time.strftime("%H:%M:%S"),
                                    })
                            except Exception as e:
                                self.add_log("harness", f"⚠️ [Harness Auto-Apply] Lỗi: {e}")
                        applied_cnt = h_res.get("applied_count", 0) if "h_res" in locals() and isinstance(h_res, dict) else 0
                        self.note_director_turn(edited_files_count=applied_cnt)
                except Exception as err:
                    error_text = f"⚠️ [Thông báo kết nối Bridge :{BRIDGE_PORT}]: {err}"
                if not reasoning_text and reply_text:
                    think_m = re.search(r"<(?:think|reasoning)>(.*?)</(?:think|reasoning)>", reply_text, re.DOTALL | re.IGNORECASE)
                    if think_m:
                        reasoning_text = think_m.group(1).strip()
                        reply_text = re.sub(r"<(?:think|reasoning)>.*?</(?:think|reasoning)>", "", reply_text, flags=re.DOTALL | re.IGNORECASE).strip()

                if reply_text is not None:
                    self.remember(persona_key, "assistant", reply_text, reasoning_text)
                msg_obj = {
                    "speaker": persona_key,
                    "name": persona["name"],
                    "avatar": persona["avatar"],
                    "role_title": persona["role_title"],
                    "content": reply_text if reply_text is not None else error_text,
                    "timestamp": time.strftime("%H:%M:%S"),
                }
                if reasoning_text:
                    msg_obj["reasoning"] = reasoning_text
                self.council_messages.append(msg_obj)
                new_replies.append(msg_obj)
                self.notify_event("message", msg_obj)
        finally:
            self.active_speaker = None
            self.council_thinking = None
            self.live_reasoning = None
            self.notify_event("status_changed", {
                "active_speaker": None,
                "council_thinking": None,
                "director_state": self.director_state,
                "subagent_states": self.subagent_states,
            })

        if director_reply:
            self.maybe_start_ship(director_reply)
        return new_replies

    async def run_subagent_task(self, role_id: str, task: str) -> str:
        """Kỹ sư nhận việc từ Giám đốc, làm trong thread riêng và báo lại. Tự động đọc file và vá vào đĩa."""
        state = self.subagent_states.setdefault(role_id, {"id": role_id, "status": "idle", "current_task": "", "files_count": 0})
        state.update({"status": "working", "current_task": task[:120]})
        self.notify_event("subagent", {"states": self.subagent_states})
        self.add_log(role_id, f"📥 Nhận việc từ Giám đốc: {task[:160]}")

        backend = role_llm_kwargs(role_id).get("backend")
        worker_persona = (
            f"Bạn là kỹ sư phần mềm chuyên trách {role_id} của công xưởng. "
            "Nhiệm vụ của bạn là đọc mã nguồn, phân tích, đánh giá kiến trúc hoặc trực tiếp viết mã sửa đổi theo đúng yêu cầu của Giám đốc. "
            "Trả lời bằng tiếng Việt rõ ràng, logic, có dẫn chứng cụ thể từ các file mã nguồn."
        )
        prompts = [
            worker_persona,
            file_access_instruction(backend),
        ]
        codebase_ctx = self.build_existing_codebase_context(max_chars=6000)
        if codebase_ctx:
            prompts.append(codebase_ctx)

        task_prompt = (
            f"Giám đốc giao việc: {task}\n\n"
            "Chỉ dẫn thực hiện:\n"
            "- Hãy kiểm tra các file liên quan trên đĩa (dùng `xem_file:` hoặc `tim_ma:` nếu cần đọc nội dung chi tiết).\n"
            "- Nếu là nhiệm vụ ĐÁNH GIÁ / RÀ SOÁT / AUDIT: Hãy phân tích sâu kiến trúc, chỉ ra ưu điểm, rủi ro, lỗi tiềm ẩn và giải pháp đề xuất để báo cáo lại cho Giám đốc.\n"
            "- Nếu là nhiệm vụ LẬP TRÌNH / SỬA CODE: Hãy đưa ra bản vá mã nguồn chuẩn xác (dùng SEARCH/REPLACE hoặc khối ghi đè file) để hệ thống tự động vá vào đĩa.\n"
            "Báo cáo ngắn gọn, rành mạch."
        )

        try:
            resp = await self.ask_with_files(
                role_id,
                prompts,
                task_prompt,
                **role_llm_kwargs(role_id),
            )
            msg = resp.get("choices", [{}])[0].get("message", {})
            reply = msg.get("content") or "Không có kết quả."
            self.remember(role_id, "assistant", reply, msg.get("reasoning_content"))

            # TỰ ĐỘNG VÁ CODE DO WORKER TẠO RA VÀO ĐĨA WORKSPACE
            ws = getattr(self, "build_workspace", os.path.join(self.project_dir, "build_workspace"))
            if not os.path.exists(ws):
                ws = os.path.join(self.project_dir, "merged_source")
            if os.path.exists(ws) and reply:
                try:
                    h_res = workspace_harness.extract_and_apply_harness_edits(ws, reply)
                    if h_res.get("applied_count", 0) > 0:
                        applied_files = [x.get("file") for x in h_res.get("applied", []) if x.get("file")]
                        state["files_count"] = state.get("files_count", 0) + h_res["applied_count"]
                        self.add_log(role_id, f"⚡ [Vá đĩa thành công] Đã cập nhật {h_res['applied_count']} file: {', '.join(applied_files[:4])}")
                        self.council_messages.append({
                            "speaker": "system",
                            "name": f"Kỹ sư {role_id}",
                            "avatar": "🛠️",
                            "role_title": "Tự động cập nhật mã nguồn",
                            "content": f"🛠️ **[Kỹ sư {role_id}]** đã sửa và đồng bộ trực tiếp vào đĩa ({h_res['applied_count']} file):\n" + "\n".join(f"- 📄 `{f}`" for f in applied_files),
                            "timestamp": time.strftime("%H:%M:%S"),
                        })
                except Exception as e:
                    self.add_log(role_id, f"⚠️ [Vá đĩa lỗi]: {e}")

            self.add_log(role_id, f"✅ Báo cáo cho Giám đốc: {reply[:160]}")
        except Exception as err:
            reply = f"Không hoàn thành: {err}"
            self.add_log(role_id, f"⚠️ {reply[:200]}")
        finally:
            state.update({"status": "idle", "current_task": ""})
            self.notify_event("subagent", {"states": self.subagent_states})
        return reply

    async def report_after_delegation(self, first_text: str, delegations: list) -> tuple:
        """Giao việc cho kỹ sư, chờ kết quả, rồi để Giám đốc tổng hợp báo cáo cho Chủ tịch."""
        self.add_log("director", f"👔 Giám đốc giao {len(delegations)} việc cho kỹ sư")
        self.council_messages.append({
            "speaker": "system",
            "name": "Người Giao liên (Courier / Shipper)",
            "avatar": "🚚",
            "role_title": "Điều phối tin nhắn & Phân công",
            "content": f"🚚 **[Người Giao liên]** Đã chuyển giao **{len(delegations)}** nhiệm vụ từ Giám đốc Kỹ thuật tới các Kỹ sư chuyên trách:\n"
            + "\n".join(f"- **{d['to']}**: {d['task']}" for d in delegations),
            "timestamp": time.strftime("%H:%M:%S"),
        })

        results = await asyncio.gather(*(self.run_subagent_task(d["to"], d["task"]) for d in delegations))
        
        self.council_messages.append({
            "speaker": "system",
            "name": "Người Giao liên (Courier / Shipper)",
            "avatar": "📨",
            "role_title": "Thu thập báo cáo Kỹ sư",
            "content": f"📨 **[Người Giao liên]** Đã thu thập đủ báo cáo phản hồi từ **{len(results)}** Kỹ sư và chuyển về cho Giám đốc Kỹ thuật tổng hợp trình Chủ tịch.",
            "timestamp": time.strftime("%H:%M:%S"),
        })

        lines = "\n\n".join(f"=== BÁO CÁO CỦA {d['to'].upper()} ({d['task']}) ===\n{r}" for d, r in zip(delegations, results))
        director_prompt = (
            f"Dưới đây là kết quả và báo cáo chuyên sâu từ các Kỹ sư chuyên trách:\n\n{lines}\n\n"
            "Hãy đọc kỹ toàn bộ kết quả trên và tổng hợp một báo cáo hoàn chỉnh gửi cho Chủ tịch: "
            "nêu rõ tình trạng từng phần, các phát hiện quan trọng (nếu đánh giá dự án), việc nào đã xong, việc nào còn tồn đọng và đề xuất các bước tiếp theo."
        )
        try:
            resp = await self.ask_with_files(
                "director",
                [COUNCIL_PERSONAS["director"]["system_prompt"], file_access_instruction(role_llm_kwargs("director").get("backend"))],
                director_prompt,
                **role_llm_kwargs("director"),
            )
        except Exception as err:
            return f"{first_text}\n\n⚠️ Chưa tổng hợp được kết quả từ kỹ sư: {err}", None
        msg = resp.get("choices", [{}])[0].get("message", {})
        return msg.get("content") or first_text, msg.get("reasoning_content")

    async def execute_director_brief(self, brief: str) -> List[Dict[str, Any]]:
        """Cầu nối liên phiên: Giám đốc nhận brief từ Trợ lý, tự code hoặc phân công Kỹ sư Worker."""
        persona = COUNCIL_PERSONAS["director"]
        self.active_speaker = "director"
        self.council_thinking = "👔 Giám đốc Kỹ thuật đang phân tích kiến trúc & chuẩn bị phân công..."
        self.notify_event("status_changed", {"active_speaker": "director", "council_thinking": self.council_thinking})
        self.add_log("director", f"👔 Giám đốc Kỹ thuật tiếp nhận brief: {brief[:80]}...")

        code_ctx = self.build_existing_codebase_context(max_chars=6000)
        turn_kwargs = role_llm_kwargs("director")
        if turn_kwargs.get("backend") == "chatgpt":
            turn_kwargs.update({"allow_rotate": True})

        system_prompts = [
            persona["system_prompt"],
            REASONING_INSTRUCTION,
            DELEGATION_INSTRUCTION,
            DISPATCH_INSTRUCTION,
            file_access_instruction(turn_kwargs.get("backend")),
        ]
        director_prompt = f"Trợ lý Điều phối chuyển giao yêu cầu của Chủ tịch:\n{brief}"
        resp = await self.ask_with_files(
            "director",
            system_prompts,
            director_prompt,
            context=f"Mã nguồn hiện có của dự án:\n{code_ctx}" if code_ctx else "",
            **turn_kwargs,
        )
        reply_msg = resp.get("choices", [{}])[0].get("message", {})
        reply_text = reply_msg.get("content", "Không có phản hồi.")
        reasoning_text = reply_msg.get("reasoning_content")

        # Kiểm tra xem Giám đốc có giao việc cho các Worker không
        reply_text, delegations = parse_delegations(reply_text)
        if delegations:
            self.add_log("director", f"👔 Giám đốc phân công {len(delegations)} nhiệm vụ cho các Kỹ sư Worker")
            reply_text, reasoning_text = await self.report_after_delegation(reply_text, delegations)

        # Harness auto-apply nếu Giám đốc trực tiếp sửa code
        ws = getattr(self, "build_workspace", os.path.join(self.project_dir, "build_workspace"))
        if not os.path.exists(ws):
            ws = os.path.join(self.project_dir, "merged_source")
        if os.path.exists(ws) and reply_text:
            try:
                h_res = workspace_harness.extract_and_apply_harness_edits(ws, reply_text)
                if h_res.get("applied_count", 0) > 0:
                    applied_files = [x.get("file") for x in h_res.get("applied", []) if x.get("file")]
                    self.add_log("harness", f"⚡ [Vá đĩa] Giám đốc đã cập nhật {h_res['applied_count']} file: {', '.join(applied_files[:4])}")
                    self.rebuild_merged_files()
            except Exception as e:
                self.add_log("harness", f"⚠️ [Harness Auto-Apply] Lỗi: {e}")

        msg_obj = {
            "speaker": "director",
            "name": persona["name"],
            "avatar": persona["avatar"],
            "role_title": persona["role_title"],
            "content": reply_text,
            "timestamp": time.strftime("%H:%M:%S"),
        }
        if reasoning_text:
            msg_obj["reasoning"] = reasoning_text
        self.council_messages.append(msg_obj)
        self.notify_event("message", msg_obj)
        return [msg_obj]

    async def build_meeting_record(self) -> List[str]:
        """Gom lịch sử của tất cả thành viên đã tham gia (cả hai phía), mỗi người một phần.
        Ai có lịch sử dài thì tóm tắt phần cũ trước, rồi mới đưa vào biên bản."""
        record: List[str] = []
        final_words = [m for m in self.council_messages if m.get("speaker") == "president" and m.get("target") is None]
        if final_words:
            record.append("=== Chỉ đạo của Chủ tịch ===\n" + "\n".join(f"[{m['timestamp']}] {m['content']}" for m in final_words))
        if self.meeting and self.meeting.get("room"):
            status = MEETING_STATUS_LABELS.get(self.meeting["status"], self.meeting["status"])
            record.append(
                f"=== Phòng họp: {self.meeting['topic']} ===\n"
                f"Kết quả: {status}. Phương án đang xét cuối cùng: {self.meeting['candidate'] or '(chưa có)'}\n\n"
                + self._room_text()
            )
        for member in ("director", "advocate", "critic", "arbiter"):
            m = self.memory_of(member)
            if not m["messages"] and not m["summary"]:
                continue
            if self.memory_chars(member) > MEETING_FULL_LIMIT:
                await self.compact_memory(member, force=True)
            name = COUNCIL_PERSONAS[member]["name"]
            lines = []
            if m["summary"]:
                lines.append(f"[Tóm tắt phần trước của {name}]\n{m['summary']}")
            for x in m["messages"]:
                if x.get("room"):
                    continue
                who = "Chủ tịch" if x["role"] == "user" else name
                lines.append(f"[{x['timestamp']}] {who}: {x['content']}")
            record.append(f"=== Lịch sử của {name} ===\n" + "\n".join(lines))
        return record

    async def run_secretary_finalize(self, final_directive: str = "") -> Dict[str, Any]:
        """Thư ký lập Biên bản có cấu trúc, Đặc tả kỹ thuật và Roadmap từ chỉ đạo chốt của Chủ tịch.
        Chỉ lập biên bản; việc triển khai chờ Chủ tịch duyệt."""
        if not final_directive.strip():
            raise ValueError("Cần chỉ đạo chốt của Chủ tịch trước khi lập biên bản.")
        self.status = "secretary_spec"
        if final_directive.strip():
            self.council_messages.append({
                "speaker": "president",
                "name": "Chủ tịch (Chốt dự án)",
                "avatar": "👑",
                "content": final_directive.strip(),
                "timestamp": time.strftime("%H:%M:%S"),
            })

        self.add_log("secretary", "📝 [Thư ký] Đang tổng hợp Biên bản cuộc họp, viết Đặc tả Kỹ thuật và lập Roadmap...")
        code_ctx = self.build_existing_codebase_context(max_chars=10000)
        # Thư ký là người ghi biên bản nên đọc toàn bộ cuộc họp; khi quá dài thì tự tóm tắt phần cũ
        record = await self.build_meeting_record()
        messages = [{"role": "system", "content": SECRETARY_SYSTEM_PROMPT}]
        if code_ctx:
            messages.append({"role": "system", "content": f"Mã nguồn hiện có của dự án:\n{code_ctx}"})
        meeting_text = "\n\n".join(record) if record else "(Chưa có nội dung cuộc họp.)"
        messages.append({"role": "user", "content": f"Lịch sử đầy đủ của từng thành viên trong cuộc họp:\n\n{meeting_text}\n\nHãy xuất JSON Hồ sơ Dự án chuẩn từ toàn bộ cuộc họp trên."})
        resp = await call_bridge_llm(
            messages,
            session_id="secretary-main",
            **role_llm_kwargs("secretary"),
            json_mode=True,
            stateless=True,
        )
        raw_content = (
            resp.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "{}")
        )
        spec_data = parse_json_safely(raw_content)

        self.title = spec_data.get("project_name") or self.title
        directive = final_directive.strip()
        decisions = [{"text": directive, "source": "chỉ đạo chốt của Chủ tịch"}]
        for d in spec_data.get("decisions") or []:
            if isinstance(d, dict) and str(d.get("text", "")).strip() and str(d["text"]).strip() != directive:
                decisions.append({"text": str(d["text"]).strip(), "source": str(d.get("source") or "thảo luận của Hội đồng")})
        self.decision_record = {
            "directive": directive,
            "decisions": decisions,
            "rejected_options": [x for x in (spec_data.get("rejected_options") or []) if isinstance(x, dict)],
            "dissent": [x for x in (spec_data.get("dissent") or []) if isinstance(x, dict)],
            "open_questions": [str(x) for x in (spec_data.get("open_questions") or []) if str(x).strip()],
        }
        minutes_body = spec_data.get("meeting_minutes_md") or "Thư ký chưa xuất được nội dung biên bản."
        self.meeting_minutes_md = (
            "# Biên bản cuộc họp\n\n## Quyết định chốt (nguyên văn chỉ đạo của Chủ tịch)\n"
            f"{directive}\n\n{minutes_body}"
        )
        self.awaiting_approval = True
        self.tech_spec_md = spec_data.get("tech_spec_md") or "# Đặc tả kỹ thuật\nChi tiết kiến trúc hệ thống."
        self.install_command = spec_data.get("install_command") or "echo 'No external dependencies required'"
        self.test_command = spec_data.get("test_command") or "python3 test_suite.py"
        self.run_command = spec_data.get("run_command") or "python3 main.py"
        self.roadmap = spec_data.get("roadmap") or []

        # Lưu 3 tài liệu của Thư ký ra đĩa
        with open(os.path.join(self.project_dir, "MEETING_MINUTES.md"), "w", encoding="utf-8") as f:
            f.write(self.meeting_minutes_md)
        with open(os.path.join(self.project_dir, "TECH_SPEC.md"), "w", encoding="utf-8") as f:
            f.write(self.tech_spec_md)
        with open(os.path.join(self.project_dir, "ROADMAP.json"), "w", encoding="utf-8") as f:
            json.dump(self.roadmap, f, ensure_ascii=False, indent=2)

        self.add_log(
            "secretary",
            f"✅ [Thư ký] Đã hoàn thành Biên bản họp, TECH_SPEC.md và ROADMAP.json ({len(self.roadmap)} giai đoạn).",
        )
        return self.to_dict()

    async def execute_full_roadmap(self, max_heal_rounds: int = 3):
        """
        Giám đốc Điều hành chỉ đạo thực thi toàn bộ Roadmap:
        - Giao song song tối đa 5 Subagent (hoặc tự làm nếu không thể song song)
        - Worker Giao liên thu thập các gói alpha gửi về cho Giám đốc merge
        - Xuất full_source.zip kèm lệnh cài
        - Worker Thực thi tải zip, chạy cài đặt + test và gửi log về cho Giám đốc tự vá lỗi.
        """
        self.status = "executing"
        self.awaiting_approval = False
        courier = CourierWorker(self.project_dir, self.add_log)
        builder = BuilderRunnerWorker(self.project_dir, self.add_log)

        try:
            for phase_idx, phase in enumerate(self.roadmap, start=1):
                phase_name = phase.get("phase_name", f"Phase {phase_idx}")
                tasks = phase.get("tasks", [])
                phase["status"] = "in_progress"
                self.director_state = {
                    "status": "coordinating",
                    "current_action": f"Đang điều phối {phase_name} ({len(tasks)} tasks)",
                }
                self.add_log("director", f"🎯 [Giám đốc] Bắt đầu {phase_name} gồm {len(tasks)} gói công việc.")

                parallel_tasks = [t for t in tasks if t.get("parallel", True)]
                sequential_tasks = [t for t in tasks if not t.get("parallel", True)]

                # 1. Thực thi các task tuần tự do chính Giám đốc đảm nhiệm
                for seq_task in sequential_tasks:
                    await self._run_single_task(seq_task, courier, is_director_self=True)

                # 2. Thực thi song song tối đa 5 Subagents cho các task song song
                if parallel_tasks:
                    self.add_log(
                        "director",
                        f"⚡ [Giám đốc] Mở {min(len(parallel_tasks), 5)} phiên Subagent chạy song song...",
                    )
                    await asyncio.gather(
                        *(self._run_single_task(t, courier, is_director_self=False) for t in parallel_tasks)
                    )

                phase["status"] = "completed"
                self.add_log("director", f"✅ [Giám đốc] Hoàn tất {phase_name}. Đã merge {len(self.merged_files)} files.")

            # 3. Giám đốc đóng gói full_source.zip và chuyển cho Worker Thực thi
            full_zip = self._package_full_source_zip()

            # 4. Vòng lặp Kiểm thử & Tự sửa lỗi (Self-Healing Loop) giữa Builder Worker và Giám đốc
            for attempt in range(1, max_heal_rounds + 1):
                self.builder_state["status"] = "running"
                report = await builder.execute_and_report(
                    full_zip, self.install_command, self.test_command, iteration=attempt
                )
                self.builder_state["last_exit_code"] = report["exit_code"]
                self.builder_state["last_log"] = report["log"]
                self.builder_state["runs"].append(report)

                if report["passed"]:
                    self.builder_state["status"] = "passed"
                    self.director_state = {
                        "status": "completed",
                        "current_action": f"Hoàn thành xuất sắc! Build & Test PASS ở vòng #{attempt}.",
                    }
                    self.status = "completed"
                    self.add_log(
                        "director",
                        f"🏆 [Giám đốc] Toàn bộ Roadmap đã hoàn thành và kiểm thử PASS 100%! Gói full_source.zip đã sẵn sàng.",
                    )
                    return

                # Nếu FAIL -> Giám đốc đọc log lỗi và sửa
                self.add_log(
                    "director",
                    f"🔧 [Giám đốc] Nhận log lỗi từ Worker Thực thi (Vòng #{attempt}). Đang phân tích và tạo bản vá...",
                )
                self.director_state = {
                    "status": "healing",
                    "current_action": f"Đang phân tích log lỗi và sửa code (Vòng #{attempt})",
                }
                await self._director_heal_from_log(report["log"], report["exit_code"])
                full_zip = self._package_full_source_zip()

            self.builder_state["status"] = "failed"
            self.status = "completed_with_warnings"
            self.director_state = {
                "status": "completed_with_warnings",
                "current_action": f"Đã chạy {max_heal_rounds} vòng tự sửa lỗi. Xem log chi tiết.",
            }
        except Exception as e:
            self.status = "failed"
            self.director_state = {"status": "error", "current_action": str(e)}
            self.add_log("director", f"⚠️ [Giám đốc] Lỗi trong quá trình điều phối: {e}")

    async def _run_single_task(self, task: Dict[str, Any], courier: CourierWorker, is_director_self: bool):
        task_id = task.get("task_id", f"task_{uuid.uuid4().hex[:4]}")
        title = task.get("title", task_id)
        allowed_files = task.get("allowed_files", [])
        contract_spec = task.get("contract_spec", "")
        assigned = "director-main" if is_director_self else task.get("assigned_to", "subagent-1")
        if not is_director_self and assigned not in self.subagent_states:
            assigned = "subagent-1"

        task["status"] = "running"
        async with self._subagent_semaphore:
            if is_director_self:
                self.director_state = {
                    "status": "coding",
                    "current_action": f"Tự thực hiện task tuần tự: {title}",
                }
                self.add_log("director", f"👨‍💻 [Giám đốc] Tự thực hiện task tuần tự '{title}' ({', '.join(allowed_files)})")
            else:
                self.subagent_states[assigned] = {
                    "id": assigned,
                    "status": "working",
                    "current_task": f"{task_id}: {title}",
                    "files_count": len(allowed_files),
                }
                self.add_log(assigned, f"⚙️ [{assigned}] Nhận lệnh từ Giám đốc: '{title}' -> {allowed_files}")

            memory_key = "director" if is_director_self else assigned
            builder_backend = role_llm_kwargs(memory_key).get("backend")
            codebase_ctx = self.build_existing_codebase_context(max_chars=8000)
            prompt = DIRECTOR_TASK_PROMPT_TEMPLATE.format(
                output_rules=(BUILDER_OUTPUT_RULES_GPT if builder_backend == "chatgpt" else BUILDER_OUTPUT_RULES_TEXT).format(task_id=task_id),
                agent_id="Giám đốc Điều hành" if is_director_self else assigned,
                tech_spec=self.tech_spec_md + ('\n\n' + codebase_ctx if codebase_ctx else ''),
                task_id=task_id,
                task_title=title,
                allowed_files=", ".join(allowed_files),
                contract_spec=contract_spec,
            )
            dl_dir = os.path.join(DOWNLOADS_DIR, f"{self.project_id}_{assigned}")
            builder_kwargs = role_llm_kwargs(memory_key)
            # GPT nhận kèm zip mã nguồn hiện có; DeepSeek chưa nhận zip nên không đính kèm
            attachments = None
            if self.merged_files and builder_kwargs.get("backend") == "chatgpt":
                attachments = [{"path": self.write_project_snapshot_zip(), "name": "project_snapshot.zip"}]
            bridge_resp = await self.ask_role(
                memory_key,
                ["Bạn là kỹ sư phần mềm xuất sắc. Luôn trả về mã nguồn đầy đủ của các file được giao, đúng định dạng được yêu cầu trong lệnh."],
                prompt,
                remember_user=False,
                **builder_kwargs,
                json_mode=(builder_backend == "chatgpt"),
                expect_zip=True,
                download_dir=dl_dir,
                attachments=attachments,
            )

            # Worker Giao liên thu nhận kết quả và nộp về cho Giám đốc
            self.courier_state["status"] = "delivering"
            delivery = courier.collect_subagent_delivery(task_id, assigned, bridge_resp)
            self.remember(memory_key, "user", f"Nhiệm vụ {task_id} — {title}. File được giao: {', '.join(allowed_files)}. Đặc tả: {contract_spec}")
            self.remember(memory_key, "assistant", f"Đã hoàn thành {task_id}, bàn giao {len(delivery['files'])} file: {', '.join(delivery['files'].keys())[:1500]}")
            self.courier_state["deliveries"].append({
                "task_id": task_id,
                "from": assigned,
                "alpha_zip": delivery["alpha_zip_path"],
                "file_count": len(delivery["files"]),
            })
            self.courier_state["status"] = "idle"

            # Giám đốc Merge code vào kho chung và workspace trên đĩa
            ws = getattr(self, "build_workspace", os.path.join(self.project_dir, "build_workspace"))
            for rel_path, content in delivery["files"].items():
                abs_target = os.path.join(self.merged_dir, rel_path)
                if os.path.commonpath([os.path.realpath(self.merged_dir), os.path.realpath(abs_target)]) != os.path.realpath(self.merged_dir):
                    self.add_log("director", f"⚠️ [Giám đốc] Bỏ qua file thoát khỏi kho mã nguồn: {rel_path[:120]}")
                    continue
                self.merged_files[rel_path] = content
                os.makedirs(os.path.dirname(abs_target), exist_ok=True)
                with open(abs_target, "w", encoding="utf-8") as f:
                    f.write(content)
                if ws and os.path.isdir(ws):
                    ws_target = os.path.join(ws, rel_path)
                    os.makedirs(os.path.dirname(ws_target), exist_ok=True)
                    with open(ws_target, "w", encoding="utf-8") as f:
                        f.write(content)

            if not is_director_self:
                # Giao liên mang kết quả từ thread kỹ sư sang thread Giám đốc để Giám đốc xác nhận
                await self.courier_handoff_to_director(task_id, assigned, delivery)

            task["status"] = "completed"
            if not is_director_self:
                self.subagent_states[assigned]["status"] = "completed"

    def _package_full_source_zip(self) -> str:
        """Giám đốc đóng gói toàn bộ mã nguồn đã merge kèm script cài đặt thành full_source.zip."""
        install_script = (
            "#!/bin/bash\n"
            "set -e\n"
            "cd \"$(dirname \"$0\")\"\n"
            f"echo '📦 Đang cài đặt dự án: {self.title}'\n"
            f"{self.install_command}\n"
            f"echo '🧪 Đang chạy kiểm thử: {self.test_command}'\n"
            f"{self.test_command}\n"
            "echo '✅ Hoàn tất cài đặt và kiểm thử!'\n"
        )
        script_path = os.path.join(self.merged_dir, "install_and_build.sh")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(install_script)
        os.chmod(script_path, 0o755)
        self.merged_files["install_and_build.sh"] = install_script

        zip_path = os.path.join(self.project_dir, "full_source.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(self.merged_dir):
                for file in files:
                    full_p = os.path.join(root, file)
                    rel_p = os.path.relpath(full_p, self.merged_dir)
                    zf.write(full_p, rel_p)

        self.full_zip_path = zip_path
        self.add_log(
            "director",
            f"📦 [Giám đốc] Đã đóng gói 'full_source.zip' ({len(self.merged_files)} files) kèm lệnh cài đặt 'install_and_build.sh'.",
        )
        return zip_path

    async def _director_heal_from_log(self, error_log: str, exit_code: int):
        """Giám đốc đọc log lỗi từ Builder Worker và cập nhật các file bị lỗi."""
        files_summary = []
        for path, code in self.merged_files.items():
            files_summary.append(f"--- FILE: {path} ---\n{code[:2500]}")

        prompt = DIRECTOR_FIX_PROMPT_TEMPLATE.format(
            install_cmd=self.install_command,
            test_cmd=self.test_command,
            exit_code=exit_code,
            error_log=error_log[-4000:],
            current_files_summary="\n\n".join(files_summary),
        )
        resp = await self.ask_role(
            "director",
            ["Bạn là Giám đốc Kỹ thuật chuyên sửa lỗi build/runtime thông qua log thực tế."],
            prompt,
            remember_user=False,
            **role_llm_kwargs("director"),
            json_mode=True,
        )
        raw = resp.get("choices", [{}])[0].get("message", {}).get("content", "{}")
        fix_data = parse_json_safely(raw)
        diagnosis = fix_data.get("diagnosis", "Đã phân tích và cập nhật mã nguồn.")
        self.add_log("director", f"🩺 [Chẩn đoán của Giám đốc]: {diagnosis}")

        if fix_data.get("updated_install_command"):
            self.install_command = fix_data["updated_install_command"]
        if fix_data.get("updated_test_command"):
            self.test_command = fix_data["updated_test_command"]

        updated_count = 0
        for f_obj in fix_data.get("files_to_update", []):
            fpath = clean_file_path(f_obj.get("path") or "")
            fcontent = f_obj.get("content")
            if fpath and fcontent is not None:
                self.merged_files[fpath] = fcontent
                abs_p = os.path.join(self.merged_dir, fpath)
                os.makedirs(os.path.dirname(abs_p), exist_ok=True)
                with open(abs_p, "w", encoding="utf-8") as f:
                    f.write(fcontent)
                updated_count += 1

        self.add_log("director", f"🛠️ [Giám đốc] Đã vá xong {updated_count} file mã nguồn.")
