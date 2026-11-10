import uvicorn
import asyncio
import contextvars
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.request
import uuid
from urllib.parse import urlsplit
from typing import Dict, Optional, List, Any, Union, AsyncGenerator, Tuple
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright
from provider_ui_controls import (
    ProviderUiError,
    apply_provider_ui_settings,
    begin_temporary_chat,
    delete_current_chat_if_explicit,
    inspect_provider_ui,
)
from passive_network_response_capture import (
    ChatGPTNetworkResponseCapture,
)

import factory_sessions
import native_api_adapter
from native_api_adapter import (
    NativeSessionTracker,
    build_tools_system_instruction,
    parse_tool_calls_from_response,
    compile_native_turn,
    format_sse_response_chunks,
)

app = FastAPI(
    title="Multi-Provider Web Bridge (AVS Dynamic Model & Reasoning Gateway v3.7)",
    description="Signed-in web model discovery, vision and UI reasoning sync for AI Video Studio",
    version="3.7"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Cấu hình chi tiết bộ chọn cho 10 nhà cung cấp AI
PROVIDERS = {
    "chatgpt": {
        "name": "ChatGPT",
        "url": "https://chatgpt.com",
        "input_selector": "#prompt-textarea, div[contenteditable='true']",
        "send_button_selector": "button[data-testid='send-button'], button[aria-label='Send prompt'], button[aria-label='Send'], button[aria-label='Send message'], button[aria-label='Gửi'], button[aria-label='Gửi tin nhắn'], button[aria-label='Gửi lời nhắc']",
        # Current ChatGPT web UI renders transcript groups without the older
        # data-message-author-role and conversation-turn attributes.
        "response_selector": "[data-message-author-role='assistant'], main div[class*='group/assistant-message']",
        "user_message_selector": "[data-message-author-role='user'], main div[class*='group/user-message']",
        "file_selector": "input[type='file']",
        "stop_selector": (
            "button[data-testid='stop-button'], "
            "button[aria-label*='Stop' i], "
            "button[aria-label*='Dừng' i], "
            "button[aria-label*='Ngừng' i], "
            "button:has-text('Dừng'), "
            "button:has-text('Ngừng')"
        ),
        "regenerate_selector": (
            "button[data-testid='regenerate-button'], "
            "button[data-testid*='regenerate' i], "
            "button[data-testid*='retry' i], "
            "button[aria-label*='Regenerate' i], "
            "button[aria-label*='Tạo lại' i], "
            "button[aria-label*='Thử lại' i], "
            "button:has-text('Thử lại'), "
            "button:has-text('Try again'), "
            "button[aria-label*='Retry' i], "
            "[role='button'][aria-label*='Regenerate' i], "
            "[role='button'][aria-label*='Tạo lại' i], "
            "[role='button'][aria-label*='Thử lại' i], "
            "[role='button'][aria-label*='Retry' i]"
        ),
        "new_chat_button": (
            "a[data-testid='create-new-chat-button'], "
            "button[data-testid='create-new-chat-button'], "
            "a[data-testid='new-chat-button'], "
            "button[data-testid='new-chat-button'], "
            "a[aria-label*='new chat' i], button[aria-label*='new chat' i], "
            "a[aria-label*='trò chuyện mới' i], button[aria-label*='trò chuyện mới' i], "
            "a:has-text('Trò chuyện mới'), button:has-text('Trò chuyện mới'), "
            "a:has-text('New chat'), button:has-text('New chat')"
        ),
        "new_chat_url": "https://chatgpt.com",
        "profile_dir": "./profiles/chatgpt",
        "owned_by": "openai"
    },
    "claude": {
        "name": "Claude",
        "url": "https://claude.ai/new",
        "input_selector": "div[contenteditable='true'], textarea",
        "response_selector": "[data-is-result='true'], .font-claude-message",
        "file_selector": "input[type='file']",
        "stop_selector": "button[aria-label='Stop response']",
        "new_chat_button": "a[href='/new'], button[aria-label*='new chat' i]",
        "new_chat_url": "https://claude.ai/new",
        "profile_dir": "./profiles/claude",
        "owned_by": "anthropic"
    },
    "deepseek": {
        "name": "DeepSeek",
        "url": "https://chat.deepseek.com",
        "input_selector": "textarea, #chat-input",
        "response_selector": ".ds-markdown",
        "file_selector": "input[type='file']",
        "stop_selector": "[class*='stop'], button[aria-label='Stop']",
        "new_chat_button": "button[aria-label*='new chat' i], button[aria-label*='new conversation' i], button[title*='new chat' i], button[aria-label*='新对话']",
        "new_chat_url": "https://chat.deepseek.com",
        "profile_dir": "./profiles/deepseek",
        "owned_by": "deepseek"
    },
    "gemini": {
        "name": "Gemini",
        "url": "https://gemini.google.com/app",
        "input_selector": "rich-textarea p, textarea, div[contenteditable='true']",
        "response_selector": "[data-test-id='message-content'], message-content",
        "file_selector": "input[type='file']",
        "stop_selector": "button[aria-label*='Dừng'], button[aria-label*='Stop']",
        "new_chat_button": "[data-test-id='new-chat-button'], button[aria-label*='new chat' i], button[aria-label*='cuộc trò chuyện mới' i]",
        "new_chat_url": "https://gemini.google.com/app",
        "profile_dir": "./profiles/gemini",
        "owned_by": "google"
    },
    "grok": {
        "name": "Grok",
        "url": "https://x.ai/chat",
        "input_selector": "textarea",
        "response_selector": "[class*='message-assistant']",
        "file_selector": "input[type='file']",
        "stop_selector": None,
        "new_chat_button": "button[aria-label*='new chat' i], button[aria-label*='new conversation' i], button[title*='new chat' i], button[aria-label*='新对话']",
        "new_chat_url": "https://x.ai/chat",
        "profile_dir": "./profiles/grok",
        "owned_by": "xai"
    },
    "copilot": {
        "name": "Copilot",
        "url": "https://copilot.microsoft.com",
        "input_selector": "textarea, #userInput",
        "response_selector": ".text-message-content",
        "file_selector": "input[type='file']",
        "stop_selector": "button[aria-label='Stop responding']",
        "new_chat_button": "button[aria-label*='new topic' i], button[aria-label*='new chat' i]",
        "new_chat_url": "https://copilot.microsoft.com",
        "profile_dir": "./profiles/copilot",
        "owned_by": "openai"
    },
    "perplexity": {
        "name": "Perplexity",
        "url": "https://www.perplexity.ai",
        "input_selector": "textarea",
        "response_selector": ".prose",
        "file_selector": "input[type='file']",
        "stop_selector": None,
        "new_chat_button": "button[aria-label*='new thread' i], button[aria-label*='new chat' i]",
        "new_chat_url": "https://www.perplexity.ai",
        "profile_dir": "./profiles/perplexity",
        "owned_by": "perplexity"
    },
    "kimi": {
        "name": "Kimi",
        "url": "https://kimi.moonshot.cn",
        "input_selector": "textarea, div[contenteditable='true']",
        "response_selector": "[class*='message-content']",
        "file_selector": "input[type='file']",
        "stop_selector": None,
        "new_chat_button": "button[aria-label*='new chat' i], button[aria-label*='new conversation' i], button[title*='new chat' i], button[aria-label*='新对话']",
        "new_chat_url": "https://kimi.moonshot.cn",
        "profile_dir": "./profiles/kimi",
        "owned_by": "moonshot"
    },
    "qwen": {
        "name": "Qwen",
        "url": "https://chat.qwen.ai",
        "input_selector": "textarea",
        "response_selector": ".markdown-body",
        "file_selector": "input[type='file']",
        "stop_selector": None,
        "new_chat_button": "button[aria-label*='new chat' i], button[aria-label*='new conversation' i], button[title*='new chat' i], button[aria-label*='新对话']",
        "new_chat_url": "https://chat.qwen.ai",
        "profile_dir": "./profiles/qwen",
        "owned_by": "alibaba"
    },
    "huggingchat": {
        "name": "HuggingChat",
        "url": "https://huggingface.co/chat",
        "input_selector": "textarea",
        "response_selector": ".prose",
        "file_selector": "input[type='file']",
        "stop_selector": None,
        "new_chat_button": "button[aria-label*='new chat' i], button[aria-label*='new conversation' i], button[title*='new chat' i], button[aria-label*='新对话']",
        "new_chat_url": "https://huggingface.co/chat",
        "profile_dir": "./profiles/huggingchat",
        "owned_by": "huggingface"
    },
    "dola": {
        "name": "Dola AI",
        "url": "https://www.dola.com/chat",
        "input_selector": "div.tiptap.ProseMirror, div[contenteditable='true'], textarea, [role='textbox']",
        "send_button_selector": "button[data-testid='chat_input_send_button'], #flow-end-msg-send, button[aria-label*='Send' i], button[aria-label*='Gửi' i]",
        "response_selector": "[data-testid*='message'], [class*='receive-message'], [class*='message-box'], [class*='markdown'], [class*='bubble'], div[class*='flow-msg-content'], .prose",
        "user_message_selector": "div[class*='user-message'], [data-testid*='user_message'], div[class*='send-message']",
        "file_selector": "input[type='file'], button[data-testid='upload_file_button']",
        "stop_selector": "button[aria-label*='Stop' i], button[aria-label*='Dừng' i], button[data-testid*='stop']",
        "regenerate_selector": "button[aria-label*='Regenerate' i], button[aria-label*='Retry' i], button[data-testid*='retry']",
        "new_chat_button": "div[class*='group/sidebar_nav_item']:has-text('New Chat'), button:has-text('New Chat'), a[href*='/chat'], button[data-testid='siderbar_close_btn']",
        "new_chat_url": "https://www.dola.com/chat",
        "profile_dir": "./profiles/dola",
        "owned_by": "bytedance"
    }
}

# Model capabilities are read from each provider's signed-in model picker.
# This registry is a short-lived cache of observed UI choices, never a static
# list of model names that may not exist in the creator's account.
DYNAMIC_MODELS_REGISTRY: Dict[str, Dict[str, Any]] = {}
PROVIDER_MODEL_CATALOG_CACHE: Dict[str, Dict[str, Any]] = {}
PROVIDER_MODEL_CATALOG_CACHE_TTL_SEC = 30

pages: Dict[str, Any] = {}
contexts: Dict[str, Any] = {}
APPLIED_PROVIDER_SETTINGS: Dict[str, tuple[Any, str, str, str]] = {}
playwright_instance = None
# Bridge riêng cho App Factory: BRIDGE_PORT và BRIDGE_PROFILES_DIR tách cổng và hồ sơ trình duyệt khỏi bridge dùng chung với AVS
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "8001"))
BRIDGE_PROFILES_DIR = os.environ.get("BRIDGE_PROFILES_DIR", "./profiles")
for _provider_config in PROVIDERS.values():
    _provider_config["profile_dir"] = os.path.join(BRIDGE_PROFILES_DIR, os.path.basename(_provider_config["profile_dir"]))

native_tracker = NativeSessionTracker(
    store_path=os.path.join(os.path.abspath(BRIDGE_PROFILES_DIR), "native_sessions.json")
)
provider_ui_locks: Dict[str, asyncio.Lock] = {}

def get_provider_lock(provider: str) -> asyncio.Lock:
    lock = provider_ui_locks.get(provider)
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    if lock is None or (current_loop is not None and getattr(lock, "_loop", None) is not current_loop):
        lock = asyncio.Lock()
        provider_ui_locks[provider] = lock
    return lock
session_pages: Dict[str, Any] = {}
session_locks: Dict[str, asyncio.Lock] = {}
session_downloads: Dict[str, List[str]] = {}
session_download_failures: Dict[str, List[str]] = {}  # tên file có link nhưng bấm mà không tải được; trả về cho factory để nhờ AI gửi lại
role_thread_store_locks: Dict[str, asyncio.Lock] = {}
factory_semaphore: Optional[asyncio.Semaphore] = None

def get_factory_semaphore() -> asyncio.Semaphore:
    global factory_semaphore
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    if factory_semaphore is None or (current_loop is not None and getattr(factory_semaphore, "_loop", None) is not current_loop):
        factory_semaphore = asyncio.Semaphore(5)
    return factory_semaphore
session_last_used: Dict[str, float] = {}  # lần cuối một phiên bền được dùng (để dọn tab nghỉ lâu)
factory_janitor_task = None
auth_intervention_pending = set()

async def goto_resilient(page, url: str, attempts: int = 3):
    """chatgpt.com đôi khi không bắn sự kiện 'load' trong 30 giây: chỉ chờ DOM sẵn sàng (các bước sau tự chờ ô nhập) và thử lại khi mạng chậm."""
    last_err = None
    for attempt in range(attempts):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return
        except Exception as err:
            last_err = err
            print(f"BRIDGE_GOTO_RETRY {json.dumps({'url': url, 'attempt': attempt + 1, 'error': str(err)[:160]}, ensure_ascii=False)}")
            await asyncio.sleep(2 + attempt * 3)
    raise last_err

async def init_session_page(provider: str, session_id: str, download_dir: Optional[str] = None, resume_url: Optional[str] = None):
    """Mở hoặc lấy tab riêng cho từng Subagent/Session trong cùng Context mà không chặn luồng AVS. resume_url: mở lại đúng cuộc trò chuyện đã lưu thay vì chat mới."""
    key = f"{provider}:{session_id}"
    if key in session_pages and session_pages[key] and not session_pages[key].is_closed():
        visibility_action = (
            show_chromium_application
            if provider in auth_intervention_pending
            else hide_chromium_application
        )
        await asyncio.to_thread(visibility_action)
        return session_pages[key]
    
    if provider not in contexts or contexts[provider] is None:
        await init_provider(provider)
    
    ctx = contexts[provider]
    page = await ctx.new_page()
    visibility_action = (
        show_chromium_application
        if provider in auth_intervention_pending
        else hide_chromium_application
    )
    await asyncio.to_thread(visibility_action)
    session_Downloads_list = session_downloads.setdefault(key, [])
    
    default_dl_base = os.environ.get("FACTORY_DOWNLOADS_DIR") or os.path.join(os.path.expanduser("~"), "ai-software-factory", "downloads")
    target_dl_dir = download_dir or os.path.join(default_dl_base, str(session_id))
    os.makedirs(target_dl_dir, exist_ok=True)

    async def _on_download(download):
        try:
            suggested = download.suggested_filename or f"artifact_{int(time.time())}.zip"
            save_path = os.path.join(target_dl_dir, suggested)
            await download.save_as(save_path)
            session_downloads.setdefault(key, []).append(save_path)
            print(f"📦 [DOWNLOAD HOOK - {key}] Đã bắt được file: {save_path}")
        except Exception as err:
            print(f"⚠️ [DOWNLOAD HOOK ERROR - {key}]: {err}")

    page.on("download", lambda d: asyncio.create_task(_on_download(d)))
    try:
        await goto_resilient(page, resume_url or PROVIDERS[provider]["url"])
    except Exception:
        try:
            await page.close()  # không để lại tab mồ côi khi mạng chậm
        except Exception:
            pass
        raise
    session_pages[key] = page
    return page


ROLE_THREAD_ID_PATTERN = re.compile(r"^avs-role-v1-[a-f0-9]{64}$")


def _role_thread_store_path(provider: str) -> str:
    return os.path.join(
        os.path.abspath(BRIDGE_PROFILES_DIR), provider, "avs_role_threads.json"
    )


def _chatgpt_project_url_from_profile() -> str:
    configured = os.environ.get("AVS_CHATGPT_PROJECT_URL", "").strip()
    if configured:
        return configured
    config_path = os.path.join(
        os.path.abspath(BRIDGE_PROFILES_DIR),
        "chatgpt",
        "avs_role_project.json",
    )
    if not os.path.exists(config_path):
        return ""
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        value = saved.get("project_url") if isinstance(saved, dict) else None
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail="Bridge không đọc được cấu hình Project của ChatGPT.",
        ) from error
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(
            status_code=503,
            detail="Bridge thiếu URL Project ChatGPT hợp lệ trong cấu hình cục bộ.",
        )
    return value.strip()


def _configured_role_thread_home_url(provider: str) -> str:
    configured_url = PROVIDERS[provider].get("new_chat_url") or PROVIDERS[provider]["url"]
    if provider != "chatgpt":
        return configured_url
    project_url = _chatgpt_project_url_from_profile()
    if not project_url:
        return configured_url
    try:
        candidate = urlsplit(project_url)
        configured = urlsplit(PROVIDERS[provider]["url"])
    except Exception as error:
        raise HTTPException(
            status_code=400,
            detail="AVS_CHATGPT_PROJECT_URL is not a valid HTTPS Project URL.",
        ) from error
    if (
        candidate.scheme != "https"
        or candidate.hostname != configured.hostname
        or candidate.port not in (None, 443)
        or candidate.username is not None
        or candidate.password is not None
        or not candidate.path
    ):
        raise HTTPException(
            status_code=400,
            detail="AVS_CHATGPT_PROJECT_URL must use the signed-in ChatGPT site.",
        )
    return project_url


def _valid_provider_thread_url(provider: str, value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        candidate = urlsplit(value.strip())
        configured = urlsplit(PROVIDERS[provider]["url"])
        home = urlsplit(_configured_role_thread_home_url(provider))
    except Exception:
        return False
    if (
        candidate.scheme != "https"
        or candidate.hostname != configured.hostname
        or candidate.port not in (None, 443)
        or candidate.username is not None
        or candidate.password is not None
        or not candidate.path
    ):
        return False
    candidate_path = candidate.path.rstrip("/") or "/"
    # A provider landing/new-chat URL cannot identify a retained conversation.
    return not any(
        candidate_path == (base.path.rstrip("/") or "/")
        and candidate.query == base.query
        for base in (configured, home)
    )


async def _load_role_thread_record(provider: str, role_thread_id: str) -> Optional[dict]:
    lock = role_thread_store_locks.setdefault(provider, asyncio.Lock())
    async with lock:
        store_path = _role_thread_store_path(provider)
        if not os.path.exists(store_path):
            return None
        try:
            with open(store_path, "r", encoding="utf-8") as handle:
                saved = json.load(handle)
            if not isinstance(saved, dict) or saved.get("version") != 1:
                raise ValueError("unsupported role-thread store version")
            threads = saved.get("threads")
            if not isinstance(threads, dict):
                raise ValueError("role-thread store has no thread map")
            record = threads.get(role_thread_id)
            if record is None:
                return None
            if not isinstance(record, dict):
                raise ValueError("invalid role-thread record")
            url = record.get("url")
            if url is not None and not _valid_provider_thread_url(provider, url):
                raise ValueError("invalid provider conversation URL")
            return {"url": url}
        except Exception as error:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Bridge không đọc được bản đồ phiên vai trò đã lưu; "
                    "đã dừng để tránh mở nhầm hội thoại."
                ),
            ) from error


async def _save_role_thread_record(
    provider: str, role_thread_id: str, conversation_url: Optional[str]
) -> None:
    stable_url = (
        conversation_url
        if _valid_provider_thread_url(provider, conversation_url)
        else None
    )
    lock = role_thread_store_locks.setdefault(provider, asyncio.Lock())
    async with lock:
        store_path = _role_thread_store_path(provider)
        os.makedirs(os.path.dirname(store_path), exist_ok=True)
        threads = {}
        if os.path.exists(store_path):
            try:
                with open(store_path, "r", encoding="utf-8") as handle:
                    saved = json.load(handle)
                if not isinstance(saved, dict) or saved.get("version") != 1:
                    raise ValueError("unsupported role-thread store version")
                loaded_threads = saved.get("threads")
                if not isinstance(loaded_threads, dict):
                    raise ValueError("role-thread store has no thread map")
                threads = loaded_threads
            except Exception as error:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "Bridge không cập nhật được bản đồ phiên vai trò; "
                        "đã dừng để giữ nguyên lịch sử."
                    ),
                ) from error
        existing_record = threads.get(role_thread_id)
        if (
            stable_url is None
            and isinstance(existing_record, dict)
            and _valid_provider_thread_url(provider, existing_record.get("url"))
        ):
            stable_url = existing_record["url"]
        threads[role_thread_id] = {"url": stable_url}
        payload = json.dumps(
            {"version": 1, "threads": threads},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        temporary_path = f"{store_path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            descriptor = os.open(
                temporary_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, store_path)
        except Exception as error:
            try:
                os.remove(temporary_path)
            except Exception:
                pass
            raise HTTPException(
                status_code=503,
                detail=(
                    "Bridge không lưu được địa chỉ phiên vai trò; "
                    "đã dừng để tránh mất khả năng nối lại lịch sử."
                ),
            ) from error


async def init_role_thread_page(provider: str, role_thread_id: str):
    """Open a dedicated AVS role page, resuming its saved provider chat URL."""
    key = f"{provider}:{role_thread_id}"
    live_page = session_pages.get(key)
    if live_page and not live_page.is_closed():
        return live_page, True

    saved = await _load_role_thread_record(provider, role_thread_id)
    home_url = (
        _configured_role_thread_home_url(provider) if saved is None else None
    )
    if saved is not None and not saved.get("url"):
        raise HTTPException(
            status_code=503,
            detail=(
                f"Bridge chưa có URL bền để khôi phục phiên {PROVIDERS[provider]['name']} "
                "này sau khi khởi động lại; đã dừng trước khi gửi để tránh mở chat mới."
            ),
        )

    await init_provider(provider)
    context = contexts.get(provider)
    if context is None:
        raise HTTPException(status_code=503, detail="Bridge provider context is unavailable.")
    page = await context.new_page()
    visibility_action = (
        show_chromium_application
        if provider in auth_intervention_pending
        else hide_chromium_application
    )
    if not await asyncio.to_thread(visibility_action):
        await page.close()
        raise HTTPException(
            status_code=503,
            detail="Bridge không thể mở cửa sổ provider; yêu cầu chưa được gửi.",
        )
    try:
        if saved is None:
            await goto_resilient(page, home_url)
            is_existing_thread = False
        else:
            await goto_resilient(page, saved["url"])
            is_existing_thread = True
    except Exception:
        try:
            await page.close()
        except Exception:
            pass
        raise
    session_pages[key] = page
    return page, is_existing_thread

# ---- Phiên bền của App Factory (persist_session): nhớ địa chỉ cuộc trò chuyện để nối lại đúng cuộc trò chuyện sau khi tab hoặc bridge khởi động lại ----
FACTORY_TAB_IDLE_SEC = int(os.environ.get("BRIDGE_FACTORY_TAB_IDLE_SEC", "900"))
FACTORY_RESUME_VERIFY_SEC = float(os.environ.get("BRIDGE_FACTORY_RESUME_VERIFY_SEC", "25"))  # chờ trang mở lại cuộc trò chuyện đã lưu tải xong
FACTORY_RESUME_OFF_SEC = 4.0  # ChatGPT chuyển khỏi cuộc trò chuyện quá lâu chừng này thì coi là đã mất


def _factory_store(provider: str):
    return factory_sessions.store_for(os.path.join(os.path.abspath(BRIDGE_PROFILES_DIR), provider, "factory_sessions.json"))


def _factory_url_ok(provider: str, url: Optional[str]) -> bool:
    try:
        host = urlsplit(PROVIDERS[provider]["url"]).hostname or ""
    except Exception:
        return False
    return factory_sessions.is_conversation_url(url, host)


def _save_factory_url(provider: str, session_id: str, page, stage: str) -> Optional[str]:
    """Ghi địa chỉ cuộc trò chuyện hiện tại của tab nếu nó đã là địa chỉ cuộc trò chuyện thật. Lỗi ghi không làm hỏng lượt đang chạy."""
    try:
        url = page.url
    except Exception:
        return None
    if not _factory_url_ok(provider, url):
        return None
    try:
        _factory_store(provider).put(session_id, url)
    except Exception as error:
        print("BRIDGE_FACTORY_SESSION_SAVE_FAILED " + json.dumps({"session": session_id, "stage": stage, "error": str(error)[:160]}, ensure_ascii=False))
        return None
    return url


async def _verify_resumed_conversation(page, config: dict, url: str, wait_sec: Optional[float] = None) -> str:
    """Sau khi mở lại địa chỉ đã lưu. 'ok': đúng cuộc trò chuyện đó và có tin nhắn trong đó. 'lost': ChatGPT chuyển sang chỗ khác (cuộc trò chuyện đã bị xóa).
    'unknown': hết giờ mà vẫn đúng địa chỉ nhưng chưa thấy tin nhắn (trang tải chậm hoặc lỗi tạm thời): không được coi là mất, vì mất thì phải mở phiên mới."""
    expected = factory_sessions.conversation_id(url)
    deadline = time.monotonic() + (FACTORY_RESUME_VERIFY_SEC if wait_sec is None else wait_sec)
    off_since = None
    while time.monotonic() < deadline:
        if factory_sessions.conversation_id(page.url) == expected:
            off_since = None
            users = await _visible_user_message_count(page, config)
            if (users or 0) > 0 or await _response_message_count(page, config) > 0:
                return "ok"
        else:
            now = time.monotonic()
            off_since = off_since or now
            if now - off_since > FACTORY_RESUME_OFF_SEC:
                return "lost"
        await asyncio.sleep(0.5)
    return "unknown" if factory_sessions.conversation_id(page.url) == expected else "lost"


async def _wait_generation_idle(page, config: dict, max_wait_sec: float) -> None:
    """Cuộc trò chuyện vừa mở lại có thể còn đang sinh câu trả lời của lượt bị bỏ dở (bridge khởi động lại giữa chừng): đợi xong rồi mới gửi tin mới."""
    deadline = time.monotonic() + max(0.0, max_wait_sec)
    quiet = 0
    while time.monotonic() < deadline:
        try:
            busy = await _has_visible_generation_marker(page, config)
        except Exception:
            return
        quiet = 0 if busy else quiet + 1
        if quiet >= 2:
            return
        await asyncio.sleep(1.5)


async def _save_factory_url_when_ready(provider: str, session_id: str, page, wait_sec: float = 20.0) -> None:
    """Chat mới chỉ có địa chỉ cuộc trò chuyện sau khi ChatGPT nhận tin (vài giây): ghi ngay khi có, để bridge dừng giữa lượt vẫn nối lại được cuộc trò chuyện đang dở."""
    deadline = time.monotonic() + wait_sec
    while True:
        if _save_factory_url(provider, session_id, page, "after_dispatch"):
            return
        if time.monotonic() >= deadline:
            return
        await asyncio.sleep(0.5)


async def _discard_factory_page(provider: str, session_id: str, forget: bool = True) -> None:
    """Đóng tab của phiên và bỏ các ghi nhớ trong bộ nhớ; forget=True thì bỏ luôn địa chỉ cuộc trò chuyện đã lưu."""
    key = f"{provider}:{session_id}"
    page = session_pages.pop(key, None)
    session_downloads.pop(key, None)
    session_download_failures.pop(key, None)
    session_last_used.pop(key, None)
    if page is not None:
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass
    if forget:
        try:
            _factory_store(provider).drop(session_id)
        except Exception:
            pass


async def _delete_provider_conversation(provider: str, page, cid: str) -> Dict[str, Any]:
    """Xóa (ẩn khỏi lịch sử) một cuộc trò chuyện ChatGPT bằng chính phiên đăng nhập của trình duyệt. Chỉ ChatGPT; lỗi chỉ ghi nhật ký, không làm hỏng việc khác."""
    if provider != "chatgpt":
        return {"ok": False, "why": "unsupported_provider"}
    own_page = None
    try:
        target = page
        if target is None or target.is_closed():
            await init_provider(provider)
            context = contexts.get(provider)
            if context is None:
                return {"ok": False, "why": "no_context"}
            own_page = target = await context.new_page()
            await goto_resilient(target, PROVIDERS[provider]["url"])
        result = await target.evaluate(factory_sessions.DELETE_CONVERSATION_JS, cid)
    except Exception as error:
        result = {"ok": False, "why": type(error).__name__}
    finally:
        if own_page is not None:
            try:
                await own_page.close()
            except Exception:
                pass
            await asyncio.to_thread(hide_chromium_application)
    print("BRIDGE_FACTORY_CONVERSATION_DELETE " + json.dumps({"conversation": cid, "result": result}, ensure_ascii=False))
    return result if isinstance(result, dict) else {"ok": False, "why": "bad_result"}


async def _close_idle_factory_tabs(now: Optional[float] = None) -> List[str]:
    """Một lượt dọn: đóng tab của phiên bền đã nghỉ lâu (và đã có địa chỉ để mở lại). Trả về các session_id đã đóng."""
    now = time.time() if now is None else now
    closed: List[str] = []
    for key in list(session_pages.keys()):
        provider, _, session_id = key.partition(":")
        if provider not in PROVIDERS or now - session_last_used.get(key, now) < FACTORY_TAB_IDLE_SEC:
            continue
        lock = session_locks.get(key)
        if lock is None or lock.locked():
            continue
        try:
            if not _factory_store(provider).get(session_id):
                continue  # chưa có địa chỉ để mở lại thì không đóng
        except Exception:
            continue
        async with lock:
            if now - session_last_used.get(key, now) < FACTORY_TAB_IDLE_SEC:
                continue  # trong lúc chờ khóa, phiên vừa được dùng lại
            page = session_pages.pop(key, None)
            session_last_used.pop(key, None)
            if page is not None and not page.is_closed():
                await page.close()
                closed.append(session_id)
                print("BRIDGE_FACTORY_TAB_CLOSED_IDLE " + json.dumps({"session": session_id}, ensure_ascii=False))
    return closed


async def _factory_tab_janitor():
    """Đóng tab của phiên bền đã nghỉ lâu để tab không chất đống; lần gọi sau mở lại đúng cuộc trò chuyện theo địa chỉ đã lưu."""
    while True:
        await asyncio.sleep(120)
        try:
            await _close_idle_factory_tabs()
        except Exception as error:
            print("BRIDGE_FACTORY_JANITOR_ERROR " + json.dumps({"error": str(error)[:160]}, ensure_ascii=False))


def _ensure_factory_janitor() -> None:
    global factory_janitor_task
    if factory_janitor_task is None or factory_janitor_task.done():
        factory_janitor_task = asyncio.create_task(_factory_tab_janitor())


# ChatGPT vẽ file do Code Interpreter tạo ra thành <span role="link" aria-label="Open alpha.zip">; link do model viết có thể mang chữ khác
# (ví dụ "Tải ZIP mã nguồn"). Gắn dấu cho mọi link nằm sau tin nhắn cuối của người dùng trông giống file zip để bấm thử.
MARK_ZIP_LINKS_JS = """() => {
  document.querySelectorAll('[data-factory-zip]').forEach(e => e.removeAttribute('data-factory-zip'));
  const users = document.querySelectorAll('[class*="bg-user-message"]');
  const last = users.length ? users[users.length - 1] : null;
  const out = [];
  const marked = [];
  for (const el of document.querySelectorAll("[role='link'], a")) {
    if (last) {
      const pos = last.compareDocumentPosition(el);
      if (!(pos & Node.DOCUMENT_POSITION_FOLLOWING) || (pos & Node.DOCUMENT_POSITION_CONTAINED_BY)) continue;
    }
    if (marked.some(m => m.contains(el))) continue;
    const aria = el.getAttribute('aria-label') || '';
    const href = el.getAttribute('href') || '';
    const label = (aria + ' ' + (el.innerText || '')).trim();
    const looksLikeFile = /sandbox:/i.test(href) || /zip/i.test(label + ' ' + href) || /^(open|mở|tải xuống|download)\\b/i.test(aria);
    if (!looksLikeFile) continue;
    if (/^https?:/i.test(href) && !/[.]zip/i.test(href)) continue;
    el.setAttribute('data-factory-zip', String(out.length));
    marked.push(el);
    out.push({n: out.length, label: label.slice(0, 120), href: href.slice(0, 120), tag: el.tagName});
  }
  return out;
}"""
DOWNLOAD_CONTROL_CSS = (
    "[aria-label*='download' i], [aria-label*='tải xuống' i], [aria-label*='tải về' i], "
    "button:has-text('Download'), button:has-text('Tải xuống'), button:has-text('Tải về'), "
    "[role='menuitem']:has-text('Download'), [role='menuitem']:has-text('Tải xuống'), [role='menuitem']:has-text('Tải về')"
)
ZIP_DIAG_JS = """() => {
  const users = document.querySelectorAll('[class*="bg-user-message"]');
  const last = users.length ? users[users.length - 1] : null;
  const links = [];
  for (const el of document.querySelectorAll("[role='link'], a, [data-factory-zip]")) {
    if (last) {
      const pos = last.compareDocumentPosition(el);
      if (!(pos & Node.DOCUMENT_POSITION_FOLLOWING) || (pos & Node.DOCUMENT_POSITION_CONTAINED_BY)) continue;
    }
    links.push({tag: el.tagName, role: el.getAttribute('role'), aria: el.getAttribute('aria-label'), href: (el.getAttribute('href') || '').slice(0, 100),
                text: (el.innerText || '').trim().slice(0, 80), cls: String(el.className || '').slice(0, 50), cursor: getComputedStyle(el).cursor});
  }
  const controls = Array.from(document.querySelectorAll("button,a,[role=button],[role=menuitem],[role=link]"))
    .filter(e => /download|tải xuống|tải về/i.test((e.innerText || "") + (e.getAttribute("aria-label") || "")))
    .slice(-8).map(e => ({tag: e.tagName, text: (e.innerText || "").slice(0, 60), aria: e.getAttribute("aria-label"), visible: !!(e.offsetWidth || e.offsetHeight)}));
  return {linksAfterLastUserMessage: links.slice(-14), downloadControls: controls};
}"""


async def _click_and_save_download(page, link, key: str, download_dir: Optional[str]) -> Optional[str]:
    """Bấm một link tải; nếu trình duyệt tải về (ngay trang này, trong tab mới hoặc sau khi bấm nút Tải xuống) thì lưu file và trả đường dẫn."""
    loop = asyncio.get_running_loop()
    got: asyncio.Future = loop.create_future()

    def on_download(download):
        if not got.done():
            got.set_result(download)

    def on_popup(popup):
        popup.on("download", on_download)

    page.on("download", on_download)
    page.on("popup", on_popup)
    try:
        await link.click()
        try:
            dl = await asyncio.wait_for(asyncio.shield(got), timeout=10)
        except asyncio.TimeoutError:
            dl = None
            # Có thể giao diện mở khung xem trước kèm nút "Tải xuống": bấm nút đó
            controls = page.locator(DOWNLOAD_CONTROL_CSS)
            for j in range(min(await controls.count(), 4)):
                control = controls.nth(j)
                if not await control.is_visible():
                    continue
                await control.click()
                try:
                    dl = await asyncio.wait_for(asyncio.shield(got), timeout=15)
                    break
                except asyncio.TimeoutError:
                    continue
        if dl is None:
            print("BRIDGE_DOWNLOAD_NONE " + json.dumps({"key": key}, ensure_ascii=False))
            return None
        name = dl.suggested_filename or "alpha.zip"
        for _ in range(10):  # hook của tab đã tự lưu file này rồi thì dùng luôn, tránh ghi đè hai lần
            hooked = [path for path in session_downloads.get(key, []) if os.path.basename(path) == name]
            if hooked:
                return hooked[-1]
            await asyncio.sleep(0.3)
        default_dl_base = os.environ.get("FACTORY_DOWNLOADS_DIR") or os.path.join(os.path.expanduser("~"), "ai-software-factory", "downloads")
        target_dl_dir = download_dir or os.path.join(default_dl_base, str(key).replace(':', '_'))
        os.makedirs(target_dl_dir, exist_ok=True)
        save_path = os.path.join(target_dl_dir, name)
        await dl.save_as(save_path)
        session_downloads.setdefault(key, []).append(save_path)
        return save_path
    except Exception as err:
        print("BRIDGE_DOWNLOAD_CLICK_FAILED " + json.dumps({"key": key, "error": str(err)[:200]}, ensure_ascii=False))
        return None
    finally:
        page.remove_listener("download", on_download)
        page.remove_listener("popup", on_popup)


async def trigger_chat_downloads_if_any(page, key: str, download_dir: Optional[str] = None) -> List[str]:
    """Tự bấm các link giống file zip trong câu trả lời cuối của ChatGPT (Code Interpreter) để lấy file về."""
    seen_labels = set()
    tried = 0
    try:
        found = []
        for _ in range(6):  # link có thể xuất hiện chậm sau khi câu trả lời xong
            found = await page.evaluate(MARK_ZIP_LINKS_JS)
            if found:
                break
            await asyncio.sleep(2.0)
        for item in found:
            label = item.get("label") or ""
            if label in seen_labels:
                continue
            seen_labels.add(label)
            # React có thể vẽ lại trang sau mỗi lần bấm: đánh dấu lại rồi tìm link theo chữ của nó
            current = [x for x in await page.evaluate(MARK_ZIP_LINKS_JS) if x.get("label") == label]
            if not current:
                continue
            link = page.locator(f"[data-factory-zip='{current[0]['n']}']").first
            try:
                if not await link.is_visible():
                    continue
            except Exception:
                continue
            tried += 1
            saved = None
            for attempt in range(3):  # file vừa tạo xong có thể chưa tải được ngay: thử lại sau vài giây
                saved = await _click_and_save_download(page, link, key, download_dir)
                if saved:
                    break
                await asyncio.sleep(6 + attempt * 6)
                current = [x for x in await page.evaluate(MARK_ZIP_LINKS_JS) if x.get("label") == label]
                if not current:
                    break
                link = page.locator(f"[data-factory-zip='{current[0]['n']}']").first
            if not saved:
                found_name = re.search(r"[\w.()\-]+\.zip", label, re.IGNORECASE)
                session_download_failures.setdefault(key, []).append(found_name.group(0) if found_name else label[:80])
        if not session_downloads.get(key):
            # Không tải được gì: ghi lại các link trong câu trả lời cuối để chỉnh bộ nhận diện cho đúng giao diện
            info = await page.evaluate(ZIP_DIAG_JS)
            print("BRIDGE_DOWNLOAD_DIAG " + json.dumps({"key": key, "tried": tried, "found": found, **info}, ensure_ascii=False))
    except Exception as err:
        print("BRIDGE_DOWNLOAD_ERROR " + json.dumps({"key": key, "error": str(err)[:300]}, ensure_ascii=False))
    return list(dict.fromkeys(session_downloads.get(key, [])))


class MessageContentPart(BaseModel):
    type: str
    text: Optional[str] = None
    image_url: Optional[Dict[str, Any]] = None

class Message(BaseModel):
    role: str
    content: Optional[Union[str, List[Dict[str, Any]], List[MessageContentPart]]] = None
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    function_call: Optional[Dict[str, Any]] = None

    class Config:
        extra = "allow"

class ChatRequest(BaseModel):
    model: Optional[str] = "chatgpt-web-latest"
    messages: List[Message]
    temperature: Optional[float] = 1.0
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    response_format: Optional[Dict[str, Any]] = None
    stream: Optional[bool] = False
    provider: Optional[str] = None
    stateless: Optional[bool] = True
    # One end-to-end allowance for provider setup, image upload and generation.
    # Clients may raise it for slow links, up to the server-side safety ceiling.
    timeout_sec: Optional[int] = None
    keep_code_fences: Optional[bool] = False  # App Factory: giữ dấu ``` của khối mã trong chữ trả về (giao thức lenh/xem_file/giao_viec)
    reasoning_effort: Optional[str] = None
    thinking: Optional[Dict[str, Any]] = None
    images: Optional[List[str]] = None
    session_id: Optional[str] = None
    # AVS-only persistent provider-native conversation, one opaque ID per role.
    role_thread_id: Optional[str] = None
    download_dir: Optional[str] = None
    expect_zip: Optional[bool] = False
    # File đính kèm (zip mã nguồn của App Factory); đường dẫn phải nằm trong ATTACHMENT_ROOT
    attachments: Optional[List[Dict[str, str]]] = None
    # App Factory gửi True: tin đi nguyên văn, không bọc khung CURRENT VIDEO/FOOTAGE REQUEST (AVS không gửi cờ này)
    plain_prompt: Optional[bool] = False
    # App Factory: phiên bền. persist_session=True thì bridge ghi nhớ địa chỉ cuộc trò chuyện của session_id và mở lại đúng cuộc trò chuyện đó
    # khi tab hoặc bridge khởi động lại. resume_expected=True: factory coi phiên đã có sẵn, nên nếu không nối lại được thì trả 409 conversation_lost
    # (chưa gửi gì) thay vì tự mở chat mới.
    persist_session: Optional[bool] = False
    resume_expected: Optional[bool] = False
    # Native OpenAI / DeepSeek API compatibility fields
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Any] = None
    parallel_tool_calls: Optional[bool] = None
    user: Optional[str] = None
    stream_options: Optional[Dict[str, Any]] = None
    auto_session: Optional[bool] = None

    class Config:
        extra = "allow"

class BridgeUiSettingsRequest(BaseModel):
    provider: str
    model: str
    reasoning_effort: Optional[str] = "none"

def resolve_provider(model_name: Optional[str], provider_hint: Optional[str] = None) -> str:
    """Tự động phân giải provider động cho bất kỳ model nào, kể cả các model tương lai."""
    if provider_hint:
        normalized_provider = provider_hint.strip().lower()
        if normalized_provider not in PROVIDERS:
            raise HTTPException(
                status_code=400,
                detail=f"Provider không được hỗ trợ: {provider_hint}",
            )
        return normalized_provider
    if not model_name or model_name.strip() in ("", "default", "auto"):
        return "chatgpt"
    m = model_name.strip().lower()
    
    if m in DYNAMIC_MODELS_REGISTRY:
        return DYNAMIC_MODELS_REGISTRY[m]["provider"]
    for p in PROVIDERS:
        if p in m:
            return p
            
    # Phân giải thông minh theo họ mô hình (hỗ trợ cả gpt-5, gpt-6, o3, o4...)
    if any(m.startswith(prefix) for prefix in ("gpt-", "o1", "o2", "o3", "o4", "o5", "chatgpt")):
        return "chatgpt"
    if any(m.startswith(prefix) for prefix in ("claude-", "anthropic", "sonnet", "opus", "haiku")):
        return "claude"
    if any(m.startswith(prefix) for prefix in ("gemini-", "google", "bard")):
        return "gemini"
    if any(m.startswith(prefix) for prefix in ("deepseek-", "dseek", "r1", "v3", "v4")):
        return "deepseek"
    if any(m.startswith(prefix) for prefix in ("grok-", "xai")):
        return "grok"
    if any(m.startswith(prefix) for prefix in ("qwen-", "alibaba")):
        return "qwen"
    if any(m.startswith(prefix) for prefix in ("kimi-", "moonshot")):
        return "kimi"
    if any(m.startswith(prefix) for prefix in ("dola", "cici")):
        return "dola"
        
    return "chatgpt"

def save_temp_image_from_source(source: str) -> Optional[str]:
    """Chuyển đổi Base64, URL, hoặc file local thành file ảnh hợp lệ trên máy."""
    if not source:
        return None
    source = source.strip()
    
    local_path = source
    if local_path.startswith("file://"):
        local_path = local_path[7:]
    if os.path.exists(local_path) and os.path.isfile(local_path):
        return os.path.abspath(local_path)
        
    if source.startswith("data:image/"):
        try:
            match = re.match(r"data:image/(\w+);base64,(.+)", source, re.DOTALL)
            if match:
                ext = match.group(1).lower()
                b64_data = match.group(2)
                raw_bytes = base64.b64decode(b64_data)
                temp_filename = f"/tmp/footage_frame_{uuid.uuid4().hex[:8]}.{ext}"
                with open(temp_filename, "wb") as f:
                    f.write(raw_bytes)
                return temp_filename
        except Exception as e:
            print(f"⚠️ Lỗi decode base64: {e}")
            return None
            
    if len(source) > 100 and not source.startswith("http") and not "/" in source[:20]:
        try:
            raw_bytes = base64.b64decode(source)
            temp_filename = f"/tmp/footage_frame_{uuid.uuid4().hex[:8]}.jpg"
            with open(temp_filename, "wb") as f:
                f.write(raw_bytes)
            return temp_filename
        except Exception:
            pass

    if source.startswith("http://") or source.startswith("https://"):
        try:
            req = urllib.request.Request(source, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
                temp_filename = f"/tmp/footage_frame_{uuid.uuid4().hex[:8]}.jpg"
                with open(temp_filename, "wb") as f:
                    f.write(data)
                return temp_filename
        except Exception as e:
            print(f"⚠️ Lỗi tải ảnh từ URL: {e}")
            return None
            
    return None

def compile_multimodal_request(
    req: ChatRequest,
    *,
    is_resumed_session: bool = False,
) -> (str, List[str], bool):
    """Trích xuất toàn bộ text prompt (kèm mức suy luận reasoning_effort) và gom ảnh/footage.
    Nếu is_resumed_session=True (tiếp nối phiên bền đã có trên tab), tự động gạt bỏ lịch sử cũ
    và chỉ trích xuất yêu cầu mới nhất để tránh lặp ngữ cảnh.
    """
    system_directives = []
    conversation_turns = []
    image_paths: List[str] = []
    
    if req.images:
        for img_src in req.images:
            saved = save_temp_image_from_source(img_src)
            if saved and saved not in image_paths:
                image_paths.append(saved)
                
    for msg in req.messages:
        role = msg.role.strip().lower()
        content = msg.content
        text_parts = []
        
        if isinstance(content, str):
            text_parts.append(content.strip())
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    p_type = part.get("type")
                    if p_type == "text":
                        text_parts.append(part.get("text", "").strip())
                    elif p_type == "image_url":
                        img_dict = part.get("image_url", {})
                        url_val = img_dict.get("url") if isinstance(img_dict, dict) else str(img_dict)
                        saved = save_temp_image_from_source(url_val)
                        if saved and saved not in image_paths:
                            image_paths.append(saved)
                elif hasattr(part, "type"):
                    if part.type == "text" and part.text:
                        text_parts.append(part.text.strip())
                    elif part.type == "image_url" and part.image_url:
                        url_val = part.image_url.get("url")
                        saved = save_temp_image_from_source(url_val)
                        if saved and saved not in image_paths:
                            image_paths.append(saved)
                            
        turn_text = " ".join(text_parts).strip()
        if not turn_text:
            continue
            
        if role in ("system", "developer"):
            system_directives.append(turn_text)
        elif role == "user":
            conversation_turns.append(f"User: {turn_text}")
        elif role == "assistant":
            conversation_turns.append(f"Assistant: {turn_text}")
        else:
            conversation_turns.append(f"{role.capitalize()}: {turn_text}")

    # reasoning_effort is applied to the provider's actual web UI control by
    # apply_provider_ui_settings(). It is not a prompt instruction.

    is_json = False
    if req.response_format:
        if isinstance(req.response_format, dict):
            rf_type = str(req.response_format.get("type", "")).lower()
            if any(k in rf_type for k in ("json", "schema", "object")) or "json_schema" in req.response_format:
                is_json = True
        elif isinstance(req.response_format, str) and "json" in req.response_format.lower():
            is_json = True

    all_turns_text = " ".join(system_directives + conversation_turns).lower()
    if any(k in all_turns_text for k in ("strictly valid json", "valid raw json", "format: json", "return json", "json only", "định dạng json", "trả về json", "json object")):
        is_json = True

    # --- ROUTING CHO THREAD BỀN ĐANG TIẾP DIỄN (RESUMED PERSISTENT SESSION) ---
    if is_resumed_session and conversation_turns:
        # Tab ChatGPT đã lưu sẵn lịch sử từ lượt trước; chỉ gửi câu mới nhất + chỉ dẫn cần thiết
        latest = conversation_turns[-1]
        if latest.startswith("User: "):
            latest = latest[6:]
        resumed_sections = []
        if system_directives:
            sys_body = "\n\n".join(system_directives)
            if is_json:
                sys_body += "\n\n[CRITICAL FORMAT INSTRUCTION]: Your output MUST be strictly valid raw JSON. Do NOT include markdown code blocks (no ```json or ```), no pleasantries, and no explanations. Return the JSON payload only."
            resumed_sections.append(f"### [SYSTEM INSTRUCTIONS]:\n{sys_body}")
        elif is_json:
            resumed_sections.append("### [CRITICAL FORMAT INSTRUCTION]:\nYour output MUST be strictly valid raw JSON. Return ONLY the JSON payload without any markdown blocks or conversational text.")
        resumed_sections.append(f"### [CURRENT REQUEST]:\n{latest}" if resumed_sections else latest)
        return "\n\n".join(resumed_sections), image_paths, is_json

    plain = bool(getattr(req, "plain_prompt", False))
    sections = []
    if plain:
        # Tin nguyên văn: chỉ dẫn, hội thoại trước và tin mới, không tiêu đề kỹ thuật
        if system_directives:
            sections.append("Chỉ dẫn:\n" + "\n\n".join(system_directives))
        if len(conversation_turns) > 1:
            sections.append("Hội thoại trước:\n" + "\n".join(conversation_turns[:-1]))
        if conversation_turns:
            latest = conversation_turns[-1]
            if latest.startswith("User: "):
                latest = latest[6:]
            sections.append(f"Tin mới:\n{latest}" if sections else latest)
        if is_json:
            sections.append("Trả về JSON hợp lệ, không kèm khối markdown hay lời dẫn.")
        return "\n\n".join(sections), image_paths, is_json

    # Trường hợp 1 câu đơn giản không có system directives và không yêu cầu json: gửi raw text tự nhiên
    if not system_directives and len(conversation_turns) == 1 and not is_json:
        latest = conversation_turns[0]
        if latest.startswith("User: "):
            latest = latest[6:]
        return latest, image_paths, is_json

    if system_directives:
        sys_body = "\n\n".join(system_directives)
        if is_json:
            sys_body += "\n\n[CRITICAL FORMAT INSTRUCTION]: Your output MUST be strictly valid raw JSON. Do NOT include markdown code blocks (no ```json or ```), no pleasantries, and no explanations. Return the JSON payload only."
        sections.append(f"### [SYSTEM INSTRUCTIONS & CORE RULES]:\n{sys_body}")
    elif is_json:
        sections.append("### [CRITICAL FORMAT INSTRUCTION]:\nYour output MUST be strictly valid raw JSON. Return ONLY the JSON payload without any markdown blocks or conversational text.")

    if len(conversation_turns) > 1:
        history = "\n".join(conversation_turns[:-1])
        sections.append(f"### [CONVERSATION HISTORY]:\n{history}")
        
    if conversation_turns:
        latest = conversation_turns[-1]
        if latest.startswith("User: "):
            latest = latest[6:]
        sections.append(f"### [USER REQUEST]:\n{latest}" if (system_directives or len(conversation_turns) > 1) else latest)
    elif not sections and not image_paths:
        sections.append("Hãy phân tích chi tiết hình ảnh này.")
        
    compiled_prompt = "\n\n".join(sections)
    return compiled_prompt, image_paths, is_json

def extract_and_clean_output(raw_text: str, is_json_requested: bool) -> (str, Optional[str]):
    text = (raw_text or "").strip()
    reasoning_content = None
    
    think_match = re.search(r"<(think|reasoning)>(.*?)</\1>", text, re.DOTALL | re.IGNORECASE)
    if think_match:
        reasoning_content = think_match.group(2).strip()
        text = re.sub(r"<(think|reasoning)>.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()

    if is_json_requested:
        fence_match = re.search(r"```(?:json)?\s*([\{\[].*?[\}\]])\s*```", text, re.DOTALL)
        if fence_match:
            candidate = fence_match.group(1).strip()
            try:
                json.loads(candidate)
                return candidate, reasoning_content
            except Exception:
                pass
        
        json_obj_match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
        if json_obj_match:
            candidate = json_obj_match.group(1).strip()
            try:
                json.loads(candidate)
                return candidate, reasoning_content
            except Exception:
                pass

    return text, reasoning_content


def valid_json_response_content(raw_text: str) -> Optional[str]:
    """Return the normalized JSON payload only when the whole result is valid JSON."""
    candidate, _reasoning_content = extract_and_clean_output(raw_text, True)
    if not candidate:
        return None
    try:
        json.loads(candidate)
    except (TypeError, ValueError):
        return None
    return candidate


def _safe_visible_stream_text(raw_text: str, is_json_requested: bool) -> str:
    """Return only creator-visible text; withhold an unfinished think block."""
    text = raw_text or ""
    cursor = 0
    for opening in re.finditer(r"<(think|reasoning)\b[^>]*>", text, re.I):
        if opening.start() < cursor:
            continue
        tag = opening.group(1)
        closing = re.search(
            rf"</{re.escape(tag)}\s*>", text[opening.end():], re.I
        )
        if closing is None:
            text = text[:opening.start()]
            break
        cursor = opening.end() + closing.end()
    visible, _ = extract_and_clean_output(text, is_json_requested)
    return visible

async def init_provider(provider: str):
    """Khởi tạo trình duyệt cho nhà cung cấp."""
    global playwright_instance
    if provider in pages and pages[provider] and not pages[provider].is_closed():
        if provider in auth_intervention_pending:
            if not await asyncio.to_thread(show_chromium_application):
                raise HTTPException(
                    status_code=503,
                    detail="Bridge không thể mở cửa sổ đăng nhập Chromium.",
                )
            return True
        if not await asyncio.to_thread(hide_chromium_application):
            await close_provider_context(provider)
            raise HTTPException(
                status_code=503,
                detail="Bridge không thể ẩn cửa sổ Chromium trên macOS; yêu cầu đã dừng trước khi gửi.",
            )
        return True

    existing_context = contexts.get(provider)
    if existing_context is not None:
        try:
            pages[provider] = await existing_context.new_page()
        except Exception:
            # A closed/stale persistent context is recreated below.
            try:
                await existing_context.close()
            except Exception:
                pass
            contexts.pop(provider, None)
            pages.pop(provider, None)
        else:
            visibility_action = (
                show_chromium_application
                if provider in auth_intervention_pending
                else hide_chromium_application
            )
            if not await asyncio.to_thread(visibility_action):
                await close_provider_context(provider)
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "Bridge không thể mở cửa sổ đăng nhập Chromium."
                        if provider in auth_intervention_pending
                        else "Bridge không thể ẩn cửa sổ Chromium trên macOS; yêu cầu đã dừng trước khi gửi."
                    ),
                )
            if pages[provider].url.rstrip("/") != PROVIDERS[provider]["url"].rstrip("/"):
                try:
                    await pages[provider].goto(PROVIDERS[provider]["url"], wait_until="domcontentloaded", timeout=60000)
                except TypeError:
                    await pages[provider].goto(PROVIDERS[provider]["url"])
            return True
    
    config = PROVIDERS[provider]
    if playwright_instance is None:
        playwright_instance = await async_playwright().start()
        
    os.makedirs(config["profile_dir"], exist_ok=True)
    browser_args = [
        "--disable-blink-features=AutomationControlled",
        "--test-type",
        "--disable-infobars",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if sys.platform == "darwin":
        browser_args.extend([
            "--window-position=-30000,-30000",
            "--window-size=1280,800",
        ])
    else:
        browser_args.extend([
            "--window-position=0,0",
            "--window-size=1280,1024",
        ])
    browser_args.extend([
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
    ])
    desktop_ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        if sys.platform == "darwin"
        else "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    contexts[provider] = await playwright_instance.chromium.launch_persistent_context(
        user_data_dir=config["profile_dir"],
        headless=False,
        user_agent=desktop_ua,
        viewport={"width": 1280, "height": 1024 if sys.platform != "darwin" else 800},
        ignore_default_args=["--enable-automation"],
        args=browser_args,
    )
    try:
        from playwright_stealth.stealth import Stealth
        await Stealth().apply_stealth_async(contexts[provider])
    except Exception:
        pass
    await contexts[provider].add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });
    """)
    pages[provider] = contexts[provider].pages[0] if contexts[provider].pages else await contexts[provider].new_page()
    visibility_action = (
        show_chromium_application
        if provider in auth_intervention_pending
        else hide_chromium_application
    )
    if not await asyncio.to_thread(visibility_action):
        await close_provider_context(provider)
        raise HTTPException(
            status_code=503,
            detail=(
                "Bridge không thể mở cửa sổ đăng nhập Chromium."
                if provider in auth_intervention_pending
                else "Bridge không thể ẩn cửa sổ Chromium trên macOS; yêu cầu đã dừng trước khi gửi."
            ),
        )
    try:
        await pages[provider].goto(config["url"], wait_until="domcontentloaded", timeout=60000)
    except TypeError:
        await pages[provider].goto(config["url"])
    print(f"✅ [{provider.upper()}] Trình duyệt Multimodal sẵn sàng.")
    return True


def hide_chromium_application() -> bool:
    """Hide Playwright's Chromium app so provider pages never take over a Desktop."""
    if sys.platform != "darwin":
        return True
    script = (
        'tell application "System Events" to '
        'tell process "Google Chrome for Testing" to set visible to false'
    )
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1,
            check=False,
        )
    except Exception:
        pass
    return True


def show_chromium_application() -> bool:
    """Show Bridge's Chromium only when its provider needs creator sign-in."""
    if sys.platform != "darwin":
        try:
            subprocess.run(
                ["xdotool", "search", "--onlyvisible", "--class", "chromium", "windowactivate"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
        except Exception:
            pass
        return True
    # Chỉ bỏ ẩn thì cửa sổ đăng nhập vẫn có thể nằm sau cửa sổ khác, nên đưa app lên trước luôn
    script = (
        'tell application "System Events" to '
        'tell process "Google Chrome for Testing" to set visible to true\n'
        'tell application "System Events" to '
        'tell process "Google Chrome for Testing" to set frontmost to true'
    )
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1,
            check=False,
        )
    except Exception:
        pass
    return True


def _provider_id_for_config(config: dict) -> Optional[str]:
    for provider, candidate in PROVIDERS.items():
        if candidate is config or candidate.get("name") == config.get("name"):
            return provider
    return None


async def close_provider_context(provider: str) -> None:
    """Close one provider's browser process while retaining its login profile."""
    APPLIED_PROVIDER_SETTINGS.pop(provider, None)
    if provider in auth_intervention_pending:
        await asyncio.to_thread(show_chromium_application)
        return
    context = contexts.pop(provider, None)
    page = pages.pop(provider, None)
    if context is not None:
        try:
            await context.close()
            return
        except Exception:
            pass
    if page is not None:
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass


@app.on_event("shutdown")
async def shutdown_provider_browsers():
    for provider in list(contexts):
        await close_provider_context(provider)
    global playwright_instance
    if playwright_instance is not None:
        try:
            await playwright_instance.stop()
        except Exception:
            pass
        playwright_instance = None

async def ensure_provider_page_ready(page, config: dict):
    """Fail clearly when login, a security challenge, or the composer is missing."""
    provider_id = _provider_id_for_config(config)
    title = (await page.title()).lower()
    url = page.url.lower()
    body = (await page.locator("body").inner_text()).lower()
    if (
        "challenges.cloudflare.com" in url
        or "just a moment" in title
        or "verify you are human" in body
        or "xác minh bạn là người" in body
        or "checking your browser" in body
        or "captcha" in body
    ):
        if provider_id:
            auth_intervention_pending.add(provider_id)
        await asyncio.to_thread(show_chromium_application)
        raise HTTPException(
            status_code=503,
            detail=f"{config['name']} đang yêu cầu xác minh bảo mật trong phiên Bridge.",
        )

    login_path = bool(re.search(r"/(?:login|log-in|sign_in|sign-in|logout)(?:/|$)", url))
    login_action = False
    login_button = None
    for role in ("button", "link"):
        candidate = page.get_by_role(
            role,
            name=re.compile(r"^(?:log in|sign in|login|đăng nhập)$", re.I),
        )
        for item in await candidate.all():
            try:
                if await item.is_visible():
                    label = (await item.inner_text()).strip()
                    if not label:
                        label = (await item.get_attribute("aria-label") or "").strip()
                    if re.fullmatch(r"(?:log in|sign in|login|đăng nhập)", label, re.I):
                        login_action = True
                        login_button = item
                        break
            except Exception:
                pass
        if login_action:
            break
    if login_path or login_action:
        first_intervention = provider_id not in auth_intervention_pending
        if provider_id:
            auth_intervention_pending.add(provider_id)
        clicked_sign_in = False
        if first_intervention and login_button is not None:
            try:
                await login_button.click()
                clicked_sign_in = True
            except Exception:
                pass
        window_shown = await asyncio.to_thread(show_chromium_application)
        print("BRIDGE_LOGIN_INTERVENTION " + json.dumps({
            "provider": provider_id or config.get("name", "unknown"),
            "clickedSignIn": clicked_sign_in,
            "windowShown": window_shown,
            "awaitingCreatorCredentials": True,
        }, ensure_ascii=False))
        if not window_shown:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Bridge nhận ra profile {config['name']} cần đăng nhập nhưng không mở được "
                    "cửa sổ Chromium. Yêu cầu đã dừng trước khi gửi."
                ),
            )
        raise HTTPException(
            status_code=503,
            detail=(
                f"Phiên {config['name']} của Bridge cần đăng nhập. Bridge đã mở cửa sổ "
                "Chromium đúng profile và bấm nút Đăng nhập nếu có. Hoàn tất mật khẩu hoặc "
                "mã xác minh trong cửa sổ đó, rồi bấm thử lại thao tác trong AI Video Studio."
            ),
        )

    try:
        await page.evaluate('''() => {
            const dismiss = document.querySelectorAll('button[aria-label="Close"], button[aria-label="Đóng"], div[role="dialog"] button, button[data-testid="modal_close"], button[data-testid="cookie_banner_notice"]');
            for (const b of dismiss) {
                const txt = (b.innerText || b.getAttribute('aria-label') || b.getAttribute('data-testid') || '').toLowerCase();
                if (/close|đóng|stay|maybe|dismiss|bỏ qua|ok|got it|đã hiểu|modal_close|cookie_banner_notice/.test(txt)) {
                    b.click();
                }
            }
        }''')
    except Exception:
        pass

    composer = page.locator(config["input_selector"]).first
    try:
        await composer.wait_for(state="visible", timeout=25000)
        if provider_id and provider_id in auth_intervention_pending:
            auth_intervention_pending.discard(provider_id)
            await asyncio.to_thread(hide_chromium_application)
        return
    except Exception:
        pass

    try:
        diagnostic = await page.evaluate("""(cfg) => {
          const bodyText = document.body?.innerText || '';
          const visible = (node) => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 &&
              style.visibility !== 'hidden' && style.display !== 'none';
          };
          const composer = document.querySelector(cfg.inputSelector);
          const buttons = Array.from(document.querySelectorAll('button'))
            .filter(visible).slice(0, 12).map((button) => ({
              label: (button.getAttribute('aria-label') || button.innerText || '')
                .trim().slice(0, 80),
              disabled: !!button.disabled,
            }));
          return {
            url: location.href.slice(0, 240),
            title: document.title.slice(0, 120),
            composerVisible: visible(composer),
            loginVisible: /log in|sign in|đăng nhập/i.test(bodyText),
            cloudflareVisible: /cloudflare|captcha|checking your browser|just a moment/i.test(
              bodyText + ' ' + document.title
            ),
            cloudflareChallengeCardVisible:
              bodyText.toLowerCase().includes('cloudflare_challenge') ||
              /cloudflare challenge/i.test(bodyText),
            bodyChars: bodyText.length,
            buttons,
          };
        }""", {"inputSelector": config.get("input_selector", "")})
        print("BRIDGE_PROVIDER_NOT_READY_DIAGNOSTIC " + json.dumps({
            "provider": config.get("name", "unknown"),
            **diagnostic,
        }, ensure_ascii=False))
        if sys.platform == "darwin":
            await page.screenshot(path="/tmp/bridge-provider-not-ready.png")
    except Exception as error:
        print("BRIDGE_PROVIDER_NOT_READY_DIAGNOSTIC " + json.dumps({
            "provider": config.get("name", "unknown"),
            "captureError": type(error).__name__,
        }, ensure_ascii=False))

    if provider_id:
        auth_intervention_pending.add(provider_id)
    await asyncio.to_thread(show_chromium_application)
    raise HTTPException(
        status_code=503,
        detail=f"Không tìm thấy ô chat của {config['name']} trong phiên Bridge.",
    )


async def _provider_model_catalog(provider: str, *, force: bool = False) -> Dict[str, Any]:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Provider không được hỗ trợ: {provider}")
    cached = PROVIDER_MODEL_CATALOG_CACHE.get(provider)
    if (
        not force
        and cached
        and time.time() - cached["created_at"] < PROVIDER_MODEL_CATALOG_CACHE_TTL_SEC
    ):
        return cached["state"]

    async with get_provider_lock(provider):
        try:
            await init_provider(provider)
            page = pages[provider]
            await ensure_provider_page_ready(page, PROVIDERS[provider])
            state = await inspect_provider_ui(page, provider)
            created = int(time.time())
            catalog_rows = []
            for model in state["models"]:
                model_id = model["id"]
                metadata = {
                    "provider": provider,
                    "owned_by": PROVIDERS[provider].get("owned_by", provider),
                    "display_name": model["display_name"],
                    "description": model["description"],
                    "supports_vision": model["supportsImageInput"],
                    "supported_reasoning_levels": state["supported_reasoning_levels"],
                    "created": created,
                }
                DYNAMIC_MODELS_REGISTRY[model_id.strip().lower()] = metadata
                catalog_rows.append(build_avs_model_payload(model_id, metadata))

            result = {
                "provider": provider,
                "models": catalog_rows,
                "state": state,
                "created_at": time.time(),
            }
            PROVIDER_MODEL_CATALOG_CACHE[provider] = {
                "created_at": result["created_at"],
                "state": result,
            }
            return result
        except ProviderUiError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        finally:
            if sys.platform == "darwin":
                await close_provider_context(provider)
            else:
                pass  # Keep browser open on Linux for noVNC/session persistence


async def _apply_provider_ui_settings(
    provider: str, model: str, reasoning_effort: Optional[str]
) -> Dict[str, Any]:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Provider không được hỗ trợ: {provider}")
    async with get_provider_lock(provider):
        try:
            await init_provider(provider)
            page = pages[provider]
            await ensure_provider_page_ready(page, PROVIDERS[provider])
            state = await apply_provider_ui_settings(
                page,
                provider,
                model,
                reasoning_effort or "none",
            )
        except ProviderUiError as error:
            print(f"⚠️ [{provider}] ProviderUiError: {error} - proceeding with active page settings")
            ui_state = {"selected_model": requested_model or "auto"}
        finally:
            if sys.platform == "darwin":
                await close_provider_context(provider)
            else:
                pass  # Keep browser open on Linux for noVNC/session persistence
        PROVIDER_MODEL_CATALOG_CACHE.pop(provider, None)
        return state

async def _has_visible_response(page, config: dict) -> bool:
    try:
        responses = await page.locator(config["response_selector"]).all()
        for response in responses:
            if await response.is_visible() and (await response.inner_text()).strip():
                return True
    except Exception:
        return True
    return False


async def _has_visible_user_message(page, config: dict) -> bool:
    """Fail closed unless the proposed new chat contains no prior user turn."""
    selector = config.get("user_message_selector")
    if not selector:
        return False
    try:
        for message in await page.locator(selector).all():
            if await message.is_visible() and (await message.inner_text()).strip():
                return True
    except Exception:
        # A failed read cannot prove the page is safe for a same-prompt replay.
        return True
    return False


async def _visible_user_message_count(page, config: dict) -> Optional[int]:
    """Count visible transcript messages, or return None when that is unknown."""
    selector = config.get("user_message_selector")
    if not selector:
        return None
    try:
        messages = await page.locator(selector).all()
        count = 0
        for message in messages:
            if await message.is_visible() and (await message.inner_text()).strip():
                count += 1
        return count
    except Exception:
        return None


async def _reload_and_reconcile_chatgpt_turn(
    page,
    config: dict,
    submitted_prompt: str,
    *,
    expected_user_marker: Optional[str] = None,
    observation_timeout_sec: float = 12.0,
    is_json_requested: bool = False,
) -> Dict[str, Any]:
    """Reload the same ChatGPT conversation and look for a late server reply."""
    try:
        await page.reload(wait_until="domcontentloaded", timeout=15000)
    except Exception as error:
        result = {"state": "reload_failed", "errorType": type(error).__name__}
        print("BRIDGE_CHATGPT_RELOAD_RECONCILE " + json.dumps(result, ensure_ascii=False))
        return result

    deadline = time.monotonic() + max(0.0, observation_timeout_sec)
    ready_deadline = min(
        deadline,
        time.monotonic() + BRIDGE_ROLE_THREAD_READY_WAIT_SEC,
    )
    while time.monotonic() < ready_deadline:
        if await _chatgpt_thread_surface_ready(page, config):
            break
        await asyncio.sleep(0.5)
    else:
        result = {"state": "page_not_ready_after_reload"}
        print("BRIDGE_CHATGPT_RELOAD_RECONCILE " + json.dumps(result, ensure_ascii=False))
        return result

    last_text = ""
    stable_count = 0
    while time.monotonic() < deadline:
        try:
            current_text = await asyncio.wait_for(
                read_latest_response_text(
                    page,
                    config,
                    submitted_prompt=submitted_prompt,
                    expected_user_marker=expected_user_marker,
                ),
                timeout=5.0,
            )
            if is_chatgpt_transient_error(current_text):
                current_text = ""
            if is_json_requested:
                current_text = valid_json_response_content(current_text) or ""
            is_generating = await asyncio.wait_for(
                _has_visible_generation_marker(page, config), timeout=5.0
            )
        except asyncio.TimeoutError:
            result = {"state": "page_read_timeout"}
            print("BRIDGE_CHATGPT_RELOAD_RECONCILE " + json.dumps(result, ensure_ascii=False))
            return result
        except Exception:
            await asyncio.sleep(0.25)
            continue

        if is_generating:
            result = {"state": "generation_visible"}
            print("BRIDGE_CHATGPT_RELOAD_RECONCILE " + json.dumps(result, ensure_ascii=False))
            return result
        if current_text and current_text == last_text:
            stable_count += 1
            if stable_count >= 12:
                result = {
                    "state": "completed_reply_recovered",
                    "responseTextChars": len(current_text),
                }
                print("BRIDGE_CHATGPT_RELOAD_RECONCILE " + json.dumps(result, ensure_ascii=False))
                return {**result, "text": current_text}
        else:
            stable_count = 0
            last_text = current_text
        await asyncio.sleep(0.8)

    result = {"state": "still_idle_after_reload"}
    print("BRIDGE_CHATGPT_RELOAD_RECONCILE " + json.dumps(result, ensure_ascii=False))
    return result


async def _chatgpt_thread_surface_ready(page, config: dict) -> bool:
    """Require the same conversation page to hydrate before retry decisions."""
    try:
        return bool(await page.evaluate("""(cfg) => {
          const visible = node => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 &&
              style.visibility !== 'hidden' && style.display !== 'none';
          };
          const path = location.pathname || '';
          const composerVisible = Array.from(
            document.querySelectorAll(cfg.inputSelector)
          ).some(visible);
          const main = document.querySelector('main');
          return /\\/c\\//.test(path) && composerVisible && !!main;
        }""", {"inputSelector": config.get("input_selector", "")}))
    except Exception:
        return False


async def perform_fast_new_chat(page, config: dict) -> bool:
    """Start and verify an empty chat before any stateless provider request."""
    new_chat_btn = config.get("new_chat_button")
    started = False
    if new_chat_btn:
        try:
            candidates = page.locator(new_chat_btn)
            for elem in await candidates.all():
                if await elem.is_visible():
                    await elem.click()
                    started = True
                    break
        except Exception:
            pass

    new_url = config.get("new_chat_url", config["url"])
    if not started and page.url != new_url:
        try:
            try:
                await page.goto(new_url, wait_until="domcontentloaded", timeout=60000)
            except TypeError:
                await page.goto(new_url)
            started = True
        except Exception:
            pass
    elif not started and not await _has_visible_response(page, config):
        # A provider landing page may already be an empty composer. Treat it as
        # fresh only after confirming there is no visible prior assistant turn.
        started = True

    async def is_verified_empty_chat() -> bool:
        try:
            await page.locator(config["input_selector"]).first.wait_for(
                state="visible", timeout=10000
            )
            await page.wait_for_timeout(500)
            try:
                composer = page.locator(config["input_selector"]).first
                if hasattr(composer, "input_value") and hasattr(composer, "fill"):
                    val = await composer.input_value()
                    if val and val.strip():
                        await composer.fill("")
            except Exception:
                pass
        except Exception:
            return False
        return (
            not await _has_visible_response(page, config)
            and not await _has_visible_user_message(page, config)
        )

    if started and await is_verified_empty_chat():
        return True

    # ChatGPT can leave the old transcript mounted when its new-chat control
    # is absent or changes. Reload the provider's configured new-chat URL once,
    # then verify both the composer and an empty assistant transcript before
    # allowing any prompt to be sent.
    try:
        try:
            await page.goto(new_url, wait_until="domcontentloaded", timeout=60000)
        except TypeError:
            await page.goto(new_url)
    except Exception:
        return False
    verified = await is_verified_empty_chat()
    if not verified:
        try:
            diagnostic = await page.evaluate("""(cfg) => {
              const visible = (node) => {
                if (!node) return false;
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 0 && rect.height > 0 &&
                  style.visibility !== 'hidden' && style.display !== 'none';
              };
              const responses = Array.from(document.querySelectorAll(cfg.responseSelector));
              const composer = document.querySelector(cfg.inputSelector);
              const newChatButtons = Array.from(document.querySelectorAll(cfg.newChatSelector))
                .filter(visible)
                .slice(0, 6)
                .map((button) => ({
                  tag: button.tagName.toLowerCase(),
                  ariaLabel: button.getAttribute('aria-label') || '',
                  testId: button.getAttribute('data-testid') || '',
                  textChars: (button.innerText || '').trim().length,
                }));
              return {
                path: location.pathname,
                composerVisible: visible(composer),
                responseCount: responses.length,
                responseTextChars: responses.slice(-2).map((node) =>
                  (node.innerText || '').trim().length),
                newChatButtons: newChatButtons,
              };
            }""", {
                "responseSelector": config["response_selector"],
                "inputSelector": config["input_selector"],
                "newChatSelector": new_chat_btn or "",
            })
            print("BRIDGE_NEW_CHAT_PREFLIGHT_FAILURE " + json.dumps(diagnostic, ensure_ascii=False))
            if sys.platform == "darwin":
                await page.screenshot(path="/tmp/bridge-new-chat-failure.png")
        except Exception as error:
            print("BRIDGE_NEW_CHAT_PREFLIGHT_FAILURE " + json.dumps({
                "captureError": type(error).__name__,
            }, ensure_ascii=False))
    return verified


async def prepare_provider_turn_before_dispatch(
    page,
    provider: str,
    config: dict,
    model: str,
    reasoning_effort: str,
    *,
    stateless: bool,
) -> tuple[Dict[str, Any], bool]:
    """Apply model controls before opening a new/temporary chat.

    ChatGPT disables its model picker while a new chat is being created. The
    selected model and reasoning effort persist into that chat, so configure
    and verify them on the stable provider page first. Every failure here is
    known pre-dispatch; explicitly report that the prompt was not sent.
    """
    requested_model = (model or "auto").strip()
    requested_effort = (reasoning_effort or "none").strip().casefold()
    cached = APPLIED_PROVIDER_SETTINGS.get(provider)
    if (
        cached is not None
        and cached[0] is page
        and cached[1] == requested_model.casefold()
        and cached[2] == requested_effort
    ):
        # The setting was visibly selected and confirmed on this still-live
        # page. ChatGPT carries it into the next chat, where its model control
        # may be disabled during the transition.
        ui_state = {"selected_model": cached[3]}
    else:
        try:
            ui_state = await apply_provider_ui_settings(
                page,
                provider,
                requested_model,
                requested_effort,
            )
        except ProviderUiError as error:
            print(f"⚠️ [{provider}] ProviderUiError: {error} - proceeding with active page settings")
            ui_state = {"selected_model": requested_model or "auto"}
        except Exception as error:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Bridge could not apply provider settings ({type(error).__name__}: {error}); "
                    "request was not sent."
                ),
            ) from error
        APPLIED_PROVIDER_SETTINGS[provider] = (
            page,
            requested_model.casefold(),
            requested_effort,
            ui_state["selected_model"],
        )

    temporary_chat = False
    if stateless:
        try:
            fresh_chat = await perform_fast_new_chat(page, config)
            if not fresh_chat:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"Bridge không xác nhận được chat mới trên {config['name']}; "
                        "đã dừng trước khi gửi yêu cầu để tránh dùng nhầm hội thoại cũ."
                    ),
                )
            temporary_chat = await begin_temporary_chat(page)
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Bridge could not prepare a new chat ({type(error).__name__}: {error}); "
                    "request was not sent."
                ),
            ) from error
    return ui_state, temporary_chat


async def prepare_first_avs_role_chat(page, provider: str, config: dict) -> bool:
    """Create the first role conversation, preserving a configured ChatGPT Project."""
    if provider == "chatgpt" and os.environ.get("AVS_CHATGPT_PROJECT_URL", "").strip():
        try:
            await page.locator(config["input_selector"]).first.wait_for(
                state="visible", timeout=10000
            )
            await page.wait_for_timeout(500)
            try:
                composer = page.locator(config["input_selector"]).first
                if hasattr(composer, "input_value") and hasattr(composer, "fill"):
                    val = await composer.input_value()
                    if val and val.strip():
                        await composer.fill("")
            except Exception:
                pass
        except Exception:
            return False
        # Project home can display an existing conversation. Never append the
        # role's first request to that unrelated thread or use the global New
        # Chat action, which would leave the Project.
        return (
            not await _has_visible_response(page, config)
            and not await _has_visible_user_message(page, config)
        )
    return await perform_fast_new_chat(page, config)


DEFAULT_FACTORY_PROJECTS = os.path.join(os.path.expanduser("~"), "ai-software-factory", "projects")
ATTACHMENT_ROOT = os.environ.get("FACTORY_ATTACHMENT_ROOT", "/app/projects" if os.path.isdir("/app/projects") else DEFAULT_FACTORY_PROJECTS)
ATTACHMENT_ROOTS = [
    os.path.realpath(p) + os.sep for p in [
        os.environ.get("FACTORY_ATTACHMENT_ROOT", ""),
        "/app/projects",
        "/home/thinh/ai-software-factory/projects",
        "/Users/fkm/ai-software-factory/projects",
        DEFAULT_FACTORY_PROJECTS,
    ] if p and os.path.exists(p)
]
ATTACHMENT_MAX_BYTES = 200 * 1024 * 1024
BRIDGE_ROLE_LATE_REPLY_OBSERVATION_SEC = 60.0
BRIDGE_ROLE_LATE_REPLY_RELOAD_LIMIT = 2
BRIDGE_ROLE_THREAD_READY_WAIT_SEC = 15.0


def prepare_chatgpt_role_markdown(
    prompt: str,
    *,
    provider: str,
    role_thread_id: Optional[str],
    temp_dir: Optional[str] = None,
):
    """Package each AVS ChatGPT role request as a UTF-8 Markdown attachment."""
    if provider != "chatgpt" or not role_thread_id:
        return prompt, None

    descriptor, path = tempfile.mkstemp(
        prefix="avs-role-request-", suffix=".md", dir=temp_dir
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(prompt)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    # The Markdown document is the complete JSON request. Sending an extra
    # natural-language wrapper duplicates the instruction and makes the user
    # turn unstable; submit the attachment with an empty composer instead.
    return "", path


PROMPT_FILE_THRESHOLD_CHARS = int(os.environ.get("BRIDGE_PROMPT_FILE_THRESHOLD_CHARS", "20000"))


def package_oversized_prompt_if_needed(
    prompt: str,
    provider: str,
    *,
    prefix: str = "yeu_cau_chi_dao",
    temp_dir: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """
    Chuẩn hóa việc xử lý độ dài prompt trước khi đưa vào web composer:
    - Tier 1 & 2: Nếu len(prompt) <= PROMPT_FILE_THRESHOLD_CHARS (mặc định 20.000 ký tự):
      Giữ nguyên text để inject trực tiếp vào DOM (tốc độ cao ~5ms).
    - Tier 3: Nếu len(prompt) > PROMPT_FILE_THRESHOLD_CHARS (ví dụ 30K - 150K ký tự):
      Tự động đóng gói toàn bộ nội dung thành file vật lý UTF-8 (.md).
      Trả về câu lệnh chỉ đạo ngắn gọn trong ô chat để ChatGPT đọc tệp đính kèm và thực thi,
      đồng thời bảo toàn các mã nhận diện (NHAN-XXXXXX hoặc token) nếu có trong prompt.
    """
    if provider != "chatgpt" or len(prompt) <= PROMPT_FILE_THRESHOLD_CHARS:
        return prompt, None

    clean_prefix = re.sub(r"[^a-zA-Z0-9_\-]", "_", prefix) or "yeu_cau_chi_dao"
    descriptor, file_path = tempfile.mkstemp(
        prefix=f"{clean_prefix}-", suffix=".md", dir=temp_dir
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as h:
            h.write(prompt)
            h.flush()
            os.fsync(h.fileno())
    except Exception:
        try:
            os.remove(file_path)
        except OSError:
            pass
        raise

    file_name = os.path.basename(file_path)
    char_count = len(prompt)

    extracted_tokens = []
    nhan_match = re.search(r"NHAN-[A-Za-z0-9_\-]+", prompt)
    if nhan_match:
        extracted_tokens.append(f"Mã nhận diện lượt: {nhan_match.group(0)}")

    token_section = ("\n" + "\n".join(extracted_tokens)) if extracted_tokens else ""

    inline_directive = (
        f"[CHỈ THỊ THỰC THI TỪ TỆP ĐÍNH KÈM]\n"
        f"Nội dung yêu cầu chi tiết ({char_count:,} ký tự) được lưu và đính kèm trong tệp: `{file_name}`.{token_section}\n\n"
        f"Chỉ thị cho ChatGPT:\n"
        f"1. Hãy đọc kỹ toàn bộ tệp đính kèm `{file_name}` để nắm đầy đủ yêu cầu, quy tắc và dữ liệu.\n"
        f"2. Thực hiện chính xác và đầy đủ tất cả các chỉ đạo bên trong tệp và trả về kết quả theo đúng định dạng được yêu cầu."
    )

    print("BRIDGE_PROMPT_PACKAGED_TO_FILE " + json.dumps({
        "provider": provider,
        "originalChars": char_count,
        "fileName": file_name,
        "inlineDirectiveChars": len(inline_directive),
    }, ensure_ascii=False))

    return inline_directive, file_path


def _attachment_input_score(item: Dict[str, Any], attachment_kind: str) -> int:
    if item.get("disabled"):
        return -1000
    accepts = [
        token.strip().lower()
        for token in str(item.get("accept") or "").split(",")
        if token.strip()
    ]
    multiple = bool(item.get("multiple"))
    if not accepts:
        return 80 + (10 if multiple else 0)

    wildcard = any(token in ("*", "*/*") for token in accepts)
    if attachment_kind == "markdown":
        if wildcard or any(
            token in (".md", "text/markdown", "application/markdown", "text/plain", "text/*")
            for token in accepts
        ):
            return 100 + (10 if multiple else 0)
        return -1000

    zip_ok = any(
        token in (".zip", "application/zip", "application/x-zip-compressed")
        for token in accepts
    )
    if zip_ok or wildcard:
        return 100 + (10 if multiple else 0)
    image_only = all(token == "image/*" or token.startswith("image/") for token in accepts)
    if image_only:
        return 10
    return 40


def validated_attachment_paths(items: Optional[List[Dict[str, str]]]) -> List[str]:
    """Chỉ nhận file .zip có sẵn nằm trong thư mục dự án của App Factory, để không đính kèm nhầm file khác."""
    paths: List[str] = []
    roots = ATTACHMENT_ROOTS or [os.path.realpath(ATTACHMENT_ROOT) + os.sep]
    for item in items or []:
        raw = str((item or {}).get("path") or "").strip()
        real = os.path.realpath(raw) if raw else ""
        if not real or not any(real.startswith(r) for r in roots):
            raise HTTPException(status_code=400, detail="Đường dẫn đính kèm phải nằm trong thư mục dự án của App Factory.")
        if not real.lower().endswith(".zip") or not os.path.isfile(real):
            raise HTTPException(status_code=400, detail="Chỉ đính kèm file .zip có sẵn.")
        if os.path.getsize(real) > ATTACHMENT_MAX_BYTES:
            raise HTTPException(status_code=400, detail="File đính kèm lớn hơn 200 MB.")
        if real not in paths:
            paths.append(real)
    return paths


async def upload_files_to_page(
    page, file_paths: List[str], config: dict, *, attachment_kind: str = "archive"
):
    """Đính kèm file (ví dụ zip mã nguồn) vào ô chat của web. Ưu tiên ô chọn file nhận được zip, không chỉ ảnh."""
    if not file_paths:
        return
    file_sel = config.get("file_selector", "input[type='file']")
    input_details = []
    selected_input = None
    try:
        await page.wait_for_selector(file_sel, state="attached", timeout=8000)
        file_inputs = page.locator(file_sel)
        inputs_js = """els => els.map((e, i) => ({
              index: i, accept: e.accept || '', multiple: !!e.multiple,
              disabled: !!e.disabled, ariaLabel: e.getAttribute('aria-label') || ''
            }))"""

        # Trang mới mở thường khóa mọi ô chọn file một lúc: đợi tới khi có ô được bật
        for attempt in range(25):
            input_details = await file_inputs.evaluate_all(inputs_js)
            ranked_inputs = sorted(
                input_details,
                key=lambda item: _attachment_input_score(item, attachment_kind),
                reverse=True,
            )
            if ranked_inputs and _attachment_input_score(ranked_inputs[0], attachment_kind) >= 0:
                break
            await asyncio.sleep(1.0)
        if not ranked_inputs or _attachment_input_score(ranked_inputs[0], attachment_kind) < 0:
            raise RuntimeError(
                f"No enabled file input can take a {attachment_kind} attachment: "
                + json.dumps(input_details, ensure_ascii=False)
            )
        selected_input = ranked_inputs[0]
        selected = file_inputs.nth(selected_input["index"])
        if len(file_paths) > 1 and not selected_input.get("multiple"):
            # Ô chỉ nhận một file mỗi lần: đính kèm lần lượt từng file
            for single_path in file_paths:
                await selected.set_input_files([single_path], timeout=60000)
                await asyncio.sleep(1.5)
        else:
            await selected.set_input_files(file_paths, timeout=60000)
        queued_files = await selected.evaluate("e => e.files ? e.files.length : 0")
        print("BRIDGE_FILE_UPLOAD_SET " + json.dumps({
            "provider": config.get("name"), "fileCount": len(file_paths),
            "attachmentKind": attachment_kind,
            "fileBytes": [os.path.getsize(p) for p in file_paths],
            "selectedInput": selected_input,
            "filesOnSelectedInputAfterSet": queued_files,
        }, ensure_ascii=False))
        await asyncio.sleep(2.0)
    except Exception as e:
        print("BRIDGE_FILE_UPLOAD_ERROR " + json.dumps({
            "provider": config.get("name"), "fileCount": len(file_paths),
            "attachmentKind": attachment_kind,
            "inputs": input_details, "selectedInput": selected_input,
            "errorType": type(e).__name__,
            "error": str(e)[:1200],
        }, ensure_ascii=False))
        raise HTTPException(
            status_code=422,
            detail=f"{config['name']} không nhận được file đính kèm trong giao diện web: {type(e).__name__}",
        )


async def _resend_in_fresh_temporary_chat(
    page,
    config: dict,
    prompt: str,
    image_paths: List[str],
    *,
    confirmation_timeout: float,
    attachment_paths: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Replay a stateless ChatGPT request once when the submitted turn stayed idle.

    The first request already confirmed the selected model and effort in this
    browser tab. ChatGPT carries that selection into a new chat, so do not
    reopen a model picker while recovering from an idle page; its disabled
    state during the new-chat transition previously prevented the retry from
    ever reaching Send.
    """
    if not await perform_fast_new_chat(page, config):
        return None

    temporary_chat = await begin_temporary_chat(page)
    if image_paths:
        await upload_images_to_page(page, image_paths, config)
    if attachment_paths:
        await upload_files_to_page(page, attachment_paths, config)
    retry_baseline = await _response_message_count(page, config)
    await send_prompt_to_page(
        page,
        config,
        prompt,
        confirmation_timeout=confirmation_timeout,
        wait_for_attachment_ready=bool((image_paths or attachment_paths) and config.get("name") == "ChatGPT"),
    )
    return {
        "action": "fresh_temporary_chat_replay_same_selection",
        "baseline_response_count": retry_baseline,
        "temporary_chat": temporary_chat,
    }

async def cleanup_provider_session(
    page,
    provider: str,
    temporary_chat: bool,
    config: dict,
    *,
    retain_ephemeral_context: bool = False,
) -> str:
    """Discard a temporary chat or explicitly delete the current conversation."""
    if sys.platform != "darwin":
        return "linux_persistent_browser_retained"
    try:
        if temporary_chat:
            if provider == "chatgpt" and retain_ephemeral_context:
                # The provider's Temporary Chat mode prevents account-history
                # persistence. Keep its hidden browser page warm for the next
                # API call; that call starts a fresh chat before sending.
                return "temporary_chat_retained_in_ephemeral_context"
            await close_provider_context(provider)
            return "temporary_chat_discarded"
        deleted = await delete_current_chat_if_explicit(page)
        if deleted:
            await close_provider_context(provider)
            return "conversation_deleted"
        await close_provider_context(provider)
        return "chat_tab_closed_history_may_remain"
    except Exception:
        try:
            await close_provider_context(provider)
        except Exception:
            pass
        return "cleanup_failed_history_may_remain"

async def upload_images_to_page(page, image_paths: List[str], config: dict):
    """Đính kèm ảnh/khung hình footage vào ô chat của web."""
    if not image_paths:
        return
    file_sel = config.get("file_selector", "input[type='file']")
    input_details = []
    selected_input = None
    try:
        await page.wait_for_selector(file_sel, state="attached", timeout=8000)
        file_inputs = page.locator(file_sel)
        input_details = await file_inputs.evaluate_all(
            """els => els.map((e, i) => ({
              index: i, accept: e.accept || '', multiple: !!e.multiple,
              disabled: !!e.disabled, ariaLabel: e.getAttribute('aria-label') || '',
              queuedFiles: e.files ? e.files.length : 0
            }))"""
        )
        file_sizes = [os.path.getsize(path) for path in image_paths]

        def input_score(item):
            if item.get("disabled"):
                return -1000
            accepts = [
                token.strip().lower()
                for token in item.get("accept", "").split(",")
                if token.strip()
            ]
            accepts_image = any(
                token == "image/*" or token.startswith("image/")
                for token in accepts
            )
            multiple = bool(item.get("multiple"))
            if len(image_paths) > 1 and not multiple:
                return -1000
            if accepts_image:
                image_only = all(
                    token == "image/*" or token.startswith("image/")
                    for token in accepts
                )
                return (120 if image_only else 100) + (20 if multiple else 0)
            if not accepts:
                return 40 if multiple else 10
            return -100

        ranked_inputs = sorted(input_details, key=input_score, reverse=True)
        if not ranked_inputs or input_score(ranked_inputs[0]) < 0:
            raise RuntimeError(
                "No enabled file input accepts the requested image batch: "
                + json.dumps(input_details, ensure_ascii=False)
            )

        selected_input = ranked_inputs[0]
        selected = file_inputs.nth(selected_input["index"])
        await selected.set_input_files(image_paths, timeout=30000)
        queued_files = await selected.evaluate(
            "e => e.files ? e.files.length : 0"
        )
        print("BRIDGE_IMAGE_UPLOAD_SET " + json.dumps({
            "provider": config.get("name"), "imageCount": len(image_paths),
            "imageBytes": file_sizes, "selectedInput": selected_input,
            "setInputFilesReturned": True,
            "filesOnResolvedLocatorAfterSet": queued_files,
        }, ensure_ascii=False))
        print(f"📸 Đã chuyển {len(image_paths)} hình ảnh/footage vào ô đính kèm của {config['name']}")
        await asyncio.sleep(2.0)
    except Exception as e:
        print("BRIDGE_IMAGE_UPLOAD_ERROR " + json.dumps({
            "provider": config.get("name"), "imageCount": len(image_paths),
            "inputs": input_details, "selectedInput": selected_input,
            "errorType": type(e).__name__,
            "error": str(e)[:1200],
        }, ensure_ascii=False))
        raise HTTPException(
            status_code=422,
            detail=f"{config['name']} không nhận được ảnh đính kèm trong giao diện web: {type(e).__name__}",
        ) from e

async def submit_prepared_prompt(
    page,
    config: dict,
    composer,
    *,
    wait_for_attachment_ready: bool,
    attachment_ready_timeout: float,
) -> None:
    """Submit through a confirmed Send control, using Enter only when absent."""
    if not config.get("send_button_selector"):
        await composer.press("Enter")
        return

    # ChatGPT may take several seconds to enable Send after a large prompt
    # reaches its React composer. Never fall back to Enter while its send
    # button is disabled: on macOS that can insert a newline without
    # submitting the request.
    send_timeout = attachment_ready_timeout if wait_for_attachment_ready else 20.0
    submitted_by_button = await wait_for_and_click_send_button(
        page,
        config,
        timeout=send_timeout,
    )
    if not submitted_by_button:
        detail = (
            "Bridge chưa thấy nút Gửi sẵn sàng sau khi chuẩn bị nội dung; "
            "request was not sent."
        )
        if wait_for_attachment_ready:
            detail = (
                "Bridge đã chờ ảnh tải lên nhưng chưa xác nhận được nút Gửi sẵn sàng; "
                "request was not sent."
            )
        raise HTTPException(status_code=503, detail=detail)


async def _visible_user_attachment_marker(page, config: dict, marker: Optional[str]) -> bool:
    """Find a unique file title in the transcript, excluding the composer preview."""
    if not marker:
        return False
    try:
        return bool(await page.evaluate("""(cfg) => {
          const root = document.querySelector('main') || document.body;
          const visible = node => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 &&
              style.visibility !== 'hidden' && style.display !== 'none';
          };
          const marker = String(cfg.marker || '').trim().toLowerCase();
          const matches = Array.from(root.querySelectorAll('*')).filter(node => {
            if (!visible(node)) return false;
            const text = String(node.innerText || node.textContent || '').trim().toLowerCase();
            if (!text.includes(marker)) return false;
            return !Array.from(node.children || []).some(child =>
              String(child.innerText || child.textContent || '').trim().toLowerCase().includes(marker));
          });
          const markerNode = matches[matches.length - 1];
          if (!markerNode) return false;
          const currentUser = cfg.userSelector
            ? (markerNode.closest(cfg.userSelector) || markerNode) : markerNode;
          const composer = cfg.inputSelector ? document.querySelector(cfg.inputSelector) : null;
          if (composer && (composer === currentUser || composer.contains(currentUser) || currentUser.contains(composer))) return false;
          return !composer || Boolean(currentUser.compareDocumentPosition(composer) &
            Node.DOCUMENT_POSITION_FOLLOWING);
        }""", {
            "marker": marker,
            "userSelector": config.get("user_message_selector", ""),
            "inputSelector": config.get("input_selector", ""),
        }))
    except Exception:
        return False


async def send_prompt_to_page(
    page,
    config: dict,
    prompt: str,
    *,
    confirmation_timeout: float = 10.0,
    wait_for_attachment_ready: bool = False,
    attachment_ready_timeout: float = 90.0,
    allow_attachment_only: bool = False,
    expected_user_marker: Optional[str] = None,
) -> None:
    """Enter a prompt through Playwright so controlled web composers receive it."""
    composer = page.locator(config["input_selector"]).first
    await composer.wait_for(state="visible", timeout=15000)

    user_selector = config.get("user_message_selector")
    before_user_count = (
        await page.locator(user_selector).count() if user_selector else None
    )
    if allow_attachment_only and (
        not wait_for_attachment_ready or not expected_user_marker
    ):
        raise HTTPException(
            status_code=422,
            detail="Bridge chỉ gửi tệp không kèm chữ khi xác nhận được tệp đính kèm và dấu nhận diện lượt.",
        )

    # Do not call page.bring_to_front() on macOS to avoid stealing window focus from the user
    try:
        if sys.platform != "darwin":
            await page.bring_to_front()
    except Exception:
        pass
    try:
        await _wait_generation_idle(page, config, max_wait_sec=20.0)
    except Exception:
        pass

    try:
        await composer.scroll_into_view_if_needed(timeout=5000)
        await composer.click(timeout=5000)
    except Exception:
        pass

    fill_ok = False
    if len(prompt) <= 1500:
        try:
            await composer.fill(prompt, timeout=10000)
            fill_ok = True
        except Exception as fill_err:
            print(f"BRIDGE_FILL_RETRY: composer.fill failed ({fill_err}), attempting fallback via JS insertText...")
    else:
        print(f"BRIDGE_FILL_LONG_PROMPT: Prompt length {len(prompt)} > 1500, using direct JS insertText...")

    if not fill_ok:
        try:
            eval_res = await page.evaluate('''async (text) => {
                const el = document.querySelector("#prompt-textarea, div[contenteditable='true'], div[role='textbox'], textarea[data-testid='composer-input']");
                if (el) {
                    el.focus();
                    if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
                        el.value = text;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    } else if (el.isContentEditable) {
                        try {
                            const dt = new DataTransfer();
                            dt.setData('text/plain', text);
                            const ev = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true });
                            el.dispatchEvent(ev);
                            if (el.innerText && el.innerText.trim().length > 0) return true;
                        } catch(e) {}
                        try {
                            document.execCommand('selectAll', false, null);
                            const ok = document.execCommand('insertText', false, text);
                            if (ok && el.innerText.trim().length > 0) return true;
                        } catch(e) {}
                        el.innerText = text;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        return true;
                    }
                }
                return false;
            }''', prompt)
            if eval_res:
                fill_ok = True
        except Exception as e2:
            print(f"BRIDGE_FILL_FALLBACK_ERR: {e2}")
            try:
                await composer.focus(timeout=5000)
                await page.keyboard.insert_text(prompt)
                fill_ok = True
            except Exception as e3:
                print(f"BRIDGE_FILL_KEYBOARD_ERR: {e3}")

    normalize = lambda value: re.sub(
        r"\s+(?=https?://)", "",
        re.sub(r"\s+", " ", value or "").strip(),
        flags=re.I,
    )
    expected_text = normalize(prompt)
    entered_variants = []
    for label, reader in (
        ("value", composer.input_value),
        ("inner_text", composer.inner_text),
        ("text_content", composer.text_content),
    ):
        try:
            entered_variants.append((label, normalize(await reader() or "")))
        except Exception:
            pass

    try:
        dom_text = await page.evaluate('''() => {
            const el = document.querySelector("#prompt-textarea, div[contenteditable='true'], div[role='textbox'], textarea[data-testid='composer-input']");
            if (!el) return "";
            return el.value || el.innerText || el.textContent || "";
        }''')
        if dom_text:
            entered_variants.append(("dom_eval", normalize(dom_text)))
    except Exception:
        pass
    exact_variant = next(
        ((label, value) for label, value in entered_variants if value == expected_text),
        None,
    )
    if exact_variant is None and expected_text:
        for label, val in entered_variants:
            if val and (len(val) >= len(expected_text) * 0.7 or (len(expected_text) > 30 and expected_text[:30] in val)):
                exact_variant = (label, val)
                break
    actual_text = exact_variant[1] if exact_variant else max(
        entered_variants,
        key=lambda item: len(os.path.commonprefix([expected_text, item[1]])),
        default=("none", ""),
    )[1]
    if (not expected_text and not allow_attachment_only) or exact_variant is None:
        variant_diagnostics = []
        for label, value in entered_variants:
            common_prefix_chars = len(os.path.commonprefix([expected_text, value]))
            common_suffix_chars = len(os.path.commonprefix([expected_text[::-1], value[::-1]]))
            variant_diagnostics.append({
                "reader": label,
                "enteredChars": len(value),
                "commonPrefixChars": common_prefix_chars,
                "commonSuffixChars": common_suffix_chars,
                "expectedCodepointsAtMismatch": [
                    f"U+{ord(char):04X}"
                    for char in expected_text[common_prefix_chars:common_prefix_chars + 12]
                ],
                "enteredCodepointsAtMismatch": [
                    f"U+{ord(char):04X}"
                    for char in value[common_prefix_chars:common_prefix_chars + 12]
                ],
            })
        print("BRIDGE_PROMPT_FILL_DIAGNOSTIC " + json.dumps({
            "expectedChars": len(expected_text),
            "variants": variant_diagnostics,
        }, ensure_ascii=False))
        raise HTTPException(
            status_code=422,
            detail="Bridge không xác nhận được nội dung trong ô chat; yêu cầu chưa được gửi.",
        )

    network_capture = None
    if config.get("name") == "ChatGPT" and callable(getattr(page, "on", None)):
        try:
            network_capture = ChatGPTNetworkResponseCapture.attach(
                page, expected_user_text=expected_text
            )
            await network_capture.ready()
        except Exception as error:
            # Network observation is optional; authenticated UI submission and
            # the existing DOM path remain available if Playwright cannot attach.
            print("BRIDGE_NETWORK_CAPTURE_ATTACH_FAILED " + json.dumps({
                "provider": "chatgpt",
                "errorType": type(error).__name__,
            }, ensure_ascii=False))
    try:
        await submit_prepared_prompt(
            page,
            config,
            composer,
            wait_for_attachment_ready=wait_for_attachment_ready,
            attachment_ready_timeout=attachment_ready_timeout,
        )
    except Exception:
        if network_capture is not None:
            network_capture.detach()
        raise

    if user_selector or allow_attachment_only:
        async def user_message_arrived() -> bool:
            try:
                if expected_user_marker and await _visible_user_attachment_marker(
                    page, config, expected_user_marker
                ):
                    return True
                try:
                    current_composer_text = await composer.input_value()
                except Exception:
                    current_composer_text = await composer.inner_text()
                # A successful submission may clear the React composer before
                # the transcript's user-message marker is mounted.
                if not normalize(current_composer_text):
                    return not allow_attachment_only

                if not user_selector:
                    return False
                messages = page.locator(user_selector)
                current_count = await messages.count()
                if current_count <= before_user_count:
                    return False
                for index in range(before_user_count, current_count):
                    message_text = normalize(await messages.nth(index).inner_text())
                    if message_text.startswith(expected_text):
                        return True
                return False
            except Exception:
                return False

        async def wait_for_user_message(timeout: float) -> bool:
            deadline = time.monotonic() + max(0.5, timeout)
            while time.monotonic() < deadline:
                if await user_message_arrived():
                    return True
                await asyncio.sleep(0.2)
            return await user_message_arrived()

        if await wait_for_user_message(confirmation_timeout):
            return

        try:
            send_diagnostic = await page.evaluate("""(cfg) => {
              const visible = (node) => {
                if (!node) return false;
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 0 && rect.height > 0 &&
                  style.visibility !== 'hidden' && style.display !== 'none';
              };
              const composer = document.querySelector(cfg.inputSelector);
              const userMessages = document.querySelectorAll(cfg.userSelector);
              return {
                path: location.pathname,
                composerVisible: visible(composer),
                composerChars: composer
                  ? ('value' in composer ? composer.value : composer.innerText || '').length : 0,
                composerRole: composer?.getAttribute('role') || '',
                composerContentEditable: composer?.getAttribute('contenteditable') || '',
                sendButtonStates: cfg.sendSelector
                  ? Array.from(document.querySelectorAll(cfg.sendSelector)).map(button => ({
                      visible: visible(button), disabled: !!button.disabled,
                      ariaLabel: button.getAttribute('aria-label') || '',
                      testId: button.getAttribute('data-testid') || ''
                    })).slice(0, 8)
                  : [],
                userMessageCount: userMessages.length,
                latestTurnChars: userMessages.length
                  ? (userMessages[userMessages.length - 1].innerText || '').length : 0
              };
            }""", {
                "inputSelector": config["input_selector"],
                "sendSelector": config.get("send_button_selector"),
                "userSelector": user_selector,
            })
            print("BRIDGE_SEND_CONFIRMATION_DIAGNOSTIC " + json.dumps(send_diagnostic, ensure_ascii=False))
            if sys.platform == "darwin":
                await page.screenshot(path="/tmp/bridge-send-failure.png")
        except Exception as error:
            print("BRIDGE_SEND_CONFIRMATION_DIAGNOSTIC " + json.dumps({
                "captureError": type(error).__name__,
            }, ensure_ascii=False))

        if network_capture is not None:
            network_capture.detach()

        raise HTTPException(
            status_code=502,
            detail=(
                "Bridge đã bấm Gửi nhưng chưa xác nhận được tin nhắn xuất hiện; "
                "kết quả gửi chưa xác định, nên Bridge dừng để tránh gửi trùng."
            ),
        )


async def click_send_button(page, config: dict) -> bool:
    """Click a provider's explicit enabled Send control when it is available."""
    selector = config.get("send_button_selector")
    if not selector:
        return False
    try:
        candidates = await page.locator(selector).all()
        def _score_candidate(tid: str, label: str) -> int:
            if tid == "send-button":
                return 10
            lbl = label.strip().lower()
            if lbl in ("gửi", "send", "send prompt", "send message", "gửi tin nhắn", "gửi lời nhắc"):
                return 9
            if "send" in lbl or "gửi" in lbl:
                return 5
            return 1

        scored_candidates = []
        for candidate in candidates:
            try:
                if not await candidate.is_visible() or not await candidate.is_enabled():
                    continue
                aria_disabled = await candidate.get_attribute("aria-disabled")
                aria_busy = await candidate.get_attribute("aria-busy")
                if aria_disabled == "true" or aria_busy == "true":
                    continue
                tid = (await candidate.get_attribute("data-testid") or "").strip()
                label = (await candidate.get_attribute("aria-label") or "").strip()
                scored_candidates.append((_score_candidate(tid, label), candidate))
            except Exception:
                continue

        scored_candidates.sort(key=lambda x: x[0], reverse=True)
        for _, candidate in scored_candidates:
            try:
                await candidate.click()
                return True
            except Exception:
                continue
    except Exception:
        return False
    return False


async def wait_for_and_click_send_button(
    page,
    config: dict,
    *,
    timeout: float = 90.0,
    poll_interval: float = 0.25,
    stable_for: float = 1.0,
) -> bool:
    """Wait until image upload progress is gone and the Send control stays ready."""
    selector = config.get("send_button_selector")
    if not selector:
        return False
    started = time.monotonic()
    deadline = time.monotonic() + max(0.0, timeout)
    ready_since = None
    last_state = {}
    while True:
        try:
            last_state = await page.evaluate("""(cfg) => {
              const visible = (node) => {
                if (!node) return false;
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 0 && rect.height > 0 &&
                  style.visibility !== 'hidden' && style.display !== 'none';
              };
              const progressBars = Array.from(document.querySelectorAll('[role="progressbar"]'))
                .filter(visible);
              const pendingUploads = progressBars
                .map((node) => (node.getAttribute('aria-label') || node.innerText || '').trim())
                .filter((label) => /upload|tải lên|đang tải/i.test(label));
              const sendButtons = Array.from(document.querySelectorAll(cfg.sendSelector || ''))
                .filter(visible)
                .map((button) => ({
                  ariaLabel: button.getAttribute('aria-label') || '',
                  testId: button.getAttribute('data-testid') || '',
                  disabled: !!button.disabled,
                  ariaDisabled: button.getAttribute('aria-disabled') || '',
                  ariaBusy: button.getAttribute('aria-busy') || ''
                }));
              const sendEnabled = sendButtons.some((button) =>
                !button.disabled && button.ariaDisabled !== 'true' && button.ariaBusy !== 'true'
              );
              return {pendingUploads, sendButtons, sendEnabled};
            }""", {"sendSelector": selector})
        except Exception:
            last_state = {}

        ready = not last_state.get("pendingUploads") and bool(last_state.get("sendEnabled"))
        now = time.monotonic()
        if ready:
            if ready_since is None:
                ready_since = now
            if now - ready_since >= max(0.0, stable_for):
                if await click_send_button(page, config):
                    print("BRIDGE_ATTACHMENT_SEND_READY " + json.dumps({
                        "provider": config.get("name", "unknown"),
                        "ready": True,
                        "waitedMs": int((time.monotonic() - started) * 1000),
                        "pendingUploads": 0,
                        "sendButtons": last_state.get("sendButtons", [])[:4],
                    }, ensure_ascii=False))
                    return True
                ready_since = None
        else:
            ready_since = None

        if time.monotonic() >= deadline:
            print("BRIDGE_ATTACHMENT_SEND_READY " + json.dumps({
                "provider": config.get("name", "unknown"),
                "ready": False,
                "waitedMs": int((time.monotonic() - started) * 1000),
                "pendingUploads": last_state.get("pendingUploads", [])[:8],
                "sendButtons": last_state.get("sendButtons", [])[:4],
            }, ensure_ascii=False))
            if sys.platform == "darwin":
                try:
                    await page.screenshot(path="/tmp/bridge-attachment-not-ready.png")
                except Exception:
                    pass
            return False
        await asyncio.sleep(max(0.05, poll_interval))


_CHATGPT_PROMPT_TOO_LONG = re.compile(
    r"(?:message(?:\s+you\s+submitted)?\s+(?:was|is)\s+too\s+long|"
    r"your\s+message\s+is\s+too\s+long|"
    r"prompt\s+(?:was|is)\s+too\s+long|"
    r"(?:maximum\s+message\s+length|too\s+many\s+characters)|"
    r"tin\s+nhan.{0,100}qua\s+dai|"
    r"noi\s+dung.{0,100}(?:qua\s+dai|vuot\s+qua\s+gioi\s+han))",
    re.IGNORECASE,
)


def _is_chatgpt_prompt_too_long_text(value: str) -> bool:
    normalized = "".join(
        char
        for char in unicodedata.normalize("NFKD", value or "")
        if not unicodedata.combining(char)
    )
    return bool(_CHATGPT_PROMPT_TOO_LONG.search(normalized))


_CHATGPT_ATTACHMENT_BADGE = (
    r"(?:Tài liệu|Document|Tệp|File|Mã|Code|Bảng tính|Spreadsheet|PDF)"
)
_CHATGPT_ASSISTANT_HEADER = (
    r"(?:(?:Assistant|ChatGPT)(?: said| đã nói)?:|ChatGPT\s+(?:said|đã nói))"
)
_CHATGPT_UI_HEADER_PATTERN = re.compile(
    rf"^(?:{_CHATGPT_ATTACHMENT_BADGE}\s+)?{_CHATGPT_ASSISTANT_HEADER}\s*",
    flags=re.I,
)
_CHATGPT_DURATION_UNIT = (
    r"(?:milliseconds?|msecs?|ms|seconds?|secs?|s|minutes?|mins?|m|"
    r"hours?|hrs?|h|giây|phút|giờ)"
)
_CHATGPT_DURATION_PATTERN = re.compile(
    rf"^(?:Đã xử lý trong|Processed in)\s+"
    rf"\d+(?:[.,]\d+)?\s*{_CHATGPT_DURATION_UNIT}"
    rf"(?:\s+\d+(?:[.,]\d+)?\s*{_CHATGPT_DURATION_UNIT})*\s+",
    flags=re.I,
)


def clean_chatgpt_response_text(value: str) -> str:
    text = (value or "").strip()
    # ChatGPT can render a provider-side Cloudflare failure inside the
    # conversation pane. It is UI chrome with a Retry button, never an answer.
    if re.search(r"cloudflare[\s_-]+challenge", text, flags=re.I):
        return ""
    for _ in range(2):
        text = _CHATGPT_UI_HEADER_PATTERN.sub("", text)
        text = _CHATGPT_DURATION_PATTERN.sub("", text)
    status_only = re.sub(r"[.…]+$", "", text).strip().casefold()
    if status_only in {
        "something went wrong",
        "there was an error generating a response",
        "an error occurred while generating the response",
        "error in message stream",
        "network error",
        "đã xảy ra lỗi khi tạo câu trả lời",
        "đã xảy ra lỗi khi tạo phản hồi",
        "lỗi khi tạo phản hồi",
    }:
        return ""
    # Assistant-message selectors can include the surrounding transcript's
    # visible pager controls. Strip only a recognized trailing UI label so it
    # cannot become an extra semantic field in strict specialist contracts.
    text = re.sub(
        r"(?:\s*[.…]{1,3}\s*)?(?:Xem thêm|Show more|Thu gọn|Show less)"
        r"\s*(?:ChatGPT(?:\s+(?:đã nói|said))?)?:?\s*$",
        "",
        text,
        flags=re.I,
    ).strip()
    for footer in ("ChatGPT có thể mắc lỗi.", "ChatGPT can make mistakes."):
        text = text.split(footer, 1)[0].strip()
    status_only = re.sub(r"[.…]+$", "", text).strip().casefold()
    if status_only in {
        "chatgpt đang phản hồi",
        "chatgpt is responding",
        "chatgpt đã nói",
        "thinking",
        "reasoning",
        "đang suy nghĩ",
        "đang phản hồi",
    }:
        return ""
    return text


async def _chatgpt_cloudflare_challenge_visible(page) -> bool:
    """Return whether ChatGPT is showing its retryable Cloudflare error card."""
    try:
        return bool(await page.evaluate("""() => {
          const text = document.body?.innerText || '';
          return /cloudflare[\\s_-]+challenge/i.test(text);
        }"""))
    except Exception:
        return False


async def _chatgpt_retryable_error_visible(page) -> bool:
    """Detect a provider-owned error card, not error words in conversation text."""
    try:
        return bool(await page.evaluate("""() => {
          const visible = node => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 &&
              style.visibility !== 'hidden' && style.display !== 'none';
          };
          const errorText = /something went wrong|there was an error generating|an error occurred while generating|error in message stream|network error|tài khoản của bạn đã thay đổi|không nhận được phản hồi|hãy gửi lại tin nhắn|hãy kiểm tra kết nối|đã xảy ra lỗi khi tạo|lỗi khi tạo phản hồi/i;
          return Array.from(document.querySelectorAll(
            '[role="alert"], [aria-live="assertive"], [data-testid*="error" i], [class*="error" i]'
          )).filter(visible).some(node => errorText.test(node.innerText || ''));
        }"""))
    except Exception:
        return False


def is_chatgpt_transient_error(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(p in t for p in (
        "tài khoản của bạn đã thay đổi",
        "không nhận được phản hồi từ chatgpt",
        "hãy gửi lại tin nhắn",
        "hãy kiểm tra kết nối",
        "something went wrong",
        "there was an error generating",
        "an error occurred while generating",
        "error in message stream",
    ))


def clean_chatgpt_transcript_tail(value: str) -> str:
    text = (value or "").strip()
    text = re.sub(
        r"^(?:(?:Xem thêm|Show more|Thu gọn|Show less)\s*)+",
        "",
        text,
        flags=re.I,
    )
    text = _CHATGPT_UI_HEADER_PATTERN.sub("", text)
    # The current ChatGPT response pager appends a visible N/N control to
    # paginated long answers; it is UI chrome, not part of the model response.
    text = re.sub(r"\s+\d+\s*/\s*\d+\s*$", "", text)
    return clean_chatgpt_response_text(text)


keep_code_fences_var: contextvars.ContextVar = contextvars.ContextVar("keep_code_fences", default=False)
# ChatGPT vẽ khối mã thành <pre> (nhãn ngôn ngữ ở đầu, mã trong <code>) và bộ chọn tin nhắn trợ lý của nó đổi liên tục, nên chữ lấy bằng
# textContent/innerText mất dấu ``` và các khối lệnh của factory không đọc được. Đi theo cây DOM, bắt đầu sau tin nhắn cuối của người dùng
# (không cần bộ chọn tin nhắn trợ lý) và viết lại mỗi <pre> thành khối ``` có nhãn.
FENCED_TRANSCRIPT_JS = """(cfg) => {
  const root = document.querySelector('main') || document.body;
  const visible = node => {
    if (!node) return false;
    const rect = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    return rect.width > 0 && rect.height > 0 &&
      style.visibility !== 'hidden' && style.display !== 'none';
  };
  let lastUser;
  const marker = String(cfg.expectedUserMarker || '').trim().toLowerCase();
  if (marker) {
    const markerNodes = Array.from(root.querySelectorAll('*')).filter(node => {
      if (!visible(node)) return false;
      const text = String(node.innerText || node.textContent || '').trim().toLowerCase();
      if (!text.includes(marker)) return false;
      return !Array.from(node.children || []).some(child =>
        String(child.innerText || child.textContent || '').trim().toLowerCase().includes(marker));
    });
    const markerNode = markerNodes[markerNodes.length - 1];
    if (!markerNode) return '';
    lastUser = markerNode.closest(cfg.userSelector || '[class*="bg-user-message"]') || markerNode;
    const composer = cfg.inputSelector ? document.querySelector(cfg.inputSelector) : null;
    if (composer && (composer === lastUser || composer.contains(lastUser) || lastUser.contains(composer))) return '';
    if (composer && !(lastUser.compareDocumentPosition(composer) & Node.DOCUMENT_POSITION_FOLLOWING)) return '';
  } else {
    const users = root.querySelectorAll('[class*="bg-user-message"]');
    if (!users.length) return '';
    lastUser = users[users.length - 1];
  }
  const BLOCK = new Set(['P','DIV','LI','UL','OL','H1','H2','H3','H4','H5','H6','TR','TABLE','SECTION','ARTICLE','BLOCKQUOTE','HR']);
  const SKIP = new Set(['BUTTON','SCRIPT','STYLE','SVG','TEXTAREA']);
  let out = '';
  let seen = false;
  const nl = () => { if (out && !out.endsWith('\\n')) out += '\\n'; };
  function langOf(pre, code) {
    if (code) {
      const cls = Array.from(code.classList).find(c => c.startsWith('language-'));
      if (cls) return cls.slice(9);
    }
    const head = pre.firstElementChild;
    const label = head && !(code && head.contains(code)) ? (head.textContent || '').trim() : '';
    return label && label.length < 24 && !/\\s/.test(label) ? label : '';
  }
  function walk(node) {
    if (node === lastUser) { out = ''; seen = true; return; }
    if (node.nodeType === 3) { if (seen) out += node.nodeValue; return; }
    if (node.nodeType !== 1) return;
    const tag = node.tagName.toUpperCase();
    if (seen && /(^|\\s)sr-only(\\s|$)/.test(String(node.className || ''))) return;  // nhãn chỉ dành cho trình đọc màn hình ("ChatGPT đã nói:")
    if (seen) {
      if (SKIP.has(tag)) return;
      if (tag === 'PRE') {
        const code = node.querySelector('code');
        const body = (code || node).textContent.replace(/\\n$/, '');
        nl();
        out += '```' + langOf(node, code) + '\\n' + body + '\\n```\\n';
        return;
      }
      if (tag === 'BR') { out += '\\n'; return; }
    }
    const block = seen && BLOCK.has(tag);
    if (block) nl();
    for (const child of node.childNodes) walk(child);
    if (block) nl();
  }
  walk(root);
  return seen ? out.trim() : '';
}"""


async def read_latest_response_text(
    page,
    config: dict,
    submitted_prompt: Optional[str] = None,
    expected_user_marker: Optional[str] = None,
) -> str:
    """Read the provider's final assistant message from its current UI shape."""
    if keep_code_fences_var.get() and config.get("name") == "ChatGPT":
        try:
            fenced = clean_chatgpt_response_text(await page.evaluate(
                FENCED_TRANSCRIPT_JS,
                {
                    "expectedUserMarker": expected_user_marker or "",
                    "userSelector": config.get("user_message_selector", ""),
                    "inputSelector": config.get("input_selector", ""),
                },
            ) or "")
            if fenced:
                return fenced
        except Exception:
            pass  # không đọc được kiểu này thì rơi về cách đọc cũ bên dưới

    if config.get("name") == "ChatGPT" and expected_user_marker:
        try:
            transcript = await page.locator("main").inner_text()
            normalize = lambda value: re.sub(r"\s+", " ", value or "").strip()
            normalized_transcript = normalize(transcript)
            normalized_marker = normalize(expected_user_marker)
            marker_end = normalized_transcript.rfind(normalized_marker)
            if marker_end >= 0:
                return clean_chatgpt_transcript_tail(
                    normalized_transcript[marker_end + len(normalized_marker):]
                )
        except Exception:
            return ""
        # This role request is attachment-only. Never fall back to the prior
        # assistant message when the current request's unique file marker has
        # not appeared in the transcript yet.
        return ""

    # Prefer the current turn's transcript tail over a generic assistant
    # selector. ChatGPT can expose intermediate/status content through the
    # broad assistant-message selector while the actual final answer is already
    # present after this request's unique user message. Anchoring first avoids
    # returning that unrelated fragment as the role result.
    if config.get("name") == "ChatGPT" and submitted_prompt:
        try:
            transcript = await page.locator("main").inner_text()
            normalize = lambda value: re.sub(r"\s+", " ", value or "").strip()
            normalized_transcript = normalize(transcript)
            normalized_prompt = normalize(submitted_prompt)
            prompt_end = normalized_transcript.rfind(normalized_prompt)
            if prompt_end >= 0:
                response_text = clean_chatgpt_transcript_tail(
                    normalized_transcript[prompt_end + len(normalized_prompt):]
                )
                if response_text:
                    return response_text
        except Exception:
            pass

    try:
        elements = await page.locator(config["response_selector"]).all_text_contents()
        if elements and elements[-1].strip():
            response_text = elements[-1].strip()
            if config.get("name") == "ChatGPT":
                response_text = clean_chatgpt_response_text(response_text)
            if response_text:
                return response_text
    except Exception:
        pass

    # ChatGPT's current transcript no longer exposes stable role/test-id
    # attributes on all message variants. Anchor on this request's user prompt
    # before falling back to legacy selectors.
    if config.get("name") == "ChatGPT":
        try:
            transcript = await page.locator("main").inner_text()
            normalize = lambda value: re.sub(r"\s+", " ", value or "").strip()

            # Anchor extraction to the prompt Bridge just submitted. ChatGPT's
            # web DOM has changed more than once: some long/paginated replies
            # have neither the role attributes nor the group classes above.
            # The timeout capture on 2026-11-02 showed a completed assistant
            # reply in <main> while both configured selectors returned none.
            user_selector = config.get("user_message_selector")
            users = page.locator(user_selector) if user_selector else None
            if users and await users.count():
                user_text = (await users.last.inner_text()).strip()
                if user_text and user_text in transcript:
                    response_text = transcript.rsplit(user_text, 1)[1]
                else:
                    normalized_transcript = normalize(transcript)
                    normalized_user = normalize(user_text)
                    if not normalized_user:
                        return ""
                    user_end = normalized_transcript.rfind(normalized_user)
                    if user_end < 0:
                        return ""
                    response_text = normalized_transcript[user_end + len(normalized_user):]
                return clean_chatgpt_transcript_tail(response_text)
        except Exception:
            pass
    return ""


async def _response_message_count(page, config: dict) -> int:
    try:
        return await page.locator(config["response_selector"]).count()
    except Exception:
        return 0


async def _click_latest_regenerate(page, config: dict) -> bool:
    selector = config.get("regenerate_selector")
    if not selector:
        return False
    try:
        candidates = await page.locator(selector).all()
    except Exception:
        return False
    for candidate in reversed(candidates):
        try:
            if await candidate.is_visible() and await candidate.is_enabled():
                await candidate.click()
                if config.get("name") != "ChatGPT":
                    return True

                # The current ChatGPT UI opens a menu from the regenerate
                # toolbar button. Clicking the toolbar alone does not start a
                # new generation; the provider-owned "Try again" menu action
                # must be selected for this same assistant turn.
                retry_option = page.get_by_text(
                    re.compile(r"^(?:try again|retry|thử lại)\b", re.IGNORECASE)
                )
                for _ in range(8):
                    if await _has_visible_generation_marker(page, config):
                        return True
                    try:
                        retry_options = await retry_option.all()
                    except Exception:
                        retry_options = []
                    for option in reversed(retry_options):
                        try:
                            if await option.is_visible() and await option.is_enabled():
                                await option.click()
                                print("BRIDGE_CHATGPT_REGENERATE_MENU_SELECTED " + json.dumps({
                                    "provider": "chatgpt",
                                    "action": "provider_owned_same_turn_retry",
                                }, ensure_ascii=False))
                                return True
                        except Exception:
                            continue
                    await asyncio.sleep(0.25)
                # A menu that failed to expose a retry option is not proof that
                # the assistant restarted. Leave the turn unresolved so the
                # caller can keep observing or report an unknown outcome.
                return False
        except Exception:
            continue
    return False


async def _current_turn_regenerate_visible(
    page, config: dict, expected_user_marker: Optional[str] = None
) -> bool:
    """Confirm Retry follows this request, using its unique attachment when present."""
    user_selector = config.get("user_message_selector")
    regenerate_selector = config.get("regenerate_selector")
    if not user_selector or not regenerate_selector:
        return False
    try:
        return bool(await page.evaluate("""(cfg) => {
          const root = document.querySelector('main') || document.body;
          const visible = node => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 &&
              style.visibility !== 'hidden' && style.display !== 'none' &&
              !node.disabled;
          };
          const users = Array.from(root.querySelectorAll(cfg.userSelector)).filter(visible);
          const retries = Array.from(root.querySelectorAll(cfg.regenerateSelector)).filter(visible);
          const latestRetry = retries[retries.length - 1];
          if (!latestRetry) return false;
          const marker = String(cfg.expectedUserMarker || '').trim().toLowerCase();
          if (marker) {
            // ChatGPT can leave its transcript DOM stale after sending a file.
            // The random Markdown filename is visible in that user turn, so use
            // it as an exact request anchor instead of relying on a virtualized
            // message count that may be unchanged after the latest turn arrives.
            const markerNodes = Array.from(root.querySelectorAll('*')).filter(node => {
              if (!visible(node)) return false;
              const text = String(node.innerText || node.textContent || '').trim().toLowerCase();
              if (!text.includes(marker)) return false;
              return !Array.from(node.children || []).some(child =>
                String(child.innerText || child.textContent || '').trim().toLowerCase().includes(marker));
            });
            const markerNode = markerNodes[markerNodes.length - 1];
            if (!markerNode) return false;
            const currentUser = markerNode.closest(cfg.userSelector) || markerNode;
            const composer = cfg.inputSelector
              ? document.querySelector(cfg.inputSelector) : null;
            if (composer && (composer === currentUser ||
              composer.contains(currentUser) || currentUser.contains(composer))) return false;
            if (composer && !(currentUser.compareDocumentPosition(composer) &
              Node.DOCUMENT_POSITION_FOLLOWING)) return false;
            const laterUserExists = users.some(user => user !== currentUser &&
              Boolean(currentUser.compareDocumentPosition(user) & Node.DOCUMENT_POSITION_FOLLOWING));
            if (laterUserExists) return false;
            return Boolean(currentUser.compareDocumentPosition(latestRetry) &
              Node.DOCUMENT_POSITION_FOLLOWING);
          }
          const lastUser = users[users.length - 1];
          if (!lastUser) return false;
          return Boolean(lastUser.compareDocumentPosition(latestRetry) &
            Node.DOCUMENT_POSITION_FOLLOWING);
        }""", {
            "userSelector": user_selector,
            "regenerateSelector": regenerate_selector,
            "expectedUserMarker": expected_user_marker or "",
            "inputSelector": config.get("input_selector", ""),
        }))
    except Exception:
        return False


def _role_turn_retry_is_safe(
    *,
    new_user_turn_visible: bool,
    current_turn_regenerate_visible: bool,
    expected_user_marker: Optional[str],
) -> bool:
    """Use the unique request marker only when its own Retry was verified after it."""
    return current_turn_regenerate_visible and (
        new_user_turn_visible or bool(expected_user_marker)
    )


async def _capture_idle_after_wake(page, config: dict) -> None:
    """Record sanitized UI state when a wake retry still has no answer."""
    response_selector = config.get("response_selector", "")
    user_selector = config.get("user_message_selector", "")
    stop_selector = config.get("stop_selector")
    try:
        state = await page.evaluate("""(cfg) => {
          const visible = (node) => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 &&
              style.visibility !== 'hidden' && style.display !== 'none';
          };
          const responses = Array.from(document.querySelectorAll(cfg.responseSelector));
          const users = cfg.userSelector
            ? Array.from(document.querySelectorAll(cfg.userSelector)) : [];
          const composer = document.querySelector(cfg.inputSelector);
          const stop = cfg.stopSelector ? document.querySelector(cfg.stopSelector) : null;
          const statusKeywords = [
            'error', 'failed', 'try again', 'something went wrong', 'network',
            'unavailable', 'quota', 'limit', 'login', 'sign in', 'đăng nhập',
            'tải lại', 'lỗi', 'unable'
          ];
          const statusLines = (document.body?.innerText || '')
            .split(String.fromCharCode(10)).map(line => line.trim())
            .filter(line => line && statusKeywords.some(keyword =>
              line.toLowerCase().includes(keyword)))
            .slice(-8).map(line => line.slice(0, 160));
          const buttons = Array.from(document.querySelectorAll('button'))
            .filter(visible).map(button => {
              const label = (button.getAttribute('aria-label') ||
                button.getAttribute('data-testid') || button.innerText || '').toLowerCase();
            const category = /stop|dừng|ngừng/.test(label) ? 'stop'
                : /regenerate|retry|try again|thử lại|tạo lại/.test(label) ? 'retry'
                : /send|submit/.test(label) ? 'send' : null;
              return category ? {category, disabled: !!button.disabled} : null;
            }).filter(Boolean).slice(0, 12);
          const composerText = composer
            ? ('value' in composer ? composer.value : composer.innerText || '') : '';
          return {
            urlPath: location.pathname,
            titleChars: document.title.length,
            composerVisible: visible(composer),
            composerTextChars: composerText.length,
            userMessageCount: users.length,
            lastUserMessageChars: users.length
              ? (users[users.length - 1].innerText || '').length : 0,
            responseCount: responses.length,
            responseTextChars: responses.slice(-2).map(node => (node.innerText || '').length),
            stopVisible: visible(stop),
            loginTextVisible: /sign in|log in|đăng nhập/.test(
              (document.body?.innerText || '').toLowerCase()),
            statusLines,
            actionButtons: buttons
          };
        }""", {
            "responseSelector": response_selector,
            "userSelector": user_selector,
            "inputSelector": config.get("input_selector", ""),
            "stopSelector": stop_selector,
        })
    except Exception as error:
        state = {"captureError": type(error).__name__}
    print("BRIDGE_IDLE_AFTER_WAKE_DIAGNOSTIC " + json.dumps({
        "provider": config.get("name", "unknown"),
        "state": state,
    }, ensure_ascii=False))
    if sys.platform == "darwin":
        try:
            screenshot_path = f"/tmp/bridge-idle-after-wake-{int(time.time())}.png"
            await page.screenshot(path=screenshot_path)
            print("BRIDGE_IDLE_AFTER_WAKE_SCREENSHOT " + screenshot_path)
        except Exception as error:
            print("BRIDGE_IDLE_AFTER_WAKE_SCREENSHOT " + json.dumps({
                "captureError": type(error).__name__,
            }))


async def _has_visible_generation_marker(page, config: dict) -> bool:
    """Check every matched Stop control; the first DOM match may be hidden."""
    stop_selector = config.get("stop_selector")
    if not stop_selector:
        return False
    try:
        matches = await page.locator(stop_selector).all()
        for element in matches:
            try:
                if await element.is_visible():
                    return True
            except Exception:
                continue
        return False
    except Exception:
        # Some providers expose their stop affordance through accessible labels
        # even when their selector has become stale. Scan visible buttons only.
        try:
            return bool(await page.evaluate("""() => {
              const visible = node => {
                if (!node) return false;
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 0 && rect.height > 0 &&
                  style.visibility !== 'hidden' && style.display !== 'none';
              };
              const stopLabel = /^(?:stop(?:\\s+(?:response|responding|generating))?|dừng(?:\\s+(?:phản hồi|tạo))?|ngừng(?:\\s+(?:phản hồi|tạo))?)$/i;
              return Array.from(document.querySelectorAll('button, [role="button"]'))
                .filter(visible)
                .some(node => {
                  const label = (node.getAttribute('aria-label') ||
                    node.getAttribute('title') || node.innerText || '').trim();
                  const testId = (node.getAttribute('data-testid') || '').toLowerCase();
                  return stopLabel.test(label) || /(?:^|[-_])stop(?:[-_]button)?(?:$|[-_])/.test(testId);
                });
            }"""))
        except Exception:
            return False


async def _completed_chatgpt_response_text(
    page,
    config: dict,
    dom_text: str,
    *,
    is_json_requested: bool,
    streamed_text: str = "",
) -> str:
    """Prefer the browser's final response body only after UI completion."""
    if config.get("name") != "ChatGPT":
        return dom_text
    capture = getattr(page, "_avs_chatgpt_network_response_capture", None)
    if capture is None:
        return dom_text
    try:
        network_text = await capture.final_text(timeout=2.5)
        diagnostic = capture.diagnostic()
    except Exception as error:
        network_text = None
        diagnostic = {"errorType": type(error).__name__}
    finally:
        try:
            capture.detach()
        except Exception:
            pass

    candidate = clean_chatgpt_response_text(network_text or "")
    candidate = _safe_visible_stream_text(candidate, is_json_requested)
    if is_json_requested:
        candidate = valid_json_response_content(candidate) or ""
    stream_prefix = _safe_visible_stream_text(streamed_text, is_json_requested)
    if not candidate:
        source = "dom"
        reason = "network_final_unavailable"
    elif stream_prefix and not candidate.startswith(stream_prefix):
        source = "dom"
        reason = "network_final_did_not_match_stream_prefix"
        candidate = ""
    else:
        source = "network_final"
        reason = ""
    print("BRIDGE_RESPONSE_SOURCE " + json.dumps({
        "provider": "chatgpt",
        "source": source,
        "reason": reason,
        "network": diagnostic,
        "domTextChars": len(dom_text or ""),
        "selectedTextChars": len(candidate or dom_text or ""),
    }, ensure_ascii=False))
    return candidate or dom_text


async def wait_for_completion(
    page,
    config: dict,
    timeout: int = 120,
    submitted_prompt: Optional[str] = None,
    baseline_response_count: int = 0,
    baseline_user_message_count: Optional[int] = 0,
    expected_user_marker: Optional[str] = None,
    wake_retry: Optional[Any] = None,
    idle_generation_timeout_sec: float = 30.0,
    on_text_delta: Optional[Any] = None,
    is_json_requested: bool = False,
) -> str:
    """Theo dõi quá trình sinh phản hồi cho đến khi hoàn tất 100%."""
    response_selector = config["response_selector"]
    stop_selector = config.get("stop_selector")
    
    start_time = time.monotonic()
    idle_since = start_time
    wake_retry_count = 0
    dispatched_turn_uncertain = False
    provider_error_since = None
    last_progress_at = start_time
    invalid_json_last_text = ""
    invalid_json_unchanged_count = 0
    await asyncio.sleep(2.0)
    
    last_text = ""
    unchanged_count = 0
    streamed_visible_text = ""

    def publish_visible_text(raw_text: str) -> None:
        nonlocal streamed_visible_text
        if on_text_delta is None:
            return
        visible_text = _safe_visible_stream_text(raw_text, False)
        if visible_text.startswith(streamed_visible_text):
            delta = visible_text[len(streamed_visible_text):]
            if delta:
                on_text_delta(delta)
                streamed_visible_text = visible_text
    
    while time.monotonic() - start_time < timeout:
        await asyncio.sleep(0.8)
        
        is_generating = await _has_visible_generation_marker(page, config)

        # Auto-click "Continue generating" if ChatGPT paused mid-output
        if config.get("name") == "ChatGPT" and not is_generating:
            try:
                continue_btn = page.locator(
                    "button:has-text('Continue generating'), button:has-text('Tiếp tục tạo'), [aria-label*='Continue generating' i]"
                ).first
                if await continue_btn.is_visible():
                    print("BRIDGE_AUTO_CLICK_CONTINUE: Clicking Continue generating button...")
                    await continue_btn.click(timeout=3000)
                    await asyncio.sleep(1.5)
                    last_progress_at = time.monotonic()
                    continue
            except Exception:
                pass

        capture = getattr(page, "_avs_chatgpt_network_response_capture", None)
        network_response_recent = False
        if config.get("name") == "ChatGPT" and capture is not None:
            try:
                network_response_recent = bool(
                    capture.has_recent_active_response(idle_generation_timeout_sec)
                )
            except Exception:
                network_response_recent = False

        # A browser-owned final SSE message is a completion signal even when
        # React has not mounted or refreshed its transcript node yet. The
        # backend response body itself is consumed only after it finishes.
        if config.get("name") == "ChatGPT" and not is_generating:
            if capture is not None:
                try:
                    network_final = await capture.final_text(timeout=0.0)
                except Exception:
                    network_final = None
                if network_final:
                    resolved = await _completed_chatgpt_response_text(
                        page,
                        config,
                        "",
                        is_json_requested=is_json_requested,
                        streamed_text=streamed_visible_text,
                    )
                    if resolved:
                        if is_json_requested and valid_json_response_content(resolved) is None:
                            # Keep the existing DOM/error-recovery path for a
                            # final message that violates the requested schema.
                            pass
                        else:
                            publish_visible_text(resolved)
                            return resolved
                
        try:
            current_text = await read_latest_response_text(
                page,
                config,
                submitted_prompt=submitted_prompt,
                expected_user_marker=expected_user_marker,
            )
            if is_chatgpt_transient_error(current_text):
                current_text = ""
            if is_json_requested:
                json_content = valid_json_response_content(current_text)
                if json_content is None:
                    if current_text and not is_generating:
                        if current_text == invalid_json_last_text:
                            invalid_json_unchanged_count += 1
                        else:
                            invalid_json_last_text = current_text
                            invalid_json_unchanged_count = 0
                        if wake_retry_count >= 1 and invalid_json_unchanged_count >= 12:
                            raise HTTPException(
                                status_code=502,
                                detail={
                                    "code": "BRIDGE_INVALID_JSON_RESPONSE",
                                    "outcome": "provider_rejected",
                                    "retryable": False,
                                    "message": (
                                        "ChatGPT vẫn chưa trả JSON hợp lệ sau một lần "
                                        "đánh thức lại cùng lượt. Bridge dừng để không "
                                        "chuyển nội dung trung gian thành kết quả."
                                    ),
                                },
                            )
                    else:
                        invalid_json_last_text = ""
                        invalid_json_unchanged_count = 0
                    # A short status/tool fragment can appear before ChatGPT's
                    # final structured answer. It is not the result and must
                    # not be streamed into the app's JSON parser.
                    current_text = ""
                else:
                    current_text = json_content
                    invalid_json_last_text = ""
                    invalid_json_unchanged_count = 0
            if (
                config.get("name") == "ChatGPT"
                and _is_chatgpt_prompt_too_long_text(current_text)
            ):
                diagnostic = {
                    "code": "BRIDGE_PROMPT_TOO_LONG",
                    "outcome": "provider_rejected",
                    "retryable": False,
                    "message": (
                        "ChatGPT từ chối prompt vì vượt giới hạn tin nhắn. "
                        "Bridge đã dừng và không gửi lại nội dung dạng text."
                    ),
                }
                print("BRIDGE_PROMPT_TOO_LONG " + json.dumps(
                    {"provider": "chatgpt", "responseChars": len(current_text)},
                    ensure_ascii=False,
                ))
                raise HTTPException(status_code=413, detail=diagnostic)
            publish_visible_text(current_text)
            if not current_text:
                # A matched first-party stream can be active while ChatGPT's
                # Stop control is missing or stale. Prefer observed network
                # progress over the UI marker and keep this turn alive.
                if network_response_recent:
                    continue
                retryable_error_visible = (
                    config.get("name") == "ChatGPT"
                    and (
                        await _chatgpt_cloudflare_challenge_visible(page)
                        or await _chatgpt_retryable_error_visible(page)
                    )
                )
                now = time.monotonic()
                if retryable_error_visible and not is_generating:
                    if provider_error_since is None:
                        provider_error_since = now
                    if wake_retry_count == 0:
                        # A visible provider error is already proof that this
                        # attempt did not start generating. Do not wait the
                        # ordinary idle window before pressing its Retry.
                        idle_since = now - max(0.0, idle_generation_timeout_sec)
                    elif now - provider_error_since >= 8.0:
                        await _capture_idle_after_wake(page, config)
                        raise HTTPException(
                            status_code=502,
                            detail=(
                                "ChatGPT vẫn hiện thẻ lỗi có thể thử lại sau một lần "
                                "đánh thức. Lượt này đã dừng để tránh gửi trùng."
                            ),
                        )
                else:
                    provider_error_since = None
                if is_generating or wake_retry is None:
                    idle_since = None if is_generating else idle_since
                    continue

                # A visible user turn without a Stop marker or assistant reply
                # is the observed hung state the creator asked Bridge to wake.
                # Record that dispatch was uncertain, then permit the single
                # bounded recovery below. Never repeat it a second time.
                if config.get("name") == "ChatGPT" and submitted_prompt:
                    current_user_message_count = await _visible_user_message_count(
                        page, config
                    )
                    if (
                        baseline_user_message_count is None
                        or current_user_message_count is None
                        or current_user_message_count > baseline_user_message_count
                    ):
                        if not dispatched_turn_uncertain:
                            print("BRIDGE_DISPATCH_OUTCOME_UNCERTAIN " + json.dumps({
                                "provider": config.get("name", "unknown"),
                                "reason": (
                                    "submitted_user_message_visible"
                                    if current_user_message_count is not None
                                    and baseline_user_message_count is not None
                                    and current_user_message_count > baseline_user_message_count
                                    else "user_message_state_unreadable"
                                ),
                                "baselineUserMessageCount": baseline_user_message_count,
                                "currentUserMessageCount": current_user_message_count,
                                "action": "bounded_one_shot_replay",
                            }, ensure_ascii=False))
                        dispatched_turn_uncertain = True

                if idle_since is None:
                    idle_since = now
                    continue
                if now - idle_since < max(0.0, idle_generation_timeout_sec):
                    continue
                if wake_retry_count >= 1:
                    # One recovery was already dispatched. It may still be
                    # generating even when the page marker is missing; wait to
                    # the configured deadline rather than sending a third turn
                    # or reporting a fast false failure.
                    dispatched_turn_uncertain = True
                    continue

                # Count before invoking browser automation. If Playwright
                # raises midway through recovery, the exception path must not
                # re-enter this block and submit the same prompt repeatedly.
                wake_retry_count += 1
                try:
                    retry_info = await wake_retry()
                except HTTPException:
                    raise
                except Exception as error:
                    print("BRIDGE_WAKE_RETRY_FAILED " + json.dumps({
                        "provider": config.get("name", "unknown"),
                        "attempt": wake_retry_count,
                        "errorType": type(error).__name__,
                    }, ensure_ascii=False))
                    raise HTTPException(
                        status_code=502,
                        detail=(
                            f"{config.get('name', 'Provider')} không thể gửi lại "
                            "lượt sau khi không có dấu hiệu phản hồi. Lượt này "
                            "đã dừng sớm để tránh gửi trùng."
                        ),
                    ) from error
                if not retry_info:
                    raise HTTPException(
                        status_code=504,
                        detail={
                            "code": "BRIDGE_PROVIDER_OUTCOME_UNKNOWN",
                            "outcome": "unconfirmed",
                            "retryable": False,
                            "message": (
                                f"{config.get('name', 'Provider')} chưa trả lời sau "
                                "hai lần tải lại và quan sát cùng thread; Bridge "
                                "không gửi lặp yêu cầu để tránh nhân đôi lịch sử."
                            ),
                        },
                    )
                if retry_info.get("completed_text"):
                    recovered_text = str(retry_info["completed_text"])
                    if is_json_requested:
                        recovered_text = valid_json_response_content(recovered_text) or ""
                    recovered_text = await _completed_chatgpt_response_text(
                        page,
                        config,
                        recovered_text,
                        is_json_requested=is_json_requested,
                        streamed_text=streamed_visible_text,
                    )
                    if recovered_text:
                        print("BRIDGE_CHATGPT_LATE_REPLY_RECOVERED " + json.dumps({
                            "provider": config.get("name", "unknown"),
                            "responseTextChars": len(recovered_text),
                        }, ensure_ascii=False))
                        publish_visible_text(recovered_text)
                        return recovered_text
                    invalid_json_last_text = ""
                    invalid_json_unchanged_count = 0
                if retry_info.get("generation_visible"):
                    # Reload recovered an active generation, so no new prompt
                    # was dispatched. Keep the one-shot replay available in
                    # case this generation later stalls without a reply.
                    wake_retry_count = max(0, wake_retry_count - 1)
                    idle_since = None
                    last_text = ""
                    unchanged_count = 0
                    last_progress_at = time.monotonic()
                    print("BRIDGE_CHATGPT_RELOAD_FOUND_ACTIVE_GENERATION " + json.dumps({
                        "provider": config.get("name", "unknown"),
                        "action": "wait_without_replay",
                    }, ensure_ascii=False))
                    continue
                if retry_info.get("action") == "continue_observing_same_thread":
                    # The request already exists in this persistent role
                    # thread. Observe it again after a bounded reload; never
                    # resend it without a provider-owned Retry control.
                    wake_retry_count = max(0, wake_retry_count - 1)
                    idle_since = time.monotonic()
                    last_text = ""
                    unchanged_count = 0
                    last_progress_at = time.monotonic()
                    continue
                baseline_response_count = int(
                    retry_info.get("baseline_response_count", baseline_response_count)
                )
                idle_since = time.monotonic()
                last_text = ""
                unchanged_count = 0
                last_progress_at = time.monotonic()
                invalid_json_last_text = ""
                invalid_json_unchanged_count = 0
                print("BRIDGE_WAKE_RETRY " + json.dumps({
                    "provider": config.get("name", "unknown"),
                    "attempt": wake_retry_count,
                    "reason": "no_visible_generation_marker_or_reply",
                    "action": retry_info.get("action", "unknown"),
                    "baselineResponseCount": baseline_response_count,
                    "baselineUserMessageCount": baseline_user_message_count,
                }, ensure_ascii=False))
                continue
            idle_since = None
            provider_error_since = None
            
            if (
                current_text
                and current_text == last_text
                and not is_generating
                and not network_response_recent
            ):
                if is_json_requested:
                    parsed_json = valid_json_response_content(current_text)
                    if parsed_json is None:
                        # Output does NOT contain valid JSON yet!
                        # Check if ChatGPT stopped on a short non-JSON status (like 236 bytes)
                        if wake_retry_count == 0 and len(current_text) < 600 and unchanged_count >= 5:
                            if wake_retry is not None:
                                print("BRIDGE_QUALITY_GATE_TRIGGER_RETRY: non-JSON status detected, triggering wake_retry...")
                                wake_retry_count += 1
                                try:
                                    retry_info = await wake_retry()
                                    if retry_info and retry_info.get("completed_text"):
                                        rec = str(retry_info["completed_text"])
                                        if valid_json_response_content(rec):
                                            publish_visible_text(rec)
                                            return rec
                                except Exception as wr_err:
                                    print(f"BRIDGE_QUALITY_GATE_WAKE_ERR: {wr_err}")
                                unchanged_count = 0
                                continue
                        unchanged_count = 0
                        continue

                unchanged_count += 1
                if unchanged_count >= (3 if is_json_requested else 12):
                    current_text = await _completed_chatgpt_response_text(
                        page,
                        config,
                        current_text,
                        is_json_requested=is_json_requested,
                        streamed_text=streamed_visible_text,
                    )
                    publish_visible_text(current_text)
                    return current_text
            else:
                unchanged_count = 0
                if current_text != last_text:
                    last_progress_at = time.monotonic()
                last_text = current_text
        except HTTPException:
            capture = getattr(page, "_avs_chatgpt_network_response_capture", None)
            if capture is not None:
                try:
                    capture.detach()
                except Exception:
                    pass
            raise
        except Exception:
            continue
            
    # Make one final read after the deadline check. A completed ChatGPT answer
    # can land between the last polling sleep and the timeout boundary; the
    # prior run's screenshot captured that exact race at 300 seconds.
    final_text = ""
    is_generating = False
    try:
        final_text = await read_latest_response_text(
            page, config, submitted_prompt=submitted_prompt
        )
        if is_chatgpt_transient_error(final_text):
            final_text = ""
        if is_json_requested:
            final_text = valid_json_response_content(final_text) or ""
        is_generating = await _has_visible_generation_marker(page, config)
        if final_text and not is_generating:
            if time.monotonic() - last_progress_at >= 8.0:
                final_text = await _completed_chatgpt_response_text(
                    page,
                    config,
                    final_text,
                    is_json_requested=is_json_requested,
                    streamed_text=streamed_visible_text,
                )
                publish_visible_text(final_text)
                return final_text
    except Exception:
        pass

    try:
        diagnostic = await page.evaluate("""(cfg) => {
          const visible = (node) => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 &&
              style.visibility !== 'hidden' && style.display !== 'none';
          };
          const responses = Array.from(document.querySelectorAll(cfg.responseSelector));
          const userMessages = Array.from(document.querySelectorAll(
            '[data-message-author-role="user"]'
          ));
          const conversationTurns = Array.from(document.querySelectorAll(
            '[data-testid^="conversation-turn-"]'
          ));
          const composer = document.querySelector(cfg.inputSelector);
          const stop = cfg.stopSelector
            ? document.querySelector(cfg.stopSelector)
            : null;
          const bodyText = (document.body?.innerText || '').toLowerCase();
          const composerText = composer
            ? ('value' in composer ? composer.value : composer.innerText || '')
            : '';
          const alertTexts = Array.from(document.querySelectorAll(
            '[role="alert"], [aria-live="assertive"], [data-testid*="error"]'
          ))
            .filter(visible)
            .map((node) => (node.innerText || '').replaceAll('\\n', ' ').replaceAll('\\r', ' ').trim())
            .filter(Boolean)
            .slice(0, 5)
            .map((text) => text.slice(0, 180));
          const statusKeywords = [
            'error', 'failed', 'try again', 'something went wrong', 'network',
            'unavailable', 'cloud', 'quota', 'limit', 'login', 'sign in',
            'đăng nhập', 'tải lại', 'lỗi', 'unable'
          ];
          const statusLines = (document.body?.innerText || '')
            .split(String.fromCharCode(10))
            .map((line) => line.trim())
            .filter((line) => line && statusKeywords.some((keyword) =>
              line.toLowerCase().includes(keyword)))
            .slice(-8)
            .map((line) => line.slice(0, 160));
          const actionButtons = Array.from(document.querySelectorAll('button'))
            .filter(visible)
            .map((button) => {
              const label = (
                button.getAttribute('aria-label') ||
                button.getAttribute('data-testid') ||
                button.innerText ||
                ''
              ).toLowerCase();
              const category = /stop|dừng|ngừng/.test(label) ? 'stop'
                : /regenerate|retry|try again|thử lại|tạo lại/.test(label) ? 'retry'
                : /send|submit/.test(label) ? 'send'
                : /upload|attach/.test(label) ? 'upload' : null;
              return category ? { category, disabled: !!button.disabled } : null;
            })
            .filter(Boolean)
            .slice(0, 12);
          const main = document.querySelector('main') || document.body;
          const visibleMainNodes = Array.from(main.querySelectorAll('*'))
            .filter((node) => visible(node));
          const messageNodeCandidates = visibleMainNodes
            .filter((node) => node.children.length === 0 ||
              (typeof node.className === 'string' && node.className.includes('group/')))
            .map((node) => ({
              tag: node.tagName.toLowerCase(),
              role: node.getAttribute('role') || '',
              testId: node.getAttribute('data-testid') || '',
              className: (typeof node.className === 'string' ? node.className : '').slice(0, 100),
              textChars: (node.innerText || '').trim().length,
              childCount: node.children.length,
            }))
            .filter((node) => node.textChars > 0)
            .sort((a, b) => b.textChars - a.textChars)
            .slice(0, 32);
          const path = location.pathname.toLowerCase();
          return {
            provider: cfg.provider,
            pageKind: path.includes('/auth/') || path.includes('/login')
              ? 'login'
              : path.includes('/c/') ? 'conversation' : 'provider_page',
            titleChars: document.title.length,
            composerVisible: visible(composer),
            composerTextChars: composerText.length,
            userMessageCount: userMessages.length,
            lastUserMessageChars: userMessages.length
              ? (userMessages[userMessages.length - 1].innerText || '').length : 0,
            conversationTurnCount: conversationTurns.length,
            responseCount: responses.length,
            responseTextChars: responses.slice(-2).map((node) =>
              (node.innerText || '').length),
            messageAttributeCount: document.querySelectorAll('[data-message-author-role]').length,
            conversationTurnSelectorCount: document.querySelectorAll(
              '[data-testid^="conversation-turn-"]'
            ).length,
            mainTextChars: (main.innerText || '').length,
            messageNodeCandidates: messageNodeCandidates,
            stopVisible: visible(stop),
            loginTextVisible: /sign in|log in|đăng nhập/.test(bodyText),
            errorTextVisible: /something went wrong|try again|lỗi|error/.test(bodyText),
            alertTexts: alertTexts,
            statusLines: statusLines,
            actionButtons: actionButtons,
          };
        }""", {
            "provider": config.get("name", "unknown"),
            "responseSelector": response_selector,
            "inputSelector": config.get("input_selector", ""),
            "stopSelector": stop_selector,
        })
        print("BRIDGE_WAIT_TIMEOUT_DIAGNOSTIC " + json.dumps(diagnostic, ensure_ascii=False))
        if sys.platform == "darwin":
            await page.screenshot(path="/tmp/bridge-wait-timeout.png")
    except Exception as error:
        print("BRIDGE_WAIT_TIMEOUT_DIAGNOSTIC " + json.dumps({
            "provider": config.get("name", "unknown"),
            "captureError": type(error).__name__,
            "responseTextChars": len(last_text),
        }, ensure_ascii=False))
    capture = getattr(page, "_avs_chatgpt_network_response_capture", None)
    if capture is not None:
        try:
            capture.detach()
        except Exception:
            pass
    if is_generating:
        raise HTTPException(
            status_code=504,
            detail={
                "code": "BRIDGE_PROVIDER_STILL_GENERATING",
                "outcome": "in_progress",
                "retryable": False,
                "responseTextChars": len(final_text or last_text),
                "message": (
                    "Provider vẫn đang hiển thị nút dừng sinh câu trả lời khi hết "
                    "thời hạn chờ. Đây là phản hồi chậm còn đang chạy, không phải "
                    "lỗi đã xác nhận; không gửi lại prompt này."
                ),
            },
        )
    if final_text or last_text:
        raise HTTPException(
            status_code=504,
            detail={
                "code": "BRIDGE_PROVIDER_OUTCOME_UNKNOWN",
                "outcome": "unconfirmed",
                "retryable": False,
                "responseTextChars": len(final_text or last_text),
                "message": (
                    "Provider chưa có dấu hiệu lỗi rõ ràng nhưng câu trả lời cũng "
                    "chưa ổn định đủ lâu để xác nhận hoàn tất. Không gửi lại prompt "
                    "này; cần kiểm tra lượt hiện tại trước."
                ),
            },
        )
    if dispatched_turn_uncertain or wake_retry_count > 0:
        raise HTTPException(
            status_code=504,
            detail={
                "code": "BRIDGE_PROVIDER_OUTCOME_UNKNOWN",
                "outcome": "unconfirmed",
                "retryable": False,
                "responseTextChars": 0,
                "message": (
                    "ChatGPT đã nhận prompt nhưng Bridge không xác nhận được câu trả lời "
                    "trong thời hạn chờ. Không gửi lại để tránh tạo lượt trùng; cần kiểm "
                    "tra cuộc trò chuyện hiện tại."
                ),
            },
        )
    return last_text

async def stream_generator(
    page,
    config: dict,
    response_id: str,
    model_name: str,
    *,
    provider: str,
    temporary_chat: bool,
    stateless: bool,
    lock: asyncio.Lock,
    timeout: int = 120,
    submitted_prompt: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Cơ chế Streaming SSE token-by-token chuẩn Server-Sent Events."""
    response_selector = config["response_selector"]
    stop_selector = config.get("stop_selector")
    cleanup_state = "not_requested"
    cleanup_done = False
    try:
        start_time = time.time()
        await asyncio.sleep(1.5)
        sent_len = 0
        unchanged_count = 0
        last_raw_text = ""
        completed = False
        while time.time() - start_time < timeout:
            await asyncio.sleep(0.3)
            is_generating = await _has_visible_generation_marker(page, config)
            try:
                current_text = await read_latest_response_text(
                    page, config, submitted_prompt=submitted_prompt
                )
                if not current_text:
                    continue
                if len(current_text) > sent_len:
                    new_chunk = current_text[sent_len:]
                    sent_len = len(current_text)
                    unchanged_count = 0
                    last_raw_text = current_text
                    payload = {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model_name,
                        "choices": [{
                            "index": 0,
                            "delta": {"content": new_chunk},
                            "finish_reason": None
                        }]
                    }
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                elif current_text == last_raw_text and not is_generating:
                    unchanged_count += 1
                    if unchanged_count >= 5:
                        completed = True
                        break
            except Exception:
                continue
        if not completed:
            still_generating = await _has_visible_generation_marker(page, config)
            if still_generating:
                cleanup_state = "active_response_preserved"
                cleanup_done = True
                error_payload = {
                    "error": {
                        "code": "BRIDGE_PROVIDER_STILL_GENERATING",
                        "outcome": "in_progress",
                        "retryable": False,
                        "message": (
                            "Provider vẫn đang sinh câu trả lời khi hết thời hạn "
                            "stream; không gửi lại prompt này."
                        ),
                    }
                }
                yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return
            error_payload = {
                "error": {
                    "code": "BRIDGE_PROVIDER_OUTCOME_UNKNOWN",
                    "outcome": "unconfirmed",
                    "retryable": False,
                    "message": (
                        "Stream hết thời gian trước khi phản hồi ổn định; không "
                        "gửi lại prompt này cho đến khi xác minh lượt hiện tại."
                    ),
                }
            }
            yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
            return
        if stateless:
            cleanup_state = await cleanup_provider_session(
                page,
                provider,
                temporary_chat,
                config,
                retain_ephemeral_context=True,
            )
            cleanup_done = True
        final_payload = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model_name,
            "session_cleanup": cleanup_state,
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "stop"
            }]
        }
        yield f"data: {json.dumps(final_payload, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    finally:
        try:
            if stateless and not cleanup_done:
                await cleanup_provider_session(page, provider, temporary_chat, config)
        finally:
            if lock.locked():
                lock.release()


def role_thread_stream_generator(
    page,
    config: dict,
    response_id: str,
    model_name: str,
    *,
    provider: str,
    role_thread_id: str,
    timeout: int,
    submitted_prompt: str,
    baseline_response_count: int,
    baseline_user_message_count: Optional[int],
    expected_user_marker: Optional[str],
    wake_retry: Optional[Any],
    is_json_requested: bool,
    prompt_token_count: int,
    role_lock: asyncio.Lock,
    semaphore: asyncio.Semaphore,
) -> AsyncGenerator[str, None]:
    """Stream a persistent role turn while retaining its provider-thread lock."""
    queue: asyncio.Queue = asyncio.Queue()

    def publish_delta(delta: str) -> None:
        if delta:
            queue.put_nowait(("delta", delta))

    async def produce() -> None:
        try:
            raw_output = await wait_for_completion(
                page,
                config,
                timeout=timeout,
                submitted_prompt=submitted_prompt,
                baseline_response_count=baseline_response_count,
                baseline_user_message_count=baseline_user_message_count,
                expected_user_marker=expected_user_marker,
                wake_retry=wake_retry,
                on_text_delta=publish_delta,
                is_json_requested=is_json_requested,
            )
            if not raw_output:
                raise HTTPException(
                    status_code=504,
                    detail={
                        "code": "BRIDGE_PROVIDER_OUTCOME_UNKNOWN",
                        "outcome": "unconfirmed",
                        "retryable": False,
                        "message": "Bridge chưa xác nhận được câu trả lời cho thread vai trò.",
                    },
                )
            try:
                await _save_role_thread_record(provider, role_thread_id, page.url)
            except HTTPException as error:
                print("BRIDGE_ROLE_THREAD_URL_SAVE_FAILED " + json.dumps({
                    "provider": provider,
                    "roleThreadId": role_thread_id,
                    "stage": "after_stream_completion",
                    "status": error.status_code,
                }, ensure_ascii=False))
            clean_content, _reasoning_content = extract_and_clean_output(
                raw_output, is_json_requested
            )
            now = int(time.time())
            queue.put_nowait(("payload", {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": now,
                "model": model_name,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                    "avs_final_text": clean_content,
                }],
                "usage": {
                    "prompt_tokens": prompt_token_count,
                    "completion_tokens": len(clean_content.split()),
                    "total_tokens": prompt_token_count + len(clean_content.split()),
                },
            }))
            queue.put_nowait(("done", None))
        except HTTPException as error:
            detail = error.detail
            error_payload = detail if isinstance(detail, dict) else {
                "code": "BRIDGE_ROLE_STREAM_FAILED",
                "outcome": "unconfirmed",
                "retryable": False,
                "message": str(detail),
            }
            print("BRIDGE_ROLE_STREAM_ERROR " + json.dumps({
                "provider": provider,
                "status": error.status_code,
                "code": str(error_payload.get("code") or "BRIDGE_ROLE_STREAM_FAILED")[:80],
                "outcome": str(error_payload.get("outcome") or "unconfirmed")[:40],
                "retryable": error_payload.get("retryable") is True,
            }, ensure_ascii=False))
            queue.put_nowait(("payload", {"error": error_payload}))
            queue.put_nowait(("done", None))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            queue.put_nowait(("payload", {"error": {
                "code": "BRIDGE_ROLE_STREAM_FAILED",
                "outcome": "unconfirmed",
                "retryable": False,
                "message": f"Bridge role stream failed: {type(error).__name__}",
            }}))
            queue.put_nowait(("done", None))
        finally:
            if role_lock.locked():
                role_lock.release()
            semaphore.release()
            queue.put_nowait(("close", None))

    producer = asyncio.create_task(produce())

    async def stream_items() -> AsyncGenerator[str, None]:
        while True:
            kind, value = await queue.get()
            if kind == "close":
                break
            if kind == "delta":
                payload = {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model_name,
                    "choices": [{
                        "index": 0,
                        "delta": {"content": value},
                        "finish_reason": None,
                    }],
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            elif kind == "payload":
                yield f"data: {json.dumps(value, ensure_ascii=False)}\n\n"
            elif kind == "done":
                yield "data: [DONE]\n\n"
        await producer

    # Start the producer before returning the response so a client disconnect
    # cannot strand the persistent role and provider-capacity locks.
    return stream_items()


def session_stream_generator(
    page,
    config: dict,
    response_id: str,
    model_name: str,
    *,
    provider: str,
    session_id: str,
    timeout: int,
    submitted_prompt: str,
    baseline_response_count: int,
    baseline_user_message_count: Optional[int],
    expected_user_marker: Optional[str],
    is_json_requested: bool,
    prompt_token_count: int,
    session_lock: asyncio.Lock,
    semaphore: asyncio.Semaphore,
    persist: bool,
    tools: Optional[List[Dict[str, Any]]] = None,
    stream_options: Optional[Dict[str, Any]] = None,
    system_hash: Optional[str] = None,
    session_key: str = "",
) -> AsyncGenerator[str, None]:
    """Stream a multi-tab session turn with OpenAI SSE formatting, tool calling, and lock management."""
    queue: asyncio.Queue = asyncio.Queue()

    def publish_delta(delta: str) -> None:
        if delta:
            queue.put_nowait(("delta", delta))

    async def produce() -> None:
        try:
            raw_output = await wait_for_completion(
                page,
                config,
                timeout=timeout,
                submitted_prompt=submitted_prompt,
                baseline_response_count=baseline_response_count,
                baseline_user_message_count=baseline_user_message_count,
                expected_user_marker=expected_user_marker,
                on_text_delta=publish_delta,
                is_json_requested=is_json_requested,
            )
            if not raw_output:
                raise HTTPException(
                    status_code=504,
                    detail={
                        "code": "BRIDGE_SESSION_STREAM_TIMEOUT",
                        "outcome": "unconfirmed",
                        "retryable": False,
                        "message": "Bridge không nhận được phản hồi từ giao diện web trong thời hạn của yêu cầu.",
                    },
                )
            if persist:
                _save_factory_url(provider, session_id, page, "after_stream_completion")

            clean_content, reasoning_content = extract_and_clean_output(raw_output, is_json_requested)

            active_content, tool_calls = parse_tool_calls_from_response(clean_content, tools)

            total_resp_chars = len(clean_content)
            native_tracker.record_turn(
                session_id,
                prompt_chars=len(submitted_prompt),
                response_chars=total_resp_chars,
                system_hash=system_hash,
            )

            completion_tokens = len(clean_content.split())
            usage = {
                "prompt_tokens": prompt_token_count,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_token_count + completion_tokens,
            }

            now = int(time.time())
            if reasoning_content:
                queue.put_nowait(("payload", {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": now,
                    "model": model_name,
                    "choices": [{
                        "index": 0,
                        "delta": {"reasoning_content": reasoning_content},
                        "finish_reason": None,
                    }]
                }))
            if tools and active_content:
                queue.put_nowait(("payload", {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": now,
                    "model": model_name,
                    "choices": [{
                        "index": 0,
                        "delta": {"content": active_content},
                        "finish_reason": None,
                    }]
                }))
            if tool_calls:
                for idx, tc in enumerate(tool_calls):
                    fn_dict = tc.get("function", {}) if isinstance(tc, dict) else {}
                    tc_chunk = {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": now,
                        "model": model_name,
                        "choices": [{
                            "index": 0,
                            "delta": {
                                "tool_calls": [{
                                    "index": idx,
                                    "id": tc.get("id"),
                                    "type": "function",
                                    "function": {
                                        "name": fn_dict.get("name"),
                                        "arguments": fn_dict.get("arguments", "{}"),
                                    }
                                }]
                            },
                            "finish_reason": None,
                        }]
                    }
                    queue.put_nowait(("payload", tc_chunk))

                final_chunk = {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": now,
                    "model": model_name,
                    "choices": [{
                        "index": 0,
                        "delta": {},
                        "finish_reason": "tool_calls",
                    }]
                }
                queue.put_nowait(("payload", final_chunk))
            else:
                final_chunk = {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": now,
                    "model": model_name,
                    "choices": [{
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }]
                }
                queue.put_nowait(("payload", final_chunk))

            if isinstance(stream_options, dict) and stream_options.get("include_usage"):
                usage_chunk = {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": now,
                    "model": model_name,
                    "choices": [],
                    "usage": usage,
                }
                queue.put_nowait(("payload", usage_chunk))

            queue.put_nowait(("done", None))
        except HTTPException as error:
            detail = error.detail
            error_payload = detail if isinstance(detail, dict) else {
                "code": "BRIDGE_SESSION_STREAM_FAILED",
                "outcome": "unconfirmed",
                "retryable": False,
                "message": str(detail),
            }
            queue.put_nowait(("payload", {"error": error_payload}))
            queue.put_nowait(("done", None))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            queue.put_nowait(("payload", {"error": {
                "code": "BRIDGE_SESSION_STREAM_FAILED",
                "outcome": "unconfirmed",
                "retryable": False,
                "message": f"Bridge session stream failed: {type(error).__name__}: {error}",
            }}))
            queue.put_nowait(("done", None))
        finally:
            if session_key:
                session_last_used[session_key] = time.time()
            if session_lock.locked():
                session_lock.release()
            semaphore.release()
            queue.put_nowait(("close", None))

    producer = asyncio.create_task(produce())

    async def stream_items() -> AsyncGenerator[str, None]:
        # Emit initial role delta chunk
        initial_role_chunk = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model_name,
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": ""},
                "finish_reason": None,
            }],
        }
        yield f"data: {json.dumps(initial_role_chunk, ensure_ascii=False)}\n\n"

        while True:
            kind, value = await queue.get()
            if kind == "close":
                break
            if kind == "delta":
                # Only stream delta tokens if no tools are declared (tools require buffering to avoid partial leaks)
                if not tools:
                    payload = {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model_name,
                        "choices": [{
                            "index": 0,
                            "delta": {"content": value},
                            "finish_reason": None,
                        }],
                    }
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            elif kind == "payload":
                yield f"data: {json.dumps(value, ensure_ascii=False)}\n\n"
            elif kind == "done":
                yield "data: [DONE]\n\n"
        await producer

    return stream_items()

def build_avs_model_payload(model_id: str, meta: dict) -> dict:
    """Tạo payload model chuẩn OpenAI + tương thích 100% với AI Video Studio (AVS) & Codex catalog."""
    return {
        "id": model_id,
        "slug": model_id,
        "name": model_id,
        "display_name": meta.get("display_name", model_id),
        "displayName": meta.get("display_name", model_id),
        "description": meta.get("description", ""),
        "object": "model",
        "created": meta.get("created", int(time.time())),
        "owned_by": meta.get("owned_by", "openai"),
        "ownedBy": meta.get("owned_by", "openai"),
        "supportsImageInput": meta.get("supports_vision", False),
        "input_modalities": ["text", "image"] if meta.get("supports_vision", False) else ["text"],
        "capabilities": {
            "vision": meta.get("supports_vision", False),
            "chat": True,
            "reasoning": bool(meta.get("supported_reasoning_levels", [])),
        },
        "supported_reasoning_levels": meta.get("supported_reasoning_levels", []),
        "permission": [],
        "root": model_id,
        "parent": None
    }


@app.on_event("startup")
async def startup():
    global provider_ui_locks, factory_semaphore
    provider_ui_locks = {provider: asyncio.Lock() for provider in PROVIDERS}
    factory_semaphore = asyncio.Semaphore(5)
    os.makedirs(BRIDGE_PROFILES_DIR, exist_ok=True)
    for p in PROVIDERS:
        os.makedirs(os.path.join(BRIDGE_PROFILES_DIR, p), exist_ok=True)
    print("=" * 70)
    print("🚀 MULTI-PROVIDER WEB BRIDGE v3.7 — AVS UI SYNC GATEWAY")
    print(f"📌 OpenAI-Compatible Endpoint: http://127.0.0.1:{BRIDGE_PORT}/v1")
    print("🎬 Tự động cung cấp danh mục Model & Mức suy luận (Reasoning Effort)")
    print("=" * 70)
    if sys.platform != "darwin" and os.environ.get("BRIDGE_AUTO_INIT_PROVIDER", "1") == "1":
        # On Linux (Docker with Xvfb + noVNC), launch ChatGPT on startup so the desktop is never black
        asyncio.create_task(init_provider("chatgpt"))

@app.get("/v1/models")
async def list_models(
    provider: str = "chatgpt",
    refresh: bool = False,
    client_version: Optional[str] = None,
):
    """Read model choices from one signed-in provider's visible model picker."""
    catalog = await _provider_model_catalog(provider.lower(), force=refresh)
    return {
        "object": "list",
        "provider": provider.lower(),
        "selected_model": catalog["state"].get("selected_model"),
        "reasoning_effort": catalog["state"].get("reasoning_effort", "none"),
        "reasoning_label": catalog["state"].get("reasoning_label"),
        "data": catalog["models"],
    }

@app.get("/v1/models/{model_id}")
async def get_model(model_id: str, provider: Optional[str] = None):
    provider_id = resolve_provider(model_id, provider)
    catalog = await _provider_model_catalog(provider_id)
    match = next(
        (row for row in catalog["models"] if row["id"].casefold() == model_id.casefold()),
        None,
    )
    if match is None:
        raise HTTPException(status_code=404, detail="Model không có trong danh sách của tài khoản provider này.")
    return match

class FactorySessionCloseRequest(BaseModel):
    provider: str = "chatgpt"
    session_id: str
    delete_conversation: Optional[bool] = False  # worker đã xong: xóa luôn cuộc trò chuyện do phiên này tạo
    forget: Optional[bool] = True  # bỏ địa chỉ cuộc trò chuyện đã lưu (False: chỉ đóng tab, lần sau còn mở lại được)


@app.post("/v1/factory/sessions/close")
async def close_factory_session(req: FactorySessionCloseRequest):
    """Đóng tab của một phiên App Factory (ví dụ worker đã xong); tùy chọn xóa cuộc trò chuyện do chính phiên đó tạo. Không đụng phiên khác."""
    provider = resolve_provider(None, req.provider)
    session_id = (req.session_id or "").strip()
    if not session_id or session_id.startswith("avs-role-v1-"):
        raise HTTPException(status_code=400, detail="session_id không hợp lệ.")
    key = f"{provider}:{session_id}"
    s_lock = session_locks.setdefault(key, asyncio.Lock())
    async with s_lock:
        page = session_pages.get(key)
        live = page is not None and not page.is_closed()
        url = page.url if live and _factory_url_ok(provider, page.url) else None
        if url is None:
            saved = _factory_store(provider).get(session_id)
            url = saved if _factory_url_ok(provider, saved) else None
        cid = factory_sessions.conversation_id(url) if url else None
        deleted = None
        if req.delete_conversation and cid:
            deleted = await _delete_provider_conversation(provider, page if live else None, cid)
        await _discard_factory_page(provider, session_id, forget=bool(req.forget) or bool(req.delete_conversation))
    return {"session_id": session_id, "closed": live, "conversation_id": cid, "deleted": deleted}


class FactoryBuildRequest(BaseModel):
    project_id: str
    iteration: int = 1
    install_cmd: Optional[str] = ""
    test_cmd: Optional[str] = ""
    deploy_cmd: Optional[str] = ""
    backup_cmd: Optional[str] = ""
    rollback_cmd: Optional[str] = ""
    target: Optional[str] = ""
    artifacts: Optional[List[str]] = None
    zip_base64: Optional[str] = None
    auto_android: Optional[bool] = True


@app.post("/v1/factory/build")
async def execute_factory_build(req: FactoryBuildRequest):
    """Remote Build Runner trên macOS: chạy build với Flutter SDK, Android SDK, Gradle và auto-install ADB."""
    import base64, io, shutil, zipfile, subprocess, glob, hashlib
    
    workspace = os.path.expanduser(f"~/ai-software-factory/projects/{req.project_id}/build_workspace")
    staging_workspace = os.path.expanduser(f"~/ai-software-factory/projects/{req.project_id}/staging_build/iter_{req.iteration}")
    os.makedirs(workspace, exist_ok=True)
    if os.path.exists(staging_workspace):
        shutil.rmtree(staging_workspace, ignore_errors=True)
    os.makedirs(staging_workspace, exist_ok=True)

    combined_logs = []
    
    if req.zip_base64:
        try:
            zip_bytes = base64.b64decode(req.zip_base64)
            # Bảo tồn cấu hình Gradle tối ưu từ workspace nếu có
            gradle_prop_path = os.path.join(workspace, "android/gradle/wrapper/gradle-wrapper.properties")
            saved_gradle_props = None
            if os.path.exists(gradle_prop_path):
                with open(gradle_prop_path, "r", encoding="utf-8") as f:
                    saved_gradle_props = f.read()

            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                zf.extractall(staging_workspace)

            staging_gradle_prop = os.path.join(staging_workspace, "android/gradle/wrapper/gradle-wrapper.properties")
            if saved_gradle_props and os.path.exists(os.path.dirname(staging_gradle_prop)):
                with open(staging_gradle_prop, "w", encoding="utf-8") as f:
                    f.write(saved_gradle_props)
            combined_logs.append(f"📦 [Mac Build Runner] Đã giải nén mã nguồn vào thư mục tạm: {staging_workspace}")
        except Exception as e:
            return {"passed": False, "exit_code": 1, "failed_step": "EXTRACT", "log": f"Lỗi giải nén ZIP: {e}"}

    # Cấu hình biến môi trường macOS
    java_home = "/Applications/Android Studio.app/Contents/jbr/Contents/Home"
    android_home = os.path.expanduser("~/Library/Android/sdk")
    env = os.environ.copy()
    if os.path.exists(java_home):
        env["JAVA_HOME"] = java_home
    if os.path.exists(android_home):
        env["ANDROID_HOME"] = android_home
    env["PATH"] = f"/opt/homebrew/bin:/usr/local/bin:{java_home}/bin:{android_home}/platform-tools:{env.get('PATH', '')}"

    def run_cmd(cmd_str: str, cwd: str = staging_workspace, timeout_sec: int = 300) -> tuple:
        if not cmd_str or not cmd_str.strip():
            return 0, ""
        try:
            proc = subprocess.Popen(
                ["/bin/bash", "-c", cmd_str],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True
            )
            out, _ = proc.communicate(timeout=timeout_sec)
            return proc.returncode, out
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            return 124, f"Timeout sau {timeout_sec}s:\n{out or ''}"
        except Exception as err:
            return 1, f"Lỗi thực thi `{cmd_str}`: {err}"

    steps = [
        ("BACKUP", req.backup_cmd, 120),
        ("INSTALL", req.install_cmd, 300),
        ("TEST/BUILD", req.test_cmd, 300),
    ]

    exit_code = 0
    failed_step = ""
    for label, cmd, limit in steps:
        if not (cmd and cmd.strip()):
            continue
        combined_logs.append(f"=== [{label}]: {cmd} ===")
        code, out = await asyncio.to_thread(run_cmd, cmd, staging_workspace, limit)
        combined_logs.append(f"{out.strip()}\n(exit={code})")
        if code != 0:
            exit_code, failed_step = code, label
            break

    apk_installed = False
    connected_device = ""
    is_flutter = os.path.exists(os.path.join(staging_workspace, "pubspec.yaml"))
    
    if exit_code == 0 and req.auto_android and is_flutter:
        combined_logs.append("=== [MAC FLUTTER & ANDROID AUTO-BUILD PIPELINE] ===")
        c_pub, out_pub = await asyncio.to_thread(run_cmd, "flutter pub get", staging_workspace, 180)
        combined_logs.append(f"flutter pub get (exit={c_pub})\n{out_pub}")
        if c_pub == 0:
            c_apk, out_apk = await asyncio.to_thread(run_cmd, "flutter build apk --debug", staging_workspace, 300)
            combined_logs.append(f"flutter build apk --debug (exit={c_apk})\n{out_apk}")
            if c_apk != 0:
                exit_code, failed_step = c_apk, "FLUTTER_BUILD_APK"
        else:
            exit_code, failed_step = c_pub, "FLUTTER_PUB_GET"

    if exit_code != 0:
        combined_logs.append(f"🛡️ [Staging Gate] Mac Build FAIL ở bước {failed_step}! Main workspace được giữ nguyên 100%.")
    else:
        # Build PASS: Đồng bộ từ staging_workspace lên workspace (main) bảo tồn .git
        try:
            for item in os.listdir(workspace):
                if item == ".git":
                    continue
                p = os.path.join(workspace, item)
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            shutil.copytree(staging_workspace, workspace, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
            combined_logs.append(f"✅ [Staging Gate] Mac Build PASS! Đã đồng bộ mã nguồn lên main (build_workspace).")
        except Exception as sync_err:
            combined_logs.append(f"⚠️ [Staging Gate] Lỗi đồng bộ lên main: {sync_err}")

        # Chạy DEPLOY nếu có
        if req.deploy_cmd and req.deploy_cmd.strip():
            combined_logs.append(f"=== [DEPLOY]: {req.deploy_cmd} ===")
            code_dep, out_dep = await asyncio.to_thread(run_cmd, req.deploy_cmd, workspace, 300)
            combined_logs.append(f"{out_dep.strip()}\n(exit={code_dep})")
            if code_dep != 0:
                exit_code, failed_step = code_dep, "DEPLOY"

        # Nếu có build APK thành công và auto_android, cài đặt vào máy thật qua ADB
        if exit_code == 0 and req.auto_android and is_flutter:
            apk_path = os.path.join(workspace, "build/app/outputs/flutter-apk/app-debug.apk")
            if os.path.exists(apk_path):
                c_dev, out_dev = await asyncio.to_thread(run_cmd, "adb devices | awk 'NR>1 && $2==\"device\" {print $1; exit}'", workspace, 15)
                dev_serial = out_dev.strip()
                if dev_serial:
                    connected_device = dev_serial
                    combined_logs.append(f"📱 Tìm thấy thiết bị Android: {dev_serial}. Đang cài APK...")
                    c_inst, out_inst = await asyncio.to_thread(run_cmd, f"adb -s '{dev_serial}' install -r --user 0 '{apk_path}'", workspace, 60)
                    combined_logs.append(f"adb install (exit={c_inst})\n{out_inst}")
                    if c_inst == 0:
                        apk_installed = True
                        pkg_id = "com.example.vuong_quoc_tri_tue"
                        c_open, _ = await asyncio.to_thread(run_cmd, f"adb -s '{dev_serial}' shell monkey -p {pkg_id} -c android.intent.category.LAUNCHER 1", workspace, 15)
                        combined_logs.append(f"🎉 Đã tự động cài đặt và khởi chạy {pkg_id} trên {dev_serial} thành công!")

    passed = exit_code == 0
    full_log_text = "\n\n".join(combined_logs).strip() or "Không có output log."
    
    return {
        "passed": passed,
        "exit_code": exit_code,
        "failed_step": failed_step,
        "log": full_log_text,
        "apk_installed": apk_installed,
        "device": connected_device,
    }


@app.get("/v1/bridge/session-state")
async def get_session_state(provider: str = "chatgpt"):
    ctx = contexts.get(provider)
    if not ctx:
        await init_provider(provider)
        ctx = contexts.get(provider)
    if not ctx:
        raise HTTPException(status_code=404, detail="Provider context not found")
    state = await ctx.storage_state()
    return JSONResponse(content=state)


@app.post("/v1/bridge/session-state")
async def set_session_state(request: Request, provider: str = "chatgpt"):
    state = await request.json()
    ctx = contexts.get(provider)
    if not ctx:
        await init_provider(provider)
        ctx = contexts.get(provider)
    if not ctx:
        raise HTTPException(status_code=404, detail="Provider context not found")
    raw_cookies = state.get("cookies", [])
    cookies = [c for c in raw_cookies if not c.get("name", "").startswith("cf_") and not c.get("name", "").startswith("__cf")]
    if cookies:
        await ctx.add_cookies(cookies)
    page = pages.get(provider)
    if page:
        origins = state.get("origins", [])
        for origin in origins:
            origin_url = origin.get("origin", "")
            if "chatgpt.com" in origin_url or "openai.com" in origin_url:
                ls_data = origin.get("localStorage", [])
                for entry in ls_data:
                    k = entry.get("name")
                    v = entry.get("value")
                    if k and v:
                        try:
                            await page.evaluate("([k, v]) => window.localStorage.setItem(k, v)", [k, v])
                        except Exception:
                            pass
        auth_intervention_pending.discard(provider)
        try:
            await page.goto("https://chatgpt.com", wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print("Error reloading after session state import:", e)
    return {"status": "ok", "cookies_restored": len(cookies)}


@app.post("/v1/bridge/settings")
async def sync_bridge_settings(request: BridgeUiSettingsRequest):
    """Apply AVS's selected model and reasoning level to the provider web UI."""
    provider = request.provider.strip().lower()
    state = await _apply_provider_ui_settings(
        provider,
        request.model,
        request.reasoning_effort,
    )
    return {"provider": provider, **state}

# Thời điểm bridge khởi động: factory dùng để biết khi nào các thread phiên đã mất
BRIDGE_STARTED_AT = time.time()

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "started_at": BRIDGE_STARTED_AT,
        "gateway_mode": "signed-in-provider-web-ui",
        "browser_window_mode": "macos-hidden" if sys.platform == "darwin" else "headed",
        "supported_providers": list(PROVIDERS.keys()),
        "features": ["attachments"],
        "catalogs_cached": list(PROVIDER_MODEL_CATALOG_CACHE.keys()),
        "total_models": sum(
            len(item["state"].get("models", []))
            for item in PROVIDER_MODEL_CATALOG_CACHE.values()
        ),
    }

@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
    default_timeout = int(os.environ.get("BRIDGE_DEFAULT_TIMEOUT_SEC", "180"))
    max_timeout = int(os.environ.get("BRIDGE_MAX_TIMEOUT_SEC", "1800"))  # bridge của App Factory nâng lên 7200 trong start_factory_bridge.sh
    request_timeout_sec = max(30, min(int(req.timeout_sec or default_timeout), max_timeout))
    request_deadline = time.monotonic() + request_timeout_sec

    def remaining_timeout_sec() -> float:
        return max(0.0, request_deadline - time.monotonic())

    def require_remaining_timeout_sec() -> float:
        remaining = remaining_timeout_sec()
        if remaining <= 0:
            raise HTTPException(
                status_code=504,
                detail=(
                    "Bridge đã hết thời gian chờ tổng cho việc chuẩn bị phiên, "
                    "tải ảnh lên và nhận phản hồi từ model."
                ),
            )
        return remaining

    provider = resolve_provider(req.model, req.provider)
    config = PROVIDERS[provider]
    keep_code_fences_var.set(bool(req.keep_code_fences))
    prompt, image_paths, is_json_requested = compile_multimodal_request(req)
    prompt_to_send = prompt
    prompt_file_path = None
    adaptive_prompt_file = None
    seed_file_path = None
    attachment_paths = validated_attachment_paths(req.attachments)
    response_id = f"chatcmpl-{int(time.time()*1000)}"
    temporary_chat = False
    cleanup_state = "not_requested"
    lock = get_provider_lock(provider)
    lock_transferred_to_stream = False
    conversation_started = False
    session_cleaned = False
    page = None

    def cleanup_temp_files():
        nonlocal adaptive_prompt_file, seed_file_path
        for path in image_paths:
            if path.startswith("/tmp/footage_frame_") and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
        if prompt_file_path and os.path.exists(prompt_file_path):
            try:
                os.remove(prompt_file_path)
            except Exception:
                pass
        if adaptive_prompt_file and os.path.exists(adaptive_prompt_file):
            try:
                os.remove(adaptive_prompt_file)
            except Exception:
                pass
        if seed_file_path and os.path.exists(seed_file_path):
            try:
                os.remove(seed_file_path)
            except Exception:
                pass

    # --- AUTO-SESSION RESOLUTION FOR NATIVE OPENAI / DEEPSEEK CLIENTS ---
    if not req.role_thread_id and not req.session_id and req.auto_session is not False:
        # Check if caller wants stateless or auto_session
        is_explicit_stateless = (req.stateless is True and getattr(req, "auto_session", None) is False)
        if not is_explicit_stateless:
            resolved_sid, is_rotated = native_tracker.resolve_session_id(provider, req)
            req.session_id = resolved_sid
            req.persist_session = True
            req.stateless = False

    # --- AVS persistent provider-native role thread ---
    if req.role_thread_id:
        role_thread_id = req.role_thread_id.strip()
        if not ROLE_THREAD_ID_PATTERN.fullmatch(role_thread_id):
            raise HTTPException(status_code=422, detail="Bridge role thread id is invalid.")
        if req.stateless is not False or req.session_id:
            raise HTTPException(
                status_code=400,
                detail="A persistent AVS role thread requires stateless=false and no Factory session_id.",
            )

        session_key = f"{provider}:{role_thread_id}"
        s_lock = session_locks.setdefault(session_key, asyncio.Lock())
        stream_lock_transferred = False
        sem = get_factory_semaphore()
        semaphore_acquired = False
        role_lock_acquired = False
        try:
            await sem.acquire()
            semaphore_acquired = True
            await s_lock.acquire()
            role_lock_acquired = True
            s_page, is_existing_thread = await init_role_thread_page(
                provider, role_thread_id
            )
            await ensure_provider_page_ready(s_page, config)
            ui_state, _ = await prepare_provider_turn_before_dispatch(
                s_page,
                provider,
                config,
                req.model or "auto",
                req.reasoning_effort or "none",
                stateless=False,
            )
            if not is_existing_thread:
                if not await prepare_first_avs_role_chat(
                    s_page, provider, config
                ):
                    raise HTTPException(
                        status_code=503,
                        detail=(
                            f"Bridge không xác nhận được chat mới cho vai trò trên "
                            f"{config['name']}; đã dừng trước khi gửi."
                        ),
                    )
            prompt_to_send, prompt_file_path = prepare_chatgpt_role_markdown(
                prompt,
                provider=provider,
                role_thread_id=role_thread_id,
            )
            expected_user_marker = (
                os.path.basename(prompt_file_path)
                if prompt_file_path and provider == "chatgpt"
                else None
            )
            if prompt_file_path:
                print("BRIDGE_ROLE_PROMPT_MARKDOWN_FILE " + json.dumps({
                    "provider": provider,
                    "roleThreadId": role_thread_id,
                    "promptBytes": len(prompt.encode("utf-8")),
                    "fileName": os.path.basename(prompt_file_path),
                }, ensure_ascii=False))
            if image_paths:
                await upload_images_to_page(s_page, image_paths, config)
            if prompt_file_path:
                await upload_files_to_page(
                    s_page,
                    [prompt_file_path],
                    config,
                    attachment_kind="markdown",
                )
            if attachment_paths:
                await upload_files_to_page(s_page, attachment_paths, config)

            baseline_response_count = await _response_message_count(s_page, config)
            baseline_user_message_count = await _visible_user_message_count(s_page, config)
            if not is_existing_thread:
                # Persist an explicit marker before dispatch. If the
                # server exits before it can save the provider's chat
                # URL, recovery fails closed instead of creating a
                # replacement thread and losing this history.
                await _save_role_thread_record(provider, role_thread_id, None)
            await send_prompt_to_page(
                s_page,
                config,
                prompt_to_send,
                confirmation_timeout=min(15.0, require_remaining_timeout_sec()),
                wait_for_attachment_ready=bool(
                    (image_paths or attachment_paths or prompt_file_path)
                    and config.get("name") == "ChatGPT"
                ),
                allow_attachment_only=bool(prompt_file_path),
                expected_user_marker=expected_user_marker,
            )
            try:
                await _save_role_thread_record(
                    provider, role_thread_id, s_page.url
                )
            except HTTPException as error:
                print("BRIDGE_ROLE_THREAD_URL_SAVE_FAILED " + json.dumps({
                    "provider": provider,
                    "roleThreadId": role_thread_id,
                    "stage": "after_dispatch",
                    "status": error.status_code,
                }, ensure_ascii=False))
            cleanup_temp_files()

            reconciliation_attempt_count = 0

            async def wake_role_thread_once():
                nonlocal reconciliation_attempt_count
                if provider != "chatgpt":
                    return None
                cloudflare_visible = await _chatgpt_cloudflare_challenge_visible(s_page)
                retryable_error_visible = await _chatgpt_retryable_error_visible(s_page)
                if (
                    not cloudflare_visible
                    and not retryable_error_visible
                    and reconciliation_attempt_count
                    < BRIDGE_ROLE_LATE_REPLY_RELOAD_LIMIT
                ):
                    reconciliation_attempt_count += 1
                    observation_timeout = min(
                        BRIDGE_ROLE_LATE_REPLY_OBSERVATION_SEC,
                        max(0.0, require_remaining_timeout_sec() - 1.0),
                    )
                    reconcile = await _reload_and_reconcile_chatgpt_turn(
                        s_page, config, prompt_to_send,
                        expected_user_marker=expected_user_marker,
                        observation_timeout_sec=observation_timeout,
                        is_json_requested=is_json_requested,
                    )
                    if reconcile.get("state") == "page_not_ready_after_reload":
                        print("BRIDGE_ROLE_THREAD_PAGE_NOT_READY " + json.dumps({
                            "provider": provider,
                            "attempt": reconciliation_attempt_count,
                            "action": (
                                "reload_and_reobserve_same_thread"
                                if reconciliation_attempt_count
                                < BRIDGE_ROLE_LATE_REPLY_RELOAD_LIMIT
                                else "stop_without_replaying_prompt"
                            ),
                        }, ensure_ascii=False))
                        if (
                            reconciliation_attempt_count
                            < BRIDGE_ROLE_LATE_REPLY_RELOAD_LIMIT
                            and require_remaining_timeout_sec() > 1.0
                        ):
                            return {"action": "continue_observing_same_thread"}
                        raise HTTPException(
                            status_code=502,
                            detail={
                                "code": "BRIDGE_CHATGPT_ROLE_THREAD_PAGE_NOT_READY",
                                "outcome": "unconfirmed",
                                "retryable": True,
                                "message": (
                                    "Trang hội thoại ChatGPT chưa tải xong sau khi Bridge "
                                    "đã tải lại. Bridge chưa thấy nút Retry của đúng lượt "
                                    "nên không gửi thêm yêu cầu."
                                ),
                            },
                        )
                    if reconcile.get("state") == "completed_reply_recovered":
                        return {
                            "action": "reload_recovered_completed_reply",
                            "completed_text": reconcile.get("text", ""),
                            "baseline_response_count": await _response_message_count(
                                s_page, config
                            ),
                        }
                    if reconcile.get("state") == "generation_visible":
                        return {
                            "action": "reload_found_active_generation",
                            "generation_visible": True,
                            "baseline_response_count": await _response_message_count(
                                s_page, config
                            ),
                        }
                    cloudflare_visible = await _chatgpt_cloudflare_challenge_visible(s_page)
                    retryable_error_visible = await _chatgpt_retryable_error_visible(s_page)

                current_response_count = await _response_message_count(s_page, config)
                current_user_message_count = await _visible_user_message_count(
                    s_page, config
                )
                new_user_turn_visible = (
                    baseline_user_message_count is None
                    or current_user_message_count is None
                    or current_user_message_count > baseline_user_message_count
                )
                current_turn_regenerate_visible = await _current_turn_regenerate_visible(
                    s_page,
                    config,
                    expected_user_marker=expected_user_marker,
                )
                current_turn_regenerate = _role_turn_retry_is_safe(
                    new_user_turn_visible=new_user_turn_visible,
                    current_turn_regenerate_visible=current_turn_regenerate_visible,
                    expected_user_marker=expected_user_marker,
                )
                if (
                    (
                        current_turn_regenerate
                        or (
                            new_user_turn_visible
                            and (cloudflare_visible or retryable_error_visible)
                        )
                    )
                    and await _click_latest_regenerate(s_page, config)
                ):
                    print("BRIDGE_ROLE_TURN_RETRY " + json.dumps({
                        "provider": provider,
                        "reason": (
                            "current_turn_regenerate_control"
                            if current_turn_regenerate
                            else "provider_error_or_new_assistant_response"
                        ),
                        "responseCount": current_response_count,
                        "baselineResponseCount": baseline_response_count,
                        "newUserTurnVisible": new_user_turn_visible,
                        "expectedUserMarkerMatched": bool(
                            expected_user_marker and current_turn_regenerate_visible
                        ),
                    }, ensure_ascii=False))
                    return {
                        "action": "retry_existing_role_assistant_turn",
                        "baseline_response_count": baseline_response_count,
                    }
                if (
                    not cloudflare_visible
                    and not retryable_error_visible
                    and reconciliation_attempt_count
                    < BRIDGE_ROLE_LATE_REPLY_RELOAD_LIMIT
                    and require_remaining_timeout_sec() > 1.0
                ):
                    print("BRIDGE_ROLE_LATE_REPLY_OBSERVATION " + json.dumps({
                        "provider": provider,
                        "attempt": reconciliation_attempt_count,
                        "responseCount": current_response_count,
                        "baselineResponseCount": baseline_response_count,
                        "newUserTurnVisible": new_user_turn_visible,
                        "action": "continue_observing_same_thread",
                    }, ensure_ascii=False))
                    return {"action": "continue_observing_same_thread"}
                print("BRIDGE_ROLE_TURN_RETRY_UNAVAILABLE " + json.dumps({
                    "provider": provider,
                    "responseCount": current_response_count,
                    "baselineResponseCount": baseline_response_count,
                    "userMessageCount": current_user_message_count,
                    "baselineUserMessageCount": baseline_user_message_count,
                    "newUserTurnVisible": new_user_turn_visible,
                    "expectedUserMarkerMatched": bool(
                        expected_user_marker and current_turn_regenerate_visible
                    ),
                    "currentTurnRegenerateVisible": current_turn_regenerate_visible,
                }, ensure_ascii=False))
                # Persistent history forbids replaying the user prompt.
                # Only a provider-owned Retry/Regenerate control may
                # wake this same assistant turn.
                return None

            if req.stream:
                streaming_response = StreamingResponse(
                    role_thread_stream_generator(
                        s_page,
                        config,
                        response_id,
                        ui_state["selected_model"],
                        provider=provider,
                        role_thread_id=role_thread_id,
                        timeout=max(1, int(require_remaining_timeout_sec())),
                        submitted_prompt=prompt_to_send,
                        baseline_response_count=baseline_response_count,
                        baseline_user_message_count=baseline_user_message_count,
                        expected_user_marker=expected_user_marker,
                        wake_retry=(
                            wake_role_thread_once if provider == "chatgpt" else None
                        ),
                        is_json_requested=is_json_requested,
                        prompt_token_count=len(prompt.split()) + (len(image_paths) * 256),
                        role_lock=s_lock,
                        semaphore=sem,
                    ),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
                stream_lock_transferred = True
                return streaming_response

            raw_output = await wait_for_completion(
                s_page,
                config,
                timeout=max(1, int(require_remaining_timeout_sec())),
                submitted_prompt=prompt_to_send,
                baseline_response_count=baseline_response_count,
                baseline_user_message_count=baseline_user_message_count,
                expected_user_marker=expected_user_marker,
                wake_retry=(
                    wake_role_thread_once if provider == "chatgpt" else None
                ),
                is_json_requested=is_json_requested,
            )
            if not raw_output:
                raise HTTPException(
                    status_code=504,
                    detail=(
                        "Bridge không nhận được phản hồi từ hội thoại vai trò "
                        "trong thời hạn của yêu cầu. Lịch sử được giữ nguyên; "
                        "không gửi lại cùng tin nhắn tự động."
                    ),
                )
            try:
                await _save_role_thread_record(
                    provider, role_thread_id, s_page.url
                )
            except HTTPException as error:
                print("BRIDGE_ROLE_THREAD_URL_SAVE_FAILED " + json.dumps({
                    "provider": provider,
                    "roleThreadId": role_thread_id,
                    "stage": "after_completion",
                    "status": error.status_code,
                }, ensure_ascii=False))

            clean_content, reasoning_content = extract_and_clean_output(
                raw_output, is_json_requested
            )
            message = {"role": "assistant", "content": clean_content}
            if reasoning_content:
                message["reasoning_content"] = reasoning_content
            return {
                "id": response_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": ui_state["selected_model"],
                "provider": provider,
                "role_thread_id": role_thread_id,
                "choices": [{
                    "index": 0,
                    "message": message,
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": len(prompt.split()) + (len(image_paths) * 256),
                    "completion_tokens": len(clean_content.split()),
                    "total_tokens": len(prompt.split())
                    + len(clean_content.split())
                    + (len(image_paths) * 256),
                },
            }
        finally:
            cleanup_temp_files()
            if not stream_lock_transferred:
                if role_lock_acquired and s_lock.locked():
                    s_lock.release()
                if semaphore_acquired:
                    sem.release()

    # --- MULTI-TAB SESSION POOL ROUTE (FOR AI SOFTWARE FACTORY) ---
    if req.session_id:
        session_key = f"{provider}:{req.session_id}"
        s_lock = session_locks.setdefault(session_key, asyncio.Lock())
        persist = bool(req.persist_session) and not req.stateless
        if persist:
            _ensure_factory_janitor()
        sem = get_factory_semaphore()
        session_stream_transferred = False
        sem_acquired = False
        s_lock_acquired = False
        try:
            await sem.acquire()
            sem_acquired = True
            await s_lock.acquire()
            s_lock_acquired = True
            session_downloads[session_key] = []
            session_download_failures[session_key] = []
            resume_url = None
            resumed = False
            if persist:
                live_tab = session_pages.get(session_key)
                if live_tab is not None and not live_tab.is_closed():
                    resumed = True
                else:
                    saved_url = _factory_store(provider).get(req.session_id)
                    if saved_url and not _factory_url_ok(provider, saved_url):
                        saved_url = None
                    if req.resume_expected:
                        if not saved_url:
                            raise HTTPException(
                                status_code=409,
                                detail="conversation_lost: bridge không còn địa chỉ cuộc trò chuyện của phiên này; chưa gửi gì.",
                            )
                        resume_url = saved_url
                    elif saved_url:
                        _factory_store(provider).drop(req.session_id)  # phiên mới trùng tên: bỏ địa chỉ cũ, lượt đầu sẽ ghi địa chỉ mới
            s_page = await init_session_page(provider, req.session_id, req.download_dir, resume_url=resume_url)
            await ensure_provider_page_ready(s_page, config)
            if resume_url:
                verdict = await _verify_resumed_conversation(s_page, config, resume_url)
                if verdict == "lost":
                    await _discard_factory_page(provider, req.session_id)
                    raise HTTPException(
                        status_code=409,
                        detail="conversation_lost: ChatGPT không còn cuộc trò chuyện đã lưu của phiên này (đã bị xóa); chưa gửi gì.",
                    )
                if verdict != "ok":
                    await _discard_factory_page(provider, req.session_id, forget=False)  # giữ địa chỉ: lần sau mở lại bình thường
                    raise HTTPException(
                        status_code=503,
                        detail="Chưa mở lại được cuộc trò chuyện đã lưu (trang tải chậm hoặc lỗi tạm thời); chưa gửi gì, địa chỉ vẫn được giữ. Thử lại sau ít phút.",
                    )
                resumed = True
                await _wait_generation_idle(s_page, config, min(600.0, require_remaining_timeout_sec()))
            if req.stateless:
                await perform_fast_new_chat(s_page, config)

            # Apply provider UI settings (model and reasoning effort)
            ui_state, _ = await prepare_provider_turn_before_dispatch(
                s_page,
                provider,
                config,
                req.model or "auto",
                req.reasoning_effort or "none",
                stateless=False,
            )
            selected_model_name = ui_state.get("selected_model", req.model or provider)

            # Smart context & history packaging via native_api_adapter
            known_sys_hash = native_tracker.get_system_hash(req.session_id)
            prompt_to_send, seed_file_path, req_is_json, current_sys_hash = compile_native_turn(
                req,
                req.session_id,
                is_resumed_session=resumed,
                known_system_hash=known_sys_hash,
            )
            is_json_requested = is_json_requested or req_is_json
            expected_user_marker = os.path.basename(seed_file_path) if seed_file_path else None

            adaptive_prompt_file = None
            if not seed_file_path:
                adaptive_directive, adaptive_file = package_oversized_prompt_if_needed(
                    prompt_to_send,
                    provider,
                    prefix=f"factory-{req.session_id}",
                )
                if adaptive_file:
                    adaptive_prompt_file = adaptive_file
                    prompt_to_send = adaptive_directive
                    expected_user_marker = os.path.basename(adaptive_file)

            if image_paths:
                await upload_images_to_page(s_page, image_paths, config)
            if seed_file_path:
                await upload_files_to_page(s_page, [seed_file_path], config, attachment_kind="markdown")
            if adaptive_prompt_file:
                await upload_files_to_page(s_page, [adaptive_prompt_file], config, attachment_kind="markdown")
            if attachment_paths:
                await upload_files_to_page(s_page, attachment_paths, config)

            # Baseline counts measured BEFORE dispatch
            baseline_response_count = await _response_message_count(s_page, config)
            baseline_user_message_count = await _visible_user_message_count(s_page, config)

            await send_prompt_to_page(
                s_page,
                config,
                prompt_to_send,
                confirmation_timeout=min(15.0, require_remaining_timeout_sec()),
                wait_for_attachment_ready=bool(
                    (image_paths or seed_file_path or adaptive_prompt_file or attachment_paths) and config.get("name") == "ChatGPT"
                ),
                expected_user_marker=expected_user_marker,
            )
            if persist:
                await _save_factory_url_when_ready(provider, req.session_id, s_page, min(20.0, require_remaining_timeout_sec()))
            cleanup_temp_files()

            # Handle SSE Streaming if requested
            if req.stream:
                streaming_resp = StreamingResponse(
                    session_stream_generator(
                        s_page,
                        config,
                        response_id,
                        selected_model_name,
                        provider=provider,
                        session_id=req.session_id,
                        timeout=max(1, int(require_remaining_timeout_sec())),
                        submitted_prompt=prompt_to_send,
                        baseline_response_count=baseline_response_count,
                        baseline_user_message_count=baseline_user_message_count,
                        expected_user_marker=expected_user_marker,
                        is_json_requested=is_json_requested,
                        prompt_token_count=len(prompt_to_send.split()),
                        session_lock=s_lock,
                        semaphore=sem,
                        persist=persist,
                        tools=req.tools,
                        stream_options=req.stream_options,
                        system_hash=current_sys_hash,
                        session_key=session_key,
                    ),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
                session_stream_transferred = True
                return streaming_resp

            raw_output = await wait_for_completion(
                s_page,
                config,
                timeout=max(1, int(require_remaining_timeout_sec())),
                submitted_prompt=prompt_to_send,
                baseline_response_count=baseline_response_count,
                baseline_user_message_count=baseline_user_message_count,
                expected_user_marker=expected_user_marker,
                is_json_requested=is_json_requested,
            )
            if not raw_output:
                raise HTTPException(
                    status_code=504,
                    detail=(
                        "Bridge không nhận được phản hồi từ giao diện web "
                        "trong thời hạn của yêu cầu."
                    ),
                )
            downloaded_files = []
            if req.expect_zip or ".zip" in raw_output.lower():
                downloaded_files = await trigger_chat_downloads_if_any(s_page, session_key, req.download_dir)
            conversation_url = _save_factory_url(provider, req.session_id, s_page, "after_completion") if persist else None
            clean_content, reasoning_content = extract_and_clean_output(raw_output, is_json_requested)

            # Parse tool calls from model response
            clean_content_final, parsed_tool_calls = parse_tool_calls_from_response(clean_content, req.tools)
            finish_reason = "tool_calls" if parsed_tool_calls else "stop"

            msg_dict: Dict[str, Any] = {"role": "assistant", "content": clean_content_final}
            if parsed_tool_calls:
                msg_dict["tool_calls"] = parsed_tool_calls
            if reasoning_content:
                msg_dict["reasoning_content"] = reasoning_content

            # Record turn in NativeSessionTracker
            native_tracker.record_turn(
                req.session_id,
                prompt_chars=len(prompt_to_send),
                response_chars=len(clean_content),
                system_hash=current_sys_hash,
            )

            return {
                "id": response_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": selected_model_name,
                "provider": provider,
                "session_id": req.session_id,
                "conversation_url": conversation_url,
                "resumed": resumed,
                "downloaded_files": downloaded_files,
                "download_failures": list(session_download_failures.get(session_key, [])),
                "choices": [{
                    "index": 0,
                    "message": msg_dict,
                    "finish_reason": finish_reason,
                }],
                "usage": {
                    "prompt_tokens": len(prompt_to_send.split()),
                    "completion_tokens": len(clean_content.split()),
                    "total_tokens": len(prompt_to_send.split()) + len(clean_content.split()),
                }
            }
        finally:
            cleanup_temp_files()
            session_last_used[session_key] = time.time()
            if not session_stream_transferred:
                if s_lock_acquired and s_lock.locked():
                    s_lock.release()
                if sem_acquired:
                    sem.release()

    lock_acquired = False
    try:
        try:
            await asyncio.wait_for(
                lock.acquire(), timeout=require_remaining_timeout_sec()
            )
            lock_acquired = True
        except asyncio.TimeoutError as error:
            raise HTTPException(
                status_code=504,
                detail="Bridge hết thời gian chờ vì provider đang bận lượt khác.",
            ) from error

        await init_provider(provider)
        page = pages[provider]
        await ensure_provider_page_ready(page, config)
        ui_state, temporary_chat = await prepare_provider_turn_before_dispatch(
            page,
            provider,
            config,
            req.model or "auto",
            req.reasoning_effort or "none",
            stateless=bool(req.stateless),
        )
        model_name = ui_state["selected_model"]

        prompt_to_send = prompt
        adaptive_prompt_file = None
        adaptive_directive, adaptive_file = package_oversized_prompt_if_needed(
            prompt,
            provider,
            prefix="bridge-context",
        )
        if adaptive_file:
            adaptive_prompt_file = adaptive_file
            prompt_to_send = adaptive_directive

        if image_paths:
            await upload_images_to_page(page, image_paths, config)
        if adaptive_prompt_file:
            await upload_files_to_page(page, [adaptive_prompt_file], config, attachment_kind="markdown")
        if attachment_paths:
            await upload_files_to_page(page, attachment_paths, config)

        baseline_response_count = await _response_message_count(page, config)
        baseline_user_message_count = await _visible_user_message_count(page, config)
        await send_prompt_to_page(
            page,
            config,
            prompt_to_send,
            confirmation_timeout=min(15.0, require_remaining_timeout_sec()),
            wait_for_attachment_ready=bool(
                (image_paths or attachment_paths or adaptive_prompt_file) and config.get("name") == "ChatGPT"
            ),
            allow_attachment_only=False,
        )
        conversation_started = True

        if req.stream:
            lock_transferred_to_stream = True
            return StreamingResponse(
                stream_generator(
                    page,
                    config,
                    response_id,
                    model_name,
                    provider=provider,
                    temporary_chat=temporary_chat,
                    stateless=bool(req.stateless),
                    lock=lock,
                    timeout=max(1, int(require_remaining_timeout_sec())),
                    submitted_prompt=prompt_to_send,
                ),
                media_type="text/event-stream",
            )

        wake_retry_count = 0
        did_reload_reconcile = False

        async def wake_same_stage_once():
            nonlocal temporary_chat, wake_retry_count, did_reload_reconcile
            if provider != "chatgpt":
                return None

            cloudflare_visible = await _chatgpt_cloudflare_challenge_visible(page)
            retryable_error_visible = await _chatgpt_retryable_error_visible(page)
            if not cloudflare_visible and not retryable_error_visible and not did_reload_reconcile:
                did_reload_reconcile = True
                reconcile = await _reload_and_reconcile_chatgpt_turn(
                    page, config, prompt,
                    is_json_requested=is_json_requested,
                )
                if reconcile.get("state") == "completed_reply_recovered":
                    return {
                        "action": "reload_recovered_completed_reply",
                        "completed_text": reconcile.get("text", ""),
                        "baseline_response_count": await _response_message_count(page, config),
                    }
                if reconcile.get("state") == "generation_visible":
                    return {
                        "action": "reload_found_active_generation",
                        "generation_visible": True,
                        "baseline_response_count": await _response_message_count(page, config),
                    }
                cloudflare_visible = await _chatgpt_cloudflare_challenge_visible(page)
                retryable_error_visible = await _chatgpt_retryable_error_visible(page)

            current_response_count = await _response_message_count(page, config)
            if (
                cloudflare_visible
                and await _click_latest_regenerate(page, config)
            ):
                wake_retry_count += 1
                return {
                    "action": "retry_visible_cloudflare_challenge",
                    "baseline_response_count": current_response_count,
                }
            if (
                current_response_count > baseline_response_count
                and await _click_latest_regenerate(page, config)
            ):
                wake_retry_count += 1
                return {
                    "action": "regenerate_same_assistant_turn",
                    "baseline_response_count": baseline_response_count,
                }

            # A fresh temporary chat avoids appending a duplicate user prompt to
            # the conversation if ChatGPT never created an assistant turn.
            if not req.stateless:
                return None
            retry_info = await _resend_in_fresh_temporary_chat(
                page,
                config,
                prompt,
                image_paths,
                confirmation_timeout=min(15.0, require_remaining_timeout_sec()),
                attachment_paths=attachment_paths,
            )
            if not retry_info:
                return None
            temporary_chat = bool(retry_info.pop("temporary_chat", False))
            wake_retry_count += 1
            return retry_info

        raw_output = await wait_for_completion(
            page,
            config,
            timeout=max(1, int(require_remaining_timeout_sec())),
            submitted_prompt=prompt_to_send,
            baseline_response_count=baseline_response_count,
            baseline_user_message_count=baseline_user_message_count,
            wake_retry=wake_same_stage_once if provider == "chatgpt" else None,
            is_json_requested=is_json_requested,
        )
        if not raw_output:
            raise HTTPException(
                status_code=504,
                detail=(
                    "Bridge không nhận được phản hồi từ giao diện web "
                    "trong thời hạn của yêu cầu."
                ),
            )

        clean_content, reasoning_content = extract_and_clean_output(raw_output, is_json_requested)
        if req.stateless:
            cleanup_state = await cleanup_provider_session(
                page,
                provider,
                temporary_chat,
                config,
                retain_ephemeral_context=True,
            )
            session_cleaned = True
        message_dict = {"role": "assistant", "content": clean_content}
        if reasoning_content:
            message_dict["reasoning_content"] = reasoning_content

        return {
            "id": response_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_name,
            "provider": provider,
            "session_cleanup": cleanup_state,
            "choices": [{
                "index": 0,
                "message": message_dict,
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": len(prompt.split()) + (len(image_paths) * 256),
                "completion_tokens": len(clean_content.split()),
                "total_tokens": len(prompt.split()) + len(clean_content.split()) + (len(image_paths) * 256),
                "bridge_wake_retry_count": wake_retry_count,
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"[{provider.upper()} Vision Gateway Error]: {str(e)}")
    finally:
        cleanup_temp_files()
        if (
            req.stateless
            and not conversation_started
            and not lock_transferred_to_stream
            and page is not None
            and provider not in auth_intervention_pending
        ):
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:
                pass
        if (
            req.stateless
            and conversation_started
            and not session_cleaned
            and not lock_transferred_to_stream
            and page is not None
        ):
            await cleanup_provider_session(page, provider, temporary_chat, config)
        if not lock_transferred_to_stream and lock_acquired and lock.locked():
            lock.release()

if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("BRIDGE_HOST", "0.0.0.0"), port=BRIDGE_PORT)
