"""Phiên bền cho App Factory: bridge nhớ địa chỉ cuộc trò chuyện web của từng session_id.

Bridge giữ mỗi session_id một tab trong bộ nhớ; tab mất khi bridge khởi động lại hoặc bị đóng. Với phiên bền, bridge ghi lại địa chỉ cuộc trò chuyện
(ví dụ https://chatgpt.com/c/<uuid>) ngay khi có, để lần gọi sau mở lại đúng cuộc trò chuyện đó thay vì mở chat mới. Lịch sử do chính trang web giữ.
Module chỉ dùng thư viện chuẩn và không đụng vào phần AVS (role_thread_id có kho riêng trong server.py).
"""
import json
import os
import re
import threading
import time
import uuid
from typing import Dict, List, Optional
from urllib.parse import urlsplit

_CONVERSATION_RE = re.compile(r"/c/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/?$")
STORE_VERSION = 1


def conversation_id(url: Optional[str]) -> Optional[str]:
    """Mã cuộc trò chuyện trong địa chỉ (…/c/<uuid>, kể cả khi nằm trong một Project), hoặc None nếu địa chỉ chưa trỏ tới cuộc trò chuyện nào."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        path = urlsplit(url.strip()).path
    except ValueError:
        return None
    match = _CONVERSATION_RE.search(path)
    return match.group(1).lower() if match else None


def is_conversation_url(url: Optional[str], host: str) -> bool:
    """Địa chỉ https của đúng trang provider (không kèm tài khoản hay cổng lạ) và trỏ tới một cuộc trò chuyện cụ thể."""
    if conversation_id(url) is None:
        return False
    try:
        parts = urlsplit(str(url).strip())
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and (parts.hostname or "").lower() == (host or "").lower()
        and parts.port in (None, 443)
        and parts.username is None
        and parts.password is None
    )


class FactorySessionStore:
    """Bản đồ session_id -> địa chỉ cuộc trò chuyện, lưu trong một file JSON (ghi qua file tạm nên không hỏng file cũ nếu máy dừng giữa chừng)."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> Dict[str, Dict[str, object]]:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                saved = json.load(handle)
            sessions = saved.get("sessions") if isinstance(saved, dict) and saved.get("version") == STORE_VERSION else None
            if not isinstance(sessions, dict):
                raise ValueError("unsupported factory-session store")
            return {k: v for k, v in sessions.items() if isinstance(k, str) and isinstance(v, dict)}
        except (OSError, ValueError):
            # File hỏng: giữ lại để xem, bắt đầu bản đồ mới (phiên nào mất địa chỉ thì factory tự nạp lại gói khởi đầu)
            try:
                os.replace(self.path, f"{self.path}.corrupt-{int(time.time())}")
            except OSError:
                pass
            return {}

    def _write(self, sessions: Dict[str, Dict[str, object]]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        payload = json.dumps({"version": STORE_VERSION, "sessions": sessions}, ensure_ascii=False, separators=(",", ":"))
        tmp = f"{self.path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def get(self, session_id: str) -> Optional[str]:
        with self._lock:
            data = self._read()
            item = data.get(session_id)
            if not item:
                alt = f"ubuntu-{session_id}" if not session_id.startswith("ubuntu-") else session_id[7:]
                item = data.get(alt)
            url = item.get("url") if isinstance(item, dict) else None
        return url if isinstance(url, str) and url else None

    def put(self, session_id: str, url: str) -> None:
        with self._lock:
            sessions = self._read()
            sessions[session_id] = {"url": url, "updated": int(time.time())}
            self._write(sessions)

    def drop(self, session_id: str) -> bool:
        with self._lock:
            sessions = self._read()
            if session_id not in sessions:
                return False
            del sessions[session_id]
            self._write(sessions)
            return True

    def ids(self) -> List[str]:
        with self._lock:
            return list(self._read().keys())


_stores: Dict[str, FactorySessionStore] = {}
_stores_lock = threading.Lock()


def store_for(path: str) -> FactorySessionStore:
    with _stores_lock:
        if path not in _stores:
            _stores[path] = FactorySessionStore(path)
        return _stores[path]


# Xóa cuộc trò chuyện do chính factory tạo (phiên worker đã xong). ChatGPT web tự xóa bằng PATCH is_visible=false với token của phiên đăng nhập;
# chạy trong trang đã đăng nhập nên không cần lấy token ra ngoài. Chỉ nhận mã cuộc trò chuyện đúng dạng uuid.
DELETE_CONVERSATION_JS = """async (cid) => {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(cid)) return {ok: false, why: 'bad_id'};
  try {
    const session = await fetch('/api/auth/session', {credentials: 'include'}).then(r => r.json());
    const token = session && session.accessToken;
    if (!token) return {ok: false, why: 'no_token'};
    const headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'};
    const did = (document.cookie.match(/(?:^|;\\s*)oai-did=([^;]+)/) || [])[1];
    if (did) headers['oai-device-id'] = did;
    const r = await fetch('/backend-api/conversation/' + cid, {method: 'PATCH', headers, credentials: 'include', body: JSON.stringify({is_visible: false})});
    return {ok: r.ok, status: r.status};
  } catch (e) {
    return {ok: false, why: String(e).slice(0, 120)};
  }
}"""
