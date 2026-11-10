import mimetypes
import asyncio
import base64
import json
import glob
import os
import shutil
import subprocess
import time
import traceback
import urllib.error
import urllib.request
import uuid
import uvicorn
import zipfile
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import workspace_harness

from factory_engine import (
    FACTORY_ROOT,
    PROJECTS_DIR,
    BRIDGE_PORT,
    DOWNLOADS_DIR,
    COUNCIL_PERSONAS,
    DEEPSEEK_MODELS,
    FactoryProject,
    INBOX_POLL_SECONDS,
    close_bridge_session,
    BRIDGE_BACKENDS,
    LLM_BACKENDS,
    LLM_BACKEND_DEFAULT_MODEL,
    LLM_DEFAULT,
    LLM_EFFORTS,
    LLM_ROLE_IDS,
    ROLE_LLM_SETTINGS,
    GLOBAL_LLM_SETTING,
    save_global_llm_setting,
    load_saved_project_states,
    save_role_llm_setting,
    clean_file_path,
    BuilderRunnerWorker,
)

app = FastAPI(
    title="AI Software Factory — Công Xưởng Phần Mềm Tự Hành",
    description="Mặc định làm việc 1-1 với Giám đốc Điều hành • Tùy chọn bật Mode Hội đồng 3 Tác nhân Brainstorm",
    version="1.3.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = os.path.join(FACTORY_ROOT, "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

PROJECTS: Dict[str, FactoryProject] = {}

DEFAULT_PROJ_ID = "proj_default_session"
def default_greeting() -> List[Dict[str, Any]]:
    return [
        {
            "speaker": "assistant",
            "name": "Trợ lý Điều phối (Chief of Staff)",
            "avatar": "👩‍💼",
            "role_title": "Chế độ Mặc định: Trao đổi trực tiếp với Trợ lý",
            "content": (
                "Chào Chủ tịch! Em là Trợ lý Điều phối của anh. Màn hình chính này em sẽ trực tiếp nhận lệnh, lắng nghe ý tưởng hoặc bài toán của anh.\n"
                "• Anh chỉ cần mô tả ý tưởng phần mềm (hoặc nạp thư mục / file .ZIP dự án).\n"
                "• Nếu anh muốn họp phản biện, em sẽ triệu tập Hội đồng Cố vấn (Ủng hộ • Phản biện • Trung lập).\n"
                "• Khi anh chốt triển khai, em sẽ đúc kết thành Bản Yêu Cầu Kỹ Thuật (Brief) và phát lệnh giao Giám đốc Kỹ thuật (👔) cùng Đội ngũ Kỹ sư (👷) lập tức thi công!"
            ),
            "timestamp": time.strftime("%H:%M:%S"),
        }
    ]


default_proj = FactoryProject(DEFAULT_PROJ_ID, "Dự Án Phần Mềm Mới")
default_proj.council_messages = default_greeting()
PROJECTS[DEFAULT_PROJ_ID] = default_proj


def safe_save(proj: FactoryProject) -> None:
    """Ghi lịch sử dự án xuống đĩa. Lỗi ghi không làm dừng việc đang chạy."""
    try:
        proj.save_state()
    except OSError as err:
        print(f"⚠️ Không lưu được lịch sử dự án {proj.project_id}: {err}")


def restore_saved_projects() -> None:
    """Khi server khởi động: nạp lại mọi dự án đã lưu, để lịch sử và danh sách dự án còn sau khi khởi động lại."""
    for saved_id, data in load_saved_project_states().items():
        if saved_id == DEFAULT_PROJ_ID:
            default_proj.load_saved_state(data)
            continue
        project = FactoryProject(saved_id, data.get("title", saved_id))
        project.load_saved_state(data)
        PROJECTS[saved_id] = project


async def _run_and_save(proj: FactoryProject, coro) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception as err:
        # Việc nền lỗi mà không ai nhìn thấy thì Chủ tịch chỉ thấy im lặng: ghi vào nhật ký để còn biết vì sao
        proj.add_log("system", f"⚠️ Việc nền dừng vì lỗi: {str(err)[:300] or err.__class__.__name__}")
        traceback.print_exc()
    finally:
        safe_save(proj)


def spawn(proj: FactoryProject, coro) -> None:
    """Chạy việc nền của dự án (hội đồng, phòng họp, biên bản, triển khai). Lưu trước và sau khi chạy."""
    safe_save(proj)
    asyncio.create_task(_run_and_save(proj, coro))


restore_saved_projects()


async def _inbox_signal_loop() -> None:
    """Tín hiệu download mới: quét hộp thư zip của Giám đốc ở mọi project, có zip mới thì đánh thức Người ship."""
    while True:
        await asyncio.sleep(INBOX_POLL_SECONDS)
        for proj in list(PROJECTS.values()):
            try:
                proj.check_inbox_signal()
            except Exception:
                traceback.print_exc()


@app.on_event("startup")
async def _start_background_signals() -> None:
    asyncio.create_task(_inbox_signal_loop())


class CreateProjectRequest(BaseModel):
    title: str
    initial_idea: Optional[str] = ""
    mode: Optional[str] = "assistant"  # "assistant" (mặc định), "director" hoặc "all"


class CouncilMessageRequest(BaseModel):
    message: str
    title: Optional[str] = None
    target_agent: Optional[str] = "assistant"  # Mặc định "assistant"


class FinalizeProjectRequest(BaseModel):
    final_directive: Optional[str] = ""
    auto_execute: Optional[bool] = False


class ExecuteRoadmapRequest(BaseModel):
    max_heal_rounds: int = 3


class ImportZipRequest(BaseModel):
    filename: Optional[str] = "existing_project.zip"
    base64_data: Optional[str] = None
    local_path: Optional[str] = None
    title: Optional[str] = None


DELETED_PROJECT_IDS: set = set()  # project vừa xóa: một lượt tải trang còn dang dở không được dựng lại nó


def get_or_create_project(project_id: str, title: Optional[str] = None) -> FactoryProject:
    if project_id in DELETED_PROJECT_IDS:
        raise HTTPException(status_code=404, detail="Project này đã bị xóa.")
    if project_id not in PROJECTS:
        p = FactoryProject(project_id, title or "Dự Án Phần Mềm")
        p.council_messages = default_greeting()
        PROJECTS[project_id] = p
    elif title and title.strip():
        PROJECTS[project_id].title = title.strip()
    return PROJECTS[project_id]


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def serve_dashboard():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(index_path):
        return HTMLResponse("<h1>Đang khởi tạo giao diện Công xưởng...</h1>")
    with open(index_path, "r", encoding="utf-8") as f:
        return HTMLResponse(
            f.read(),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )


@app.api_route("/manifest.webmanifest", methods=["GET", "HEAD"], include_in_schema=False)
@app.api_route("/manifest.json", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_manifest():
    return FileResponse(
        os.path.join(STATIC_DIR, "manifest.webmanifest"),
        media_type="application/manifest+json",
    )


@app.api_route("/sw.js", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_service_worker():
    # Đặt ở gốc "/" để service worker có phạm vi (scope) toàn app
    return FileResponse(
        os.path.join(STATIC_DIR, "sw.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/personas")
async def get_personas():
    return {"personas": list(COUNCIL_PERSONAS.values())}


BRIDGE_HOST = os.environ.get("FACTORY_BRIDGE_HOST", "127.0.0.1")
BRIDGE_BASE_URL = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}"

def resolve_home_dir():
    env_root = os.environ.get("FACTORY_WORKSPACE_ROOT")
    if env_root and os.path.isdir(env_root):
        return os.path.realpath(env_root)
    if os.path.isdir("/home/thinh"):
        return "/home/thinh"
    if os.path.isdir("/app"):
        return "/app"
    return os.path.realpath(os.path.expanduser("~"))

HOME_DIR = resolve_home_dir()


@app.get("/api/fs/browse")
async def browse_folder(path: Optional[str] = None):
    """Liệt kê thư mục con để chọn đường dẫn ngay trên giao diện (cả trên Mac, Ubuntu và điện thoại)."""
    raw_path = path or HOME_DIR
    target = os.path.realpath(os.path.expanduser(raw_path))

    # Chặn các thư mục nhân hệ thống
    BLOCKED_DIRS = ("/proc", "/sys", "/dev", "/run")
    if any(target == b or target.startswith(b + os.sep) for b in BLOCKED_DIRS):
        raise HTTPException(status_code=403, detail="Không được phép truy cập thư mục hệ thống.")

    if not os.path.isdir(target):
        if os.path.isdir(HOME_DIR):
            target = HOME_DIR
        elif os.path.isdir("/home/thinh"):
            target = "/home/thinh"
        elif os.path.isdir("/app"):
            target = "/app"
        else:
            target = "/"

    dirs = []
    try:
        with os.scandir(target) as entries:
            for e in entries:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        dirs.append({
                            "name": e.name,
                            "path": e.path,
                            "mtime": int(e.stat().st_mtime)
                        })
                except (OSError, PermissionError):
                    pass
    except PermissionError:
        raise HTTPException(status_code=403, detail="Không có quyền đọc thư mục này.")

    dirs.sort(key=lambda d: d["name"].casefold())

    files = []
    try:
        with os.scandir(target) as entries:
            for e in entries:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_file(follow_symlinks=False):
                        files.append({
                            "name": e.name,
                            "path": e.path,
                            "size": e.stat().st_size,
                            "mtime": int(e.stat().st_mtime)
                        })
                except (OSError, PermissionError):
                    pass
    except PermissionError:
        pass

    files.sort(key=lambda f: f["name"].casefold())

    parent_dir = None if target == "/" else os.path.dirname(target)
    if parent_dir == target:
        parent_dir = None

    # Danh sách các lối tắt tiện ích
    shortcuts = []
    candidates = [
        ("🏠 Thư mục Home (~)", HOME_DIR),
        ("🖥️ Desktop", os.path.join(HOME_DIR, "Desktop")),
        ("📄 Documents", os.path.join(HOME_DIR, "Documents")),
        ("⬇️ Downloads", os.path.join(HOME_DIR, "Downloads")),
        ("📁 Projects", os.path.join(HOME_DIR, "Projects")),
        ("📁 projects", os.path.join(HOME_DIR, "projects")),
        ("🏭 ai-software-factory", os.path.join(HOME_DIR, "ai-software-factory")),
    ]
    for label, p in candidates:
        if os.path.isdir(p) and not any(s["path"] == p for s in shortcuts):
            shortcuts.append({"label": label, "path": p})

    return {
        "files": files,
        "path": target,
        "parent": parent_dir,
        "home": HOME_DIR,
        "dirs": dirs,
        "shortcuts": shortcuts,
    }


LOCAL_HOSTS = ("127.0.0.1", "localhost")


@app.post("/api/fs/choose-folder")
async def choose_folder_in_finder(request: Request):
    """Mở hộp thoại Finder của macOS trên chính máy đang chạy server.
    Chỉ dùng khi người dùng mở app trực tiếp trên Mac (127.0.0.1 hoặc localhost).
    Từ điện thoại qua Tailscale, giao diện dùng màn hình chọn thư mục trong app."""
    host = (request.headers.get("host") or "").split(":")[0]
    if host not in LOCAL_HOSTS:
        raise HTTPException(status_code=403, detail="Chỉ mở Finder khi đang dùng trực tiếp trên Mac.")
    script = 'POSIX path of (choose folder with prompt "Chọn thư mục dự án để nạp vào Xưởng")'
    result = await asyncio.to_thread(subprocess.run, ["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        if "-128" in result.stderr:  # người dùng bấm Hủy trong hộp thoại
            return {"path": None, "cancelled": True}
        raise HTTPException(status_code=500, detail=f"Không mở được Finder: {result.stderr.strip()}")
    return {"path": result.stdout.strip().rstrip("/") or None, "cancelled": False}


@app.post("/api/fs/choose-file")
async def choose_file_in_finder(request: Request):
    """Hộp thoại Finder chọn một file trên Mac (chỉ khi dùng trực tiếp trên Mac)."""
    host = (request.headers.get("host") or "").split(":")[0]
    if host not in LOCAL_HOSTS:
        raise HTTPException(status_code=403, detail="Chỉ mở Finder khi đang dùng trực tiếp trên Mac.")
    script = 'POSIX path of (choose file with prompt "Chọn file để nạp vào Xưởng")'
    result = await asyncio.to_thread(subprocess.run, ["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        if "-128" in result.stderr:
            return {"path": None, "cancelled": True}
        raise HTTPException(status_code=500, detail=f"Không mở được Finder: {result.stderr.strip()}")
    return {"path": result.stdout.strip() or None, "cancelled": False}


@app.get("/api/llm/settings")
async def get_llm_settings():
    return {
        "roles": ROLE_LLM_SETTINGS,
        "global": GLOBAL_LLM_SETTING,
        "efforts": list(LLM_EFFORTS),
        "default": LLM_DEFAULT,
        "backends": list(LLM_BACKENDS),
        "bridge_backends": list(BRIDGE_BACKENDS),
        "backend_default_models": LLM_BACKEND_DEFAULT_MODEL,
        "deepseek_models": list(DEEPSEEK_MODELS),
        "deepseek_key_set": bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
    }


class GlobalLlmRequest(BaseModel):
    backend: str = "chatgpt"
    reasoning_effort: str = "none"
    model: Optional[str] = ""


@app.get("/api/llm/global-settings")
async def get_global_llm_settings():
    providers = [
        {
            "id": "chatgpt",
            "name": "ChatGPT Web Bridge",
            "desc": "Kết nối trực tiếp qua trình duyệt trên máy chủ Ubuntu. Miễn phí, không giới hạn token.",
            "available": True,
            "status_text": "Đang hoạt động"
        },
        {
            "id": "deepseek_api",
            "name": "DeepSeek API",
            "desc": "Kết nối trực tiếp qua API chính thức của DeepSeek.",
            "available": bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
            "status_text": "Đang hoạt động" if bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()) else "Chưa hỗ trợ (Cần DEEPSEEK_API_KEY)"
        },
        {
            "id": "deepseek",
            "name": "DeepSeek Web",
            "desc": "Bridge Web DeepSeek.",
            "available": False,
            "status_text": "Chưa hỗ trợ"
        },
        {
            "id": "claude",
            "name": "Claude (Anthropic)",
            "desc": "Mô hình Claude 3.5 Sonnet / Opus.",
            "available": False,
            "status_text": "Chưa hỗ trợ"
        },
        {
            "id": "gemini",
            "name": "Google Gemini",
            "desc": "Mô hình Gemini 1.5 Pro / Flash.",
            "available": False,
            "status_text": "Chưa hỗ trợ"
        }
    ]
    return {
        "global": GLOBAL_LLM_SETTING,
        "providers": providers,
        "efforts": list(LLM_EFFORTS),
        "roles": ROLE_LLM_SETTINGS,
    }


@app.put("/api/llm/global-settings")
async def put_global_llm_settings(req: GlobalLlmRequest):
    try:
        saved = save_global_llm_setting(req.backend, req.reasoning_effort, req.model or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "global": saved}


class RoleLlmRequest(BaseModel):
    backend: str = "chatgpt"
    model: str = ""
    reasoning_effort: str = "none"
    inherit: bool = False


@app.put("/api/llm/settings/{role_id}")
async def put_llm_setting(role_id: str, req: RoleLlmRequest):
    try:
        saved = save_role_llm_setting(role_id, req.model, req.reasoning_effort, req.backend, inherit=req.inherit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"role": role_id, **saved}


@app.get("/api/llm/models")
async def list_llm_models(provider: str = "chatgpt"):
    """Danh sách model đọc từ bridge theo trang web (chatgpt hoặc deepseek). Nếu bridge chưa đọc được thì trả lỗi để giao diện vẫn nhập tay được."""
    if provider not in BRIDGE_BACKENDS:
        raise HTTPException(status_code=400, detail=f"Trang web không được hỗ trợ: {provider}")
    url = f"{BRIDGE_BASE_URL}/v1/models?provider={provider}"

    def _fetch():
        with urllib.request.urlopen(url, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        data = await asyncio.to_thread(_fetch)
    except urllib.error.HTTPError as e:
        # Bridge trả lỗi kèm lời nhắn (ví dụ: trang cần đăng nhập). Hiện nguyên lời nhắn để biết phải làm gì
        try:
            message = json.loads(e.read().decode("utf-8")).get("detail") or str(e)
        except Exception:
            message = str(e)
        # Lời nhắn của bridge nhắc "AI Video Studio"; ở đây nút bấm có tên khác
        message = message.replace("rồi bấm thử lại thao tác trong AI Video Studio", 'rồi bấm lại "Đăng nhập và tải model"')
        return {"models": [], "error": message}
    except Exception as e:
        return {"models": [], "error": f"Bridge chưa trả danh sách model: {e}"}
    ids = [row.get("id") for row in data.get("data", []) if isinstance(row, dict)]
    return {"models": [i for i in ids if i], "error": None}


@app.get("/api/projects")
async def list_projects():
    items = [p.to_dict() for p in sorted(PROJECTS.values(), key=lambda x: x.created_at, reverse=True)]
    return {"projects": items, "default_project_id": DEFAULT_PROJ_ID}


TRASH_DIR = os.path.expanduser("~/.Trash")


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, permanent: bool = False):
    """Xóa một project. Mặc định chuyển thư mục vào Thùng rác (lấy lại được); permanent=true thì xóa hẳn khỏi máy.
    Dự án mặc định được đặt lại thành dự án trống. Không xóa khi project đang chạy việc."""
    proj = PROJECTS.get(project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy project này.")
    meeting_running = bool(proj.meeting and proj.meeting.get("status") == "dang_hop")
    if proj.active_speaker or proj.council_thinking or proj._shipping or meeting_running or proj.status == "executing":
        raise HTTPException(status_code=409, detail="Project đang chạy việc, chờ xong rồi hãy xóa.")
    # Phiên của project không còn dùng: đóng tab trong bridge cho gọn (cuộc trò chuyện vẫn nằm trong lịch sử ChatGPT, không bị xóa)
    retired_sessions = [proj.thread_session_id(role) for role in list(proj.threads)]
    moved_to = None
    if os.path.isdir(proj.project_dir):
        if permanent:
            shutil.rmtree(proj.project_dir, ignore_errors=True)
            for leftover in glob.glob(os.path.join(DOWNLOADS_DIR, f"{project_id}_*")):
                shutil.rmtree(leftover, ignore_errors=True)
        else:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for base in (TRASH_DIR, os.path.join(PROJECTS_DIR, "_deleted")):
                try:
                    os.makedirs(base, exist_ok=True)
                    moved_to = shutil.move(proj.project_dir, os.path.join(base, f"{project_id}-{stamp}"))
                    break
                except OSError:
                    continue
            if moved_to is None:
                raise HTTPException(status_code=500, detail="Không chuyển được thư mục project vào Thùng rác.")
    for session_id in retired_sessions:
        asyncio.create_task(close_bridge_session(session_id))
    if project_id == DEFAULT_PROJ_ID:
        # Dự án mặc định luôn tồn tại: đặt lại thành trống, phiên chat mới để không dính hội thoại cũ trên bridge
        default_proj.__init__(DEFAULT_PROJ_ID, "Dự Án Phần Mềm Mới")
        default_proj.council_messages = default_greeting()
        epoch = int(time.time())
        for role in list(LLM_ROLE_IDS) + ["shipper", "roadmap"]:
            default_proj.threads[role] = {"gen": epoch, "seeded": False, "chars": 0}
        safe_save(default_proj)
        PROJECTS[DEFAULT_PROJ_ID] = default_proj
    else:
        PROJECTS.pop(project_id, None)
        DELETED_PROJECT_IDS.add(project_id)
    return {"deleted": project_id, "permanent": permanent, "moved_to": moved_to, "reset": project_id == DEFAULT_PROJ_ID}


@app.post("/api/projects")
async def create_project(req: CreateProjectRequest):
    proj_id = f"proj_{int(time.time())}_{uuid.uuid4().hex[:4]}"
    project = FactoryProject(proj_id, req.title.strip() or "Dự Án Mới")
    project.council_messages = [
        {
            "speaker": "assistant",
            "name": "Trợ lý Điều phối (Chief of Staff)",
            "avatar": "👩‍💼",
            "role_title": "Chế độ Mặc định: Trao đổi trực tiếp với Trợ lý",
            "content": f"Dự án '{project.title}' đã sẵn sàng. Em là Trợ lý Điều phối của anh. Mời Chủ tịch nêu bài toán hoặc yêu cầu cần triển khai!",
            "timestamp": time.strftime("%H:%M:%S"),
        }
    ]
    PROJECTS[proj_id] = project
    safe_save(project)
    if req.initial_idea and req.initial_idea.strip():
        target = req.mode if req.mode in ("assistant", "director", "all", "advocate", "critic", "arbiter") else "assistant"
        spawn(project, project.run_council_round(req.initial_idea.strip(), target_agent=target))
    return project.to_dict()


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str):
    proj = get_or_create_project(project_id)
    return proj.to_dict()


@app.get("/api/projects/{project_id}/events")
async def project_events_stream(project_id: str, request: Request):
    """Kênh truyền sự kiện thời gian thực (Server-Sent Events) tới trình duyệt."""
    proj = get_or_create_project(project_id)
    queue: asyncio.Queue = asyncio.Queue()
    if not hasattr(proj, "_event_listeners"):
        proj._event_listeners = []
    proj._event_listeners.append(queue)

    async def event_generator():
        try:
            yield f"data: {json.dumps({'type': 'init', 'project': proj.to_dict()}, ensure_ascii=False)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=12.0)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            if hasattr(proj, "_event_listeners") and queue in proj._event_listeners:
                proj._event_listeners.remove(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/projects/{project_id}/import-zip")
async def import_project_zip(project_id: str, req: ImportZipRequest):
    proj = get_or_create_project(project_id, req.title)
    try:
        if req.base64_data:
            b64 = req.base64_data
            if "," in b64:
                b64 = b64.split(",", 1)[1]
            raw_bytes = base64.b64decode(b64)
            result = proj.import_zip_bytes(req.filename or "project.zip", raw_bytes)
            safe_save(proj)
            return result
        elif req.local_path:
            result = proj.import_local_path(req.local_path)
            safe_save(proj)
            return result
        else:
            raise HTTPException(status_code=400, detail="Vui lòng chọn file ZIP hoặc nhập đường dẫn thư mục/file ZIP.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/projects/{project_id}/council")
async def council_discuss(project_id: str, req: CouncilMessageRequest):
    proj = get_or_create_project(project_id, req.title)
    spawn(proj, proj.run_council_round(req.message, target_agent=req.target_agent or "assistant"))
    await asyncio.sleep(0.05)
    return proj.to_dict()


class RoleMessageRequest(BaseModel):
    role_id: str
    message: str


@app.post("/api/projects/{project_id}/role-message")
async def role_message(project_id: str, req: RoleMessageRequest):
    """Nhắn 1-1 với bất kỳ vai trò nào (Trợ lý, Giám đốc, Kỹ sư 1-5, Cố vấn, Tester, Giao liên); câu trả lời hiện ở khung chat chung."""
    text = (req.message or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Nội dung tin nhắn đang trống.")
    
    role_map = {
        "sub1": "subagent-1",
        "sub2": "subagent-2",
        "sub3": "subagent-3",
        "sub4": "subagent-4",
        "sub5": "subagent-5",
    }
    target = role_map.get(req.role_id, req.role_id)
    allowed_roles = (
        "assistant", "director", "secretary", "advocate", "critic", "arbiter",
        "subagent-1", "subagent-2", "subagent-3", "subagent-4", "subagent-5",
        "courier", "tester"
    )
    if target not in allowed_roles:
        raise HTTPException(status_code=400, detail=f"Vai trò '{target}' không hợp lệ.")
    
    proj = get_or_create_project(project_id, None)
    spawn(proj, proj.run_council_round(text, target_agent=target))
    await asyncio.sleep(0.05)
    return proj.to_dict()


class MeetingRequest(BaseModel):
    topic: str = ""
    max_rounds: Optional[int] = None


@app.post("/api/projects/{project_id}/meeting")
async def start_meeting(project_id: str, req: MeetingRequest):
    """Mở phòng họp: các cố vấn tranh luận trong phòng chung, bỏ phiếu theo phương án đang xét."""
    topic = (req.topic or "").strip()
    if not topic:
        raise HTTPException(status_code=400, detail="Hãy nêu vấn đề để cả hội đồng cùng bàn.")
    proj = get_or_create_project(project_id)
    if proj.meeting and proj.meeting.get("status") == "dang_hop":
        raise HTTPException(status_code=409, detail="Phòng họp đang diễn ra. Hãy đợi phòng họp kết thúc.")
    proj.begin_meeting(topic, req.max_rounds)
    spawn(proj, proj.run_meeting_and_opinion())
    await asyncio.sleep(0.05)
    return proj.to_dict()


@app.post("/api/projects/{project_id}/finalize")
async def secretary_finalize(project_id: str, req: FinalizeProjectRequest):
    """Chốt phương án: Thư ký lập biên bản và chờ Chủ tịch duyệt. Không tự triển khai."""
    if not (req.final_directive or "").strip():
        raise HTTPException(status_code=400, detail="Hãy viết chỉ đạo chốt của Chủ tịch vào ô chat rồi bấm chốt lại.")
    proj = get_or_create_project(project_id)

    async def _finalize():
        await proj.run_secretary_finalize(req.final_directive or "")

    spawn(proj, _finalize())
    await asyncio.sleep(0.05)
    return proj.to_dict()


@app.post("/api/projects/{project_id}/execute")
async def execute_roadmap(project_id: str, req: ExecuteRoadmapRequest):
    proj = get_or_create_project(project_id)
    if not proj.roadmap or proj.decision_record is None:
        raise HTTPException(status_code=400, detail="Chưa có biên bản chờ duyệt. Hãy chốt phương án trước.")
    spawn(proj, proj.execute_full_roadmap(max_heal_rounds=req.max_heal_rounds))
    return {"status": "started", "project": proj.to_dict()}


class SendZipRequest(BaseModel):
    note: str = ""
    ship: bool = True  # False: chỉ gửi cho Giám đốc, chưa cho Người ship chạy


@app.post("/api/projects/{project_id}/send-to-director")
async def send_to_director(project_id: str, req: SendZipRequest):
    """Gửi zip dự án đã nạp cho Giám đốc (GPT) để phát triển tiếp."""
    proj = get_or_create_project(project_id)
    if not proj.merged_files:
        raise HTTPException(status_code=400, detail="Kho mã nguồn đang trống. Hãy nạp ZIP hoặc thư mục trước.")
    if proj._shipping:
        raise HTTPException(status_code=409, detail="Người ship hoặc trợ lý đang chạy.")
    spawn(proj, proj.send_zip_to_director(req.note, ship=req.ship))
    return {"status": "started", "project": proj.to_dict()}


@app.post("/api/projects/{project_id}/ship")
async def ship_now(project_id: str):
    """Đưa các zip đang nằm trong hộp thư của Giám đốc cho Người ship (bấm tay khi tự động chưa chạy)."""
    proj = get_or_create_project(project_id)
    if proj._shipping:
        raise HTTPException(status_code=409, detail="Người ship hoặc trợ lý đang chạy.")
    if not proj.start_ship_from_inbox():
        raise HTTPException(status_code=400, detail="Hộp thư của Giám đốc chưa có zip nào để ship.")
    safe_save(proj)
    return {"status": "started", "project": proj.to_dict()}


class AutopilotRequest(BaseModel):
    on: bool = True
    check_now: bool = True  # False: chỉ bật chế độ, chưa kiểm tra ngay (chờ tín hiệu Giám đốc báo xong)


@app.post("/api/projects/{project_id}/autopilot")
async def start_autopilot(project_id: str, req: Optional[AutopilotRequest] = None):
    """Bật hoặc tắt Trợ lý roadmap. Đang bật thì mỗi lần Giám đốc báo hoàn thành (zip mới hoặc mã gọi vai), trợ lý kiểm tra tài liệu sống
    và nhắn Giám đốc làm tiếp. Bật xong, trợ lý kiểm tra ngay một lần."""
    proj = get_or_create_project(project_id)
    if req is not None and not req.on:
        proj.set_autopilot(False)
        safe_save(proj)
        return {"status": "off", "project": proj.to_dict()}
    if proj._shipping or (req is not None and not req.check_now):
        proj.set_autopilot(True)  # đang chạy vòng khác hoặc chưa cần kiểm tra ngay: ghi nhận bật, các tín hiệu sau sẽ đánh thức trợ lý
        safe_save(proj)
        return {"status": "on", "project": proj.to_dict()}
    spawn(proj, proj.run_autopilot())
    return {"status": "started", "project": proj.to_dict()}


@app.post("/api/projects/{project_id}/director/rotate")
async def rotate_director_session(project_id: str):
    """Chủ động tóm tắt và mở phiên Giám đốc mới tinh trên ChatGPT để chống lag/quá tải ngữ cảnh."""
    proj = get_or_create_project(project_id)
    await proj.compact_memory("director", force=True)
    t = proj.rotate_thread("director")
    s_info = proj.director_session_info()
    gen_display = s_info.get("gen", 1)
    proj.add_log("system", f"🔄 Chủ tịch chủ động làm mới phiên Giám đốc (Thế hệ {gen_display}).")
    proj.council_messages.append({
        "speaker": "system",
        "name": "Local Workspace Harness",
        "avatar": "🔄",
        "role_title": "Làm mới phiên chủ động",
        "content": f"🔄 **[Đã Làm Mới Phiên - Thế Hệ {gen_display}]**\nĐã tóm tắt toàn bộ lịch sử trước đó và khởi tạo phiên ChatGPT sạch mới theo yêu cầu của Chủ tịch.\n- Toàn bộ mã nguồn trên đĩa máy chủ giữ nguyên 100%.\n- Ngữ cảnh được nạp lại tinh gọn, không lag tab và không nuốt tin nhắn.",
        "timestamp": time.strftime("%H:%M:%S"),
    })
    safe_save(proj)
    return {"success": True, "gen": gen_display, "session": s_info, "project": proj.to_dict()}


@app.get("/api/projects/{project_id}/file")
async def get_project_file(project_id: str, path: str):
    proj = get_or_create_project(project_id)
    content = proj.merged_files.get(path)
    if content is None:
        root = os.path.realpath(proj.merged_dir)
        abs_p = os.path.realpath(os.path.join(root, path.lstrip("/")))
        # Chỉ đọc file nằm trong kho mã nguồn của project (chặn ../ và liên kết trỏ ra ngoài)
        if os.path.commonpath([root, abs_p]) == root and os.path.isfile(abs_p):
            with open(abs_p, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        else:
            raise HTTPException(status_code=404, detail="Không tìm thấy file này trong kho.")
    return {"path": path, "content": content}


@app.get("/api/projects/{project_id}/director-zips/{name}")
async def download_director_zip(project_id: str, name: str):
    """Tải về một zip Giám đốc đã gửi ra (ví dụ gói theme). Chỉ file .zip nằm trong hộp thư của project này."""
    proj = PROJECTS.get(project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy project này.")
    if os.path.basename(name) != name or not name.lower().endswith(".zip"):
        raise HTTPException(status_code=404, detail="Không có zip này.")
    for folder in (proj.director_inbox(), os.path.join(proj.project_dir, "build_outputs")):
        base = os.path.realpath(folder)
        full = os.path.realpath(os.path.join(base, name))
        if os.path.dirname(full) == base and os.path.isfile(full):
            return FileResponse(full, filename=name, media_type="application/zip")
    raise HTTPException(status_code=404, detail="Không có zip này trong hộp thư của Giám đốc hay trong sản phẩm build.")


@app.get("/api/projects/{project_id}/download-zip")
async def download_full_zip(project_id: str):
    proj = PROJECTS.get(project_id)
    if not proj or not proj.full_zip_path or not os.path.exists(proj.full_zip_path):
        raise HTTPException(status_code=404, detail="Gói full_source.zip chưa sẵn sàng.")
    return FileResponse(
        proj.full_zip_path,
        media_type="application/zip",
        filename=f"{project_id}_full_source.zip",
    )


# ---- WORKSPACE HARNESS API: CÂY THƯ MỤC & PHẪU THUẬT MÃ NGUỒN CỤC BỘ ----
def _get_project_workspace_dir(proj) -> str:
    ws = getattr(proj, "build_workspace", os.path.join(proj.project_dir, "build_workspace"))
    if not os.path.exists(ws):
        ws = os.path.join(proj.project_dir, "merged_source")
    os.makedirs(ws, exist_ok=True)
    return ws


@app.get("/api/projects/{project_id}/harness/tree")
async def get_project_harness_tree(project_id: str):
    """Lấy cây thư mục hiện tại của dự án dạng Text ASCII và JSON để hiển thị trực quan."""
    proj = get_or_create_project(project_id)
    ws = _get_project_workspace_dir(proj)
    tree_text, json_tree = workspace_harness.get_workspace_tree(ws)
    return {
        "project_id": project_id,
        "workspace": ws,
        "tree_text": tree_text,
        "tree": json_tree,
    }


@app.get("/api/projects/{project_id}/harness/file")
async def get_project_harness_file(project_id: str, path: str, start: int = 1, count: int = 150):
    """Đọc một lát cắt mã nguồn của file trong workspace kèm số thứ tự dòng."""
    proj = get_or_create_project(project_id)
    ws = _get_project_workspace_dir(proj)
    res = workspace_harness.read_file_slice(ws, path, start_line=start, line_count=count)
    if "error" in res:
        raise HTTPException(status_code=404, detail=res["error"])
    return res


class HarnessSearchRequest(BaseModel):
    query: str
    max_results: int = 40


@app.post("/api/projects/{project_id}/harness/search")
async def search_project_harness_code(project_id: str, req: HarnessSearchRequest):
    """Tìm kiếm biến, hàm hoặc chuỗi trong toàn bộ mã nguồn của workspace."""
    proj = get_or_create_project(project_id)
    ws = _get_project_workspace_dir(proj)
    matches = workspace_harness.search_code(ws, req.query, max_results=req.max_results)
    return {"query": req.query, "count": len(matches), "matches": matches}


class HarnessEditRequest(BaseModel):
    patch_text: Optional[str] = None
    path: Optional[str] = None
    search: Optional[str] = None
    replace: Optional[str] = None
    content: Optional[str] = None


@app.post("/api/projects/{project_id}/harness/edit")
async def apply_project_harness_edits(project_id: str, req: HarnessEditRequest):
    """Áp dụng bản vá SEARCH/REPLACE hoặc tạo file mới trực tiếp trên đĩa mà không cần ZIP."""
    proj = get_or_create_project(project_id)
    ws = _get_project_workspace_dir(proj)
    if req.path and req.search:
        res = workspace_harness.apply_search_replace(ws, req.path, req.search, req.replace or "")
        if res.get("success"):
            proj.add_log("harness", f"🛠️ [Harness] Đã vá file {req.path} trên đĩa ({res.get('strategy')}).")
        return res
    elif req.path and req.content is not None:
        res = workspace_harness.write_file(ws, req.path, req.content)
        proj.add_log("harness", f"📝 [Harness] Đã ghi file {req.path} ({res.get('lines')} dòng).")
        return res
    elif req.patch_text:
        res = workspace_harness.extract_and_apply_harness_edits(ws, req.patch_text)
        proj.add_log("harness", f"🛠️ [Harness] Đã áp dụng {res['applied_count']} thay đổi vào mã nguồn.")
        return res
    raise HTTPException(status_code=400, detail="Thiếu thông tin patch_text hoặc path+search.")


class HarnessFilterRequest(BaseModel):
    text: str


@app.post("/api/projects/{project_id}/harness/filter")
async def filter_project_harness_patches(project_id: str, req: HarnessFilterRequest):
    """Bóc tách và lọc sạch các khối code/bản vá từ câu trả lời chat của AI, loại bỏ mọi lời chào/bình luận."""
    patches = workspace_harness.filter_and_extract_patches(req.text)
    return {"count": len(patches), "patches": patches}


# ==============================================================================
# GIT VERSION CONTROL, BRANCHING & SAFE MERGE API
# ==============================================================================
class GitBranchRequest(BaseModel):
    branch_name: str
    checkout: bool = True

class GitSwitchRequest(BaseModel):
    branch_name: str

class GitCommitRequest(BaseModel):
    message: str
    role: str = "Giám đốc Kỹ thuật"

class GitMergeRequest(BaseModel):
    source_branch: str
    target_branch: str = "main"

class GitRollbackRequest(BaseModel):
    commit_hash: str


@app.get("/api/projects/{project_id}/git/status")
async def get_project_git_status(project_id: str):
    proj = get_or_create_project(project_id)
    ws = _get_project_workspace_dir(proj)
    return workspace_harness.git_get_status(ws)


@app.post("/api/projects/{project_id}/git/init")
async def init_project_git(project_id: str):
    proj = get_or_create_project(project_id)
    ws = _get_project_workspace_dir(proj)
    return workspace_harness.git_init_workspace(ws)


@app.post("/api/projects/{project_id}/git/branch")
async def create_project_git_branch(project_id: str, req: GitBranchRequest):
    proj = get_or_create_project(project_id)
    ws = _get_project_workspace_dir(proj)
    res = workspace_harness.git_create_branch(ws, req.branch_name, checkout=req.checkout)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error"))
    proj.add_log("git", f"🌿 [Git] Đã tạo và chuyển sang nhánh `{res['branch']}`.")
    return res


@app.post("/api/projects/{project_id}/git/switch")
async def switch_project_git_branch(project_id: str, req: GitSwitchRequest):
    proj = get_or_create_project(project_id)
    ws = _get_project_workspace_dir(proj)
    res = workspace_harness.git_switch_branch(ws, req.branch_name)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error"))
    proj.add_log("git", f"🌿 [Git] Đã chuyển không gian làm việc sang nhánh `{req.branch_name}`.")
    return res


@app.post("/api/projects/{project_id}/git/commit")
async def commit_project_git(project_id: str, req: GitCommitRequest):
    proj = get_or_create_project(project_id)
    ws = _get_project_workspace_dir(proj)
    res = workspace_harness.git_commit(ws, req.message, author_role=req.role)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error"))
    if not res.get("no_changes"):
        proj.add_log("git", f"💾 [Git] Đã lưu Checkpoint `{res['commit']}`: {res['message']}.")
    return res


@app.get("/api/projects/{project_id}/git/diff")
async def get_project_git_diff(project_id: str, base: str = "main", target: Optional[str] = None):
    proj = get_or_create_project(project_id)
    ws = _get_project_workspace_dir(proj)
    return workspace_harness.git_diff(ws, base_branch=base, target=target)


@app.post("/api/projects/{project_id}/git/merge")
async def merge_project_git(project_id: str, req: GitMergeRequest):
    proj = get_or_create_project(project_id)
    ws = _get_project_workspace_dir(proj)
    res = workspace_harness.git_merge(ws, req.source_branch, target_branch=req.target_branch)
    if not res.get("success"):
        raise HTTPException(status_code=409 if res.get("conflict") else 400, detail=res.get("error"))
    proj.add_log("git", f"🚀 [Git Merge] Đã hợp nhất an toàn nhánh `{req.source_branch}` vào `{req.target_branch}` (Commit: {res['commit']}).")
    return res


@app.get("/api/projects/{project_id}/git/history")
async def get_project_git_history(project_id: str, limit: int = 20):
    proj = get_or_create_project(project_id)
    ws = _get_project_workspace_dir(proj)
    return {"commits": workspace_harness.git_log(ws, limit=limit)}


@app.post("/api/projects/{project_id}/git/rollback")
async def rollback_project_git(project_id: str, req: GitRollbackRequest):
    proj = get_or_create_project(project_id)
    ws = _get_project_workspace_dir(proj)
    res = workspace_harness.git_rollback(ws, req.commit_hash)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error"))
    proj.add_log("git", f"↩️ [Git Rollback] Đã khôi phục mã nguồn về mốc `{res['rollback_to']}`.")
    return res


# ==============================================================================
# WORKSPACE DIRECT BUILD & ZIP RUNNER API (UBUNTU NATIVE)
# ==============================================================================
def _detect_build_command(ws: str) -> Dict[str, str]:
    for cand in [os.path.join(ws, "TAI_LIEU_SONG", "BUILD.json"), os.path.join(ws, "BUILD.json")]:
        if os.path.isfile(cand):
            try:
                with open(cand, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    cmd = data.get("test") or data.get("build") or data.get("install") or ""
                    if cmd:
                        return {"type": "build_json", "command": cmd, "install": data.get("install", "")}
            except Exception:
                pass
    pkg_path = os.path.join(ws, "package.json")
    if os.path.isfile(pkg_path):
        try:
            with open(pkg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                scripts = data.get("scripts", {})
                if "build" in scripts:
                    return {"type": "npm", "command": "npm run build", "install": "npm install"}
                elif "typecheck" in scripts:
                    return {"type": "npm", "command": "npm run typecheck", "install": "npm install"}
                elif "test" in scripts:
                    return {"type": "npm", "command": "npm test", "install": "npm install"}
        except Exception:
            pass
    if os.path.isfile(os.path.join(ws, "Makefile")):
        return {"type": "make", "command": "make build", "install": ""}
    if os.path.isfile(os.path.join(ws, "build.sh")):
        return {"type": "script", "command": "bash build.sh", "install": ""}
    return {"type": "custom", "command": "npm run build", "install": ""}


class DirectUploadZipRequest(BaseModel):
    filename: Optional[str] = "upload.zip"
    base64_data: Optional[str] = None
    local_path: Optional[str] = None
    target_dir: Optional[str] = "workspace"


@app.post("/api/projects/{project_id}/direct-upload-zip")
async def direct_upload_zip(project_id: str, req: DirectUploadZipRequest):
    """
    Nạp tệp/ZIP trực tiếp lên máy chủ ĐỘC LẬP:
    - Hỗ trợ lưu vào Workspace dự án, thư mục Downloads, hoặc đường dẫn tuỳ chọn.
    - Không gọi AI, không đẩy vào chat council, không tốn context token.
    - Trả về vị trí lưu và lệnh thực thi gợi ý để Chủ tịch chủ động bấm 'Build'.
    """
    proj = get_or_create_project(project_id)
    ws = _get_project_workspace_dir(proj)

    # Xác định thư mục đích lưu file
    target_mode = (req.target_dir or "workspace").strip()
    if target_mode.lower() in ["downloads", "~/downloads"]:
        dest_base_dir = os.path.expanduser("~/Downloads")
        dest_display = "~/Downloads"
    elif target_mode.startswith("/") or target_mode.startswith("~"):
        dest_base_dir = os.path.expanduser(target_mode)
        dest_display = target_mode
    elif target_mode and target_mode != "workspace":
        clean_sub = clean_file_path(target_mode)
        dest_base_dir = os.path.join(ws, clean_sub)
        dest_display = f"workspace/{clean_sub}"
    else:
        dest_base_dir = ws
        dest_display = "workspace"

    os.makedirs(dest_base_dir, exist_ok=True)

    if req.base64_data:
        b64 = req.base64_data
        if "," in b64:
            b64 = b64.split(",", 1)[1]
        raw_bytes = base64.b64decode(b64)
        saved_zip = os.path.join(proj.project_dir, f"direct_{req.filename or 'upload.zip'}")
        with open(saved_zip, "wb") as f:
            f.write(raw_bytes)
        source_zip = saved_zip
    elif req.local_path:
        source_zip = os.path.expanduser(req.local_path.strip())
        if not os.path.exists(source_zip):
            raise HTTPException(status_code=400, detail=f"Không tìm thấy file: {source_zip}")
    else:
        raise HTTPException(status_code=400, detail="Thiếu dữ liệu ZIP (base64_data hoặc local_path).")

    if not zipfile.is_zipfile(source_zip):
        clean_rel = clean_file_path(req.filename or "uploaded_file")
        dest_file = os.path.join(dest_base_dir, clean_rel)
        os.makedirs(os.path.dirname(dest_file), exist_ok=True)
        with open(dest_file, "wb") as out_f:
            out_f.write(raw_bytes)

        if dest_base_dir == ws:
            try:
                text_content = raw_bytes.decode("utf-8")
                proj.merged_files[clean_rel] = text_content
            except Exception:
                pass
            if os.path.exists(ws) and ws != proj.merged_dir:
                m_dest = os.path.join(proj.merged_dir, clean_rel)
                os.makedirs(os.path.dirname(m_dest), exist_ok=True)
                try:
                    shutil.copy2(dest_file, m_dest)
                except Exception:
                    pass

        # Lệnh gợi ý
        file_ref = clean_rel if dest_base_dir == ws else dest_file
        det_cmd = f"cat {file_ref}"
        if clean_rel.endswith(".py"):
            det_cmd = f"python3 {file_ref}"
        elif clean_rel.endswith(".sh"):
            det_cmd = f"bash {file_ref}"
        elif clean_rel.endswith(".js"):
            det_cmd = f"node {file_ref}"
        elif clean_rel == "package.json":
            det_cmd = "npm install && npm test"
        elif clean_rel == "requirements.txt":
            det_cmd = "pip install -r requirements.txt"

        proj.add_log("system", f"📦 [Nạp File] Đã nạp thành công {clean_rel} vào {dest_display}.")
        safe_save(proj)
        return {
            "success": True,
            "filename": clean_rel,
            "file_count": 1,
            "workspace": dest_base_dir,
            "dest_display": dest_display,
            "target_dir": target_mode,
            "detected_command": det_cmd,
            "install_command": "",
            "is_zip": False,
        }

    # Giải nén an toàn vào thư mục đích
    file_count = 0
    with zipfile.ZipFile(source_zip, "r") as zf:
        members = [m for m in zf.namelist() if not m.endswith("/") and "__MACOSX" not in m]
        common_root = None
        if members:
            first_parts = {m.split("/")[0] for m in members if "/" in m}
            if len(first_parts) == 1 and all("/" in m for m in members):
                common_root = list(first_parts)[0] + "/"

        for member in members:
            rel_path = member[len(common_root):] if (common_root and member.startswith(common_root)) else member
            clean_rel = clean_file_path(rel_path)
            if not clean_rel or clean_rel.startswith(".git/"):
                continue
            dest_file = os.path.join(dest_base_dir, clean_rel)
            os.makedirs(os.path.dirname(dest_file), exist_ok=True)
            with open(dest_file, "wb") as out_f:
                out_f.write(zf.read(member))
            if dest_base_dir == ws:
                try:
                    text_content = zf.read(member).decode("utf-8")
                    proj.merged_files[clean_rel] = text_content
                except Exception:
                    pass
            file_count += 1

    # Đồng bộ sang merged_dir nếu lưu vào ws
    if dest_base_dir == ws and os.path.exists(ws) and ws != proj.merged_dir:
        for root_d, _, files in os.walk(ws):
            for f in files:
                rel = os.path.relpath(os.path.join(root_d, f), ws)
                if not rel.startswith(".git"):
                    m_dest = os.path.join(proj.merged_dir, rel)
                    os.makedirs(os.path.dirname(m_dest), exist_ok=True)
                    try:
                        shutil.copy2(os.path.join(root_d, f), m_dest)
                    except Exception:
                        pass

    detected = _detect_build_command(dest_base_dir) if dest_base_dir == ws else {"command": f"ls -la {dest_base_dir}"}
    proj.add_log("system", f"📦 [Nạp ZIP] Đã nạp thành công {file_count} files vào {dest_display}.")
    safe_save(proj)

    return {
        "success": True,
        "filename": req.filename or os.path.basename(source_zip),
        "file_count": file_count,
        "workspace": dest_base_dir,
        "dest_display": dest_display,
        "target_dir": target_mode,
        "detected_command": detected.get("command", "ls -la"),
        "detected_type": detected.get("type", "custom"),
        "message": f"Đã giải nén an toàn {file_count} files vào build_workspace trên Ubuntu. Chưa chạy lệnh nào. Bấm 'Chạy Build' để bắt đầu."
    }


class RunBuildRequest(BaseModel):
    command: Optional[str] = None
    timeout: Optional[int] = 300
    auto_fix: Optional[bool] = True


class CreateSessionRequest(BaseModel):
    title: Optional[str] = None


@app.post("/api/projects/{project_id}/sessions")
async def create_project_session(project_id: str, req: CreateSessionRequest):
    """Khởi tạo một phiên làm việc mới cho dự án, lưu phiên cũ và mở phiên chat sạch."""
    proj = get_or_create_project(project_id)
    new_sess = proj.create_new_session(req.title)
    safe_save(proj)
    return {
        "success": True,
        "session": new_sess,
        "project": proj.to_dict(),
    }


@app.post("/api/projects/{project_id}/sessions/{session_id}/switch")
async def switch_project_session(project_id: str, session_id: str):
    """Chuyển đổi sang phiên làm việc khác trong dự án."""
    proj = get_or_create_project(project_id)
    ok = proj.switch_session(session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Không tìm thấy phiên làm việc.")
    safe_save(proj)
    return {
        "success": True,
        "current_session_id": proj.current_session_id,
        "project": proj.to_dict(),
    }


@app.post("/api/projects/{project_id}/run-build")
async def run_project_build(project_id: str, req: RunBuildRequest):
    """
    Thực thi lệnh Build/Test trực tiếp trên máy Ubuntu trong thư mục build_workspace:
    - Bắt trọn toàn bộ stdout/stderr.
    - Cập nhật builder_state của project.
    - Trả về log chi tiết để hiển thị trên giao diện theo thời gian thực.
    """
    proj = get_or_create_project(project_id)
    ws = _get_project_workspace_dir(proj)

    cmd = (req.command or "").strip()
    if not cmd:
        detected = _detect_build_command(ws)
        cmd = detected.get("command") or proj.test_command or proj.install_command or "npm run build"

    timeout = req.timeout or 300
    proj.builder_state["status"] = "running"
    proj.add_log("builder", f"🚀 [Chủ tịch ấn Build] Đang chạy `{cmd}` trong `{os.path.basename(ws)}`...")

    t0 = time.time()
    code, out = await asyncio.to_thread(BuilderRunnerWorker._run_shell, cmd, ws, timeout)
    duration = time.time() - t0

    passed = (code == 0)
    proj.builder_state["status"] = "passed" if passed else "failed"
    proj.builder_state["last_exit_code"] = code
    proj.builder_state["last_log"] = out
    run_entry = {
        "iteration": len(proj.builder_state.get("runs", [])) + 1,
        "command": cmd,
        "exit_code": code,
        "passed": passed,
        "log": out,
        "duration_sec": round(duration, 2),
        "timestamp": time.strftime("%H:%M:%S")
    }
    proj.builder_state.setdefault("runs", []).append(run_entry)
    proj.builder_state["runs"] = proj.builder_state["runs"][-20:]

    log_file = os.path.join(proj.project_dir, f"build_manual_run_{run_entry['iteration']}.log")
    try:
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(out)
    except Exception:
        pass

    status_str = "✅ THÀNH CÔNG" if passed else f"❌ THẤT BẠI (Exit {code})"
    proj.add_log("builder", f"📋 [Kết quả Build] {status_str} sau {round(duration, 1)}s. Đã lưu log.")
    safe_save(proj)

    if not passed and getattr(req, "auto_fix", True):
        proj.add_log("builder", f"🔧 [Tự sửa lỗi] Đang tự động giao lại cho Giám đốc & Kỹ sư khắc phục lỗi...")
        spawn(proj, proj.run_self_healing_loop(cmd, out, code))

    return {
        "success": passed,
        "exit_code": code,
        "command": cmd,
        "log": out,
        "duration_sec": round(duration, 2),
        "timestamp": run_entry["timestamp"],
        "workspace": ws,
    }


class TerminalRunRequest(BaseModel):
    command: str
    timeout: Optional[int] = 120
    auto_heal: bool = False


@app.post("/api/projects/{project_id}/terminal/run")
async def run_project_terminal(project_id: str, req: TerminalRunRequest):
    """
    Thực thi lệnh Remote Terminal trực tiếp trên máy chủ trong workspace:
    - Bắt trọn stdout/stderr.
    - Hỗ trợ auto_heal nếu lệnh thất bại và người dùng bật tự sửa.
    """
    proj = get_or_create_project(project_id)
    ws = _get_project_workspace_dir(proj)
    cmd = (req.command or "").strip()
    if not cmd:
        raise HTTPException(status_code=400, detail="Lệnh không được để trống.")

    timeout = req.timeout or 120
    proj.add_log("terminal", f"💻 [Remote Terminal] Chạy `$ {cmd}` trong `{os.path.basename(ws)}`...")

    t0 = time.time()
    code, out = await asyncio.to_thread(BuilderRunnerWorker._run_shell, cmd, ws, timeout)
    duration = time.time() - t0

    proj.add_log("terminal", f"📋 [Terminal Kết quả] Exit {code} sau {round(duration, 1)}s.")

    if code != 0 and req.auto_heal:
        proj.add_log("builder", f"🔧 [Tự sửa lỗi] Đang tự động giao lại cho Kỹ sư khắc phục lỗi từ lệnh terminal...")
        spawn(proj, proj.run_self_healing_loop(cmd, out, code))

    safe_save(proj)
    return {
        "success": code == 0,
        "exit_code": code,
        "command": cmd,
        "log": out,
        "duration_sec": round(duration, 2),
        "timestamp": time.strftime("%H:%M:%S"),
        "workspace": ws,
    }


@app.get("/api/projects/{project_id}/build-info")
async def get_project_build_info(project_id: str):
    """Lấy thông tin cấu hình build, lệnh tự động phát hiện và lần chạy gần nhất."""
    proj = get_or_create_project(project_id)
    ws = _get_project_workspace_dir(proj)
    detected = _detect_build_command(ws)
    return {
        "project_id": project_id,
        "workspace": ws,
        "detected_command": detected.get("command", "npm run build"),
        "detected_type": detected.get("type", "custom"),
        "builder_state": proj.builder_state,
    }



@app.get("/api/projects/{project_id}/preview/{subpath:path}")
@app.get("/api/projects/{project_id}/preview")
async def preview_project_file(project_id: str, subpath: str = ""):
    """Serve static files from project workspace for live preview in the Workstation Panel."""
    proj = PROJECTS.get(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Không tìm thấy dự án.")
    
    ws = _get_project_workspace_dir(proj)
    clean_subpath = subpath.strip("/\\")
    if not clean_subpath:
        clean_subpath = "index.html"
        
    target_path = os.path.abspath(os.path.join(ws, clean_subpath))
    if not target_path.startswith(os.path.abspath(ws)):
        raise HTTPException(status_code=403, detail="Không được phép truy cập ngoài thư mục dự án.")
        
    if not os.path.exists(target_path):
        if clean_subpath == "index.html":
            html_files = [f for f in os.listdir(ws) if f.endswith(".html")] if os.path.exists(ws) else []
            if html_files:
                target_path = os.path.join(ws, html_files[0])
            else:
                files_list = os.listdir(ws) if os.path.exists(ws) else []
                files_html = "".join(f"<li><code>{f}</code></li>" for f in files_list) or "<li>(Chưa có tệp nào)</li>"
                return HTMLResponse(
                    "<html><body style='background:#121316;color:#e2e8f0;font-family:system-ui,-apple-system,sans-serif;padding:32px;line-height:1.6;'>"
                    "<div style='max-width:540px;margin:40px auto;background:#1a1c20;padding:24px;border-radius:12px;border:1px solid #2d3139;text-align:center;'>"
                    "<div style='font-size:36px;margin-bottom:12px;'>🌐</div>"
                    "<h3 style='margin:0 0 8px;color:#f8fafc;'>Live Web Preview</h3>"
                    "<p style='color:#94a3b8;font-size:13.5px;margin:0 0 16px;'>Chưa tìm thấy tệp <code>index.html</code> trong Workspace của dự án.</p>"
                    "<div style='text-align:left;background:#0f1012;padding:12px 16px;border-radius:8px;font-size:12.5px;color:#cbd5e1;'>"
                    "<strong>Danh sách tệp hiện có trong Workspace:</strong>"
                    f"<ul style='margin:6px 0 0;padding-left:20px;'>{files_html}</ul>"
                    "</div></div></body></html>"
                )
        else:
            raise HTTPException(status_code=404, detail="Không tìm thấy tệp.")
            
    content_type, _ = mimetypes.guess_type(target_path)
    return FileResponse(target_path, media_type=content_type or "application/octet-stream")


if __name__ == "__main__":
    print("=" * 70)
    print("🏭 AI SOFTWARE FACTORY SERVER v1.3 — ĐANG CHẠY TẠI http://127.0.0.1:8090")
    print("👔 Chế độ Mặc định: Làm việc 1-1 với Giám đốc Điều hành")
    print("=" * 70)
    uvicorn.run(app, host="0.0.0.0", port=8090)
