"""In-memory capture of ChatGPT's browser-owned final response body."""

import asyncio
import base64
import hashlib
import json
import re
import time
from urllib.parse import parse_qs, unquote, urlsplit
from typing import Any, Optional


_CAPTURE_ATTRIBUTE = "_avs_chatgpt_network_response_capture"
_FINAL_MESSAGE_TYPES = {"final", "answer", "final_answer"}
_COMPLETED_VISIBLE_CHANNELS = {"next"}
_COMPLETED_STATUSES = {"finished_successfully", "complete", "completed", "done"}
_INCOMPLETE_STATUSES = {"in_progress", "running", "incomplete", "interrupted"}
_MAX_CAPTURE_BYTES = 32 * 1024 * 1024
_CDP_SETUP_TIMEOUT_SEC = 3.0


def is_chatgpt_conversation_response(url: str, method: str = "POST") -> bool:
    """Match ChatGPT conversation response routes; ignore all other traffic."""
    if (method or "").upper() != "POST":
        return False
    try:
        parsed = urlsplit(url)
    except Exception:
        return False
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    return (
        parsed.scheme == "https"
        and parsed.hostname == "chatgpt.com"
        and len(parts) >= 2
        and parts[0] == "backend-api"
        and parts[-1] == "conversation"
    )


def is_chatgpt_conversation_detail_response(url: str, method: str = "GET") -> bool:
    """Match a single retained conversation detail read, never collection APIs."""
    if (method or "").upper() != "GET":
        return False
    try:
        parsed = urlsplit(url)
    except Exception:
        return False
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    return (
        parsed.scheme == "https"
        and parsed.hostname == "chatgpt.com"
        and len(parts) == 3
        and parts[0] == "backend-api"
        and parts[1] == "conversation"
        and bool(unquote(parts[2]))
    )


def _chatgpt_conversation_id_from_url(url: str) -> Optional[str]:
    if not is_chatgpt_conversation_detail_response(url, "GET"):
        return None
    try:
        parts = [part for part in urlsplit(url).path.strip("/").split("/") if part]
        return unquote(parts[-1])
    except Exception:
        return None


def _chatgpt_conversation_id_from_page_url(url: str) -> Optional[str]:
    """Read only the conversation id from this tab's first-party URL."""
    try:
        parsed = urlsplit(url)
    except Exception:
        return None
    if parsed.scheme != "https" or parsed.hostname != "chatgpt.com":
        return None
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    for index, part in enumerate(parts[:-1]):
        if part == "c" and parts[index + 1]:
            return unquote(parts[index + 1])
    return None


def _normalized_text_fingerprint(value: str) -> Optional[str]:
    normalized = re.sub(
        r"\s+(?=https?://)", "",
        re.sub(r"\s+", " ", value or "").strip(),
        flags=re.I,
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else None


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _request_turn_identifiers(request: dict, expected_user_fingerprint=None):
    """Extract only current-turn identity fields from a browser-owned request."""
    conversation_id = None
    user_message_id = None
    url = str(request.get("url") or "")
    try:
        query = parse_qs(urlsplit(url).query)
        for key in ("conversation_id", "conversationId"):
            values = query.get(key) or []
            if values and isinstance(values[-1], str):
                conversation_id = values[-1]
                break
    except Exception:
        pass
    raw = request.get("postData")
    if isinstance(raw, str) and len(raw.encode("utf-8", errors="ignore")) <= _MAX_CAPTURE_BYTES:
        try:
            root = json.loads(raw)
        except Exception:
            root = None
        if root is not None:
            for item in _walk_dicts(root):
                for key in ("conversation_id", "conversationId"):
                    candidate = item.get(key)
                    if isinstance(candidate, str) and candidate:
                        conversation_id = candidate
                        break
            user_messages = [
                item for item in _walk_dicts(root)
                if isinstance(item.get("author"), dict)
                and item["author"].get("role") == "user"
                and isinstance(item.get("id"), str)
                and item.get("id")
            ]
            if expected_user_fingerprint:
                matching = [
                    item for item in user_messages
                    if _normalized_text_fingerprint(_message_text(item) or "")
                    == expected_user_fingerprint
                ]
            else:
                matching = []
            candidates = matching or user_messages
            if candidates:
                user_message_id = candidates[-1].get("id")
    return conversation_id, user_message_id


def _safe_backend_route(url: str) -> Optional[str]:
    """Return a query-free route template suitable for local diagnostics."""
    try:
        parsed = urlsplit(url)
    except Exception:
        return None
    if parsed.scheme not in {"https", "wss"} or parsed.hostname != "chatgpt.com":
        return None
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if not parts or parts[0] != "backend-api":
        return None
    safe_parts = [parts[0]]
    for part in parts[1:]:
        # Preserve likely route words; hide ids or opaque path values. Query
        # strings are never read or logged.
        if part == "conversation" or part in {"f", "responses", "completions", "chat", "stream", "v2"}:
            safe_parts.append(part)
        elif part.isalpha() and len(part) <= 20:
            safe_parts.append(part)
        else:
            safe_parts.append(":dynamic")
    return "/".join(safe_parts)


def _event_payloads(body: bytes):
    text = body.decode("utf-8-sig", errors="replace")
    if "data:" not in text:
        try:
            yield json.loads(text)
        except Exception:
            return
        return

    data_lines = []

    def emit():
        if not data_lines:
            return None
        data = "\n".join(data_lines).strip()
        data_lines.clear()
        if not data or data == "[DONE]":
            return None
        try:
            return json.loads(data)
        except Exception:
            return None

    for line in text.splitlines():
        if not line.strip():
            payload = emit()
            if payload is not None:
                yield payload
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    payload = emit()
    if payload is not None:
        yield payload


def _message_text(message: dict) -> Optional[str]:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, dict):
        return None
    parts = content.get("parts")
    if not isinstance(parts, list):
        parts = [content.get("text")] if isinstance(content.get("text"), str) else []
    values = []
    for part in parts:
        if isinstance(part, str):
            values.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            values.append(part["text"])
    result = "".join(values).strip()
    return result or None


def _is_final_visible_message(message: Any) -> bool:
    if not isinstance(message, dict):
        return False
    author = message.get("author")
    if not isinstance(author, dict) or author.get("role") != "assistant":
        return False
    metadata = message.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    kind = (
        metadata.get("message_type")
        or metadata.get("channel")
        or message.get("channel")
    )
    if isinstance(kind, dict):
        kind = kind.get("name") or kind.get("type")
    channel = str(kind or "").strip().lower()
    if channel not in _FINAL_MESSAGE_TYPES and not (
        channel in _COMPLETED_VISIBLE_CHANNELS and message.get("end_turn") is True
    ):
        # A completed assistant message without an explicit final channel may
        # be hidden analysis. A `next` channel is visible only when the turn
        # explicitly ended and the message is not addressed to a tool.
        return False
    recipient = message.get("recipient")
    if isinstance(recipient, dict):
        recipient = recipient.get("name") or recipient.get("type")
    if recipient not in (None, "", "all"):
        return False
    status = str(message.get("status") or "").strip().lower()
    if status in _INCOMPLETE_STATUSES:
        return False
    return not status or status in _COMPLETED_STATUSES


def _apply_chatgpt_stream_patch(root, payload):
    """Apply one in-memory ChatGPT response patch; never persist its values."""
    path = payload.get("p")
    operation = str(payload.get("o") or "").strip().lower()
    value = payload.get("v")
    if not isinstance(path, str):
        return root
    if not path:
        if operation in {"add", "replace", "set"}:
            return value
        if operation == "remove":
            return None
        if operation == "append":
            if isinstance(root, str) and isinstance(value, str):
                return root + value
            if isinstance(root, list):
                root.append(value)
            return root
        return root
    if not path.startswith("/") or root is None:
        return root
    parts = [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]
    parent = root
    try:
        for part in parts[:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        key = parts[-1]
        if isinstance(parent, list):
            index = len(parent) if key == "-" else int(key)
            if operation == "remove":
                parent.pop(index)
            elif operation == "add":
                parent.insert(index, value)
            elif operation == "append":
                if index < len(parent):
                    if isinstance(parent[index], str) and isinstance(value, str):
                        parent[index] += value
                    elif isinstance(parent[index], list):
                        parent[index].append(value)
                    else:
                        parent.insert(index, value)
                else:
                    parent.append(value)
            else:
                parent[index] = value
        elif isinstance(parent, dict):
            if operation == "remove":
                parent.pop(key, None)
            elif operation == "append":
                existing = parent.get(key)
                if isinstance(existing, str) and isinstance(value, str):
                    parent[key] = existing + value
                elif isinstance(existing, list):
                    existing.append(value)
                elif existing is None:
                    parent[key] = value
            else:
                parent[key] = value
    except (KeyError, IndexError, TypeError, ValueError):
        return root
    return root


def extract_chatgpt_final_text(body: bytes) -> Optional[str]:
    """Return only an explicitly final, user-visible assistant message."""
    latest = None
    response_root = None
    for payload in _event_payloads(body):
        if not isinstance(payload, dict):
            continue
        if "p" in payload and "v" in payload:
            response_root = _apply_chatgpt_stream_patch(response_root, payload)
            candidate = (
                response_root.get("message")
                if isinstance(response_root, dict) else None
            )
            if _is_final_visible_message(candidate):
                text = _message_text(candidate)
                if text:
                    latest = text
        message = payload.get("message")
        if _is_final_visible_message(message):
            text = _message_text(message)
            if text:
                latest = text
        messages = payload.get("messages")
        if isinstance(messages, list):
            for candidate in messages:
                if _is_final_visible_message(candidate):
                    text = _message_text(candidate)
                    if text:
                        latest = text
    return latest


def _conversation_detail_messages(root: Any):
    if not isinstance(root, dict):
        return [], {}, None
    direct = root.get("messages")
    if isinstance(direct, list):
        return [item for item in direct if isinstance(item, dict)], {}, None
    mapping = root.get("mapping")
    if not isinstance(mapping, dict):
        return [], {}, None
    node_messages = {}
    for node_id, node in mapping.items():
        if isinstance(node, dict) and isinstance(node.get("message"), dict):
            node_messages[str(node_id)] = node["message"]
    return list(node_messages.values()), mapping, root.get("current_node")


def extract_chatgpt_conversation_detail_text(
    body: bytes,
    current_user_message_id: Optional[str],
    expected_user_fingerprint: Optional[str] = None,
) -> Optional[str]:
    """Read the final visible assistant message after this turn's user node."""
    try:
        root = json.loads(body.decode("utf-8-sig", errors="replace"))
    except Exception:
        return None
    messages, mapping, current_node = _conversation_detail_messages(root)
    if not messages:
        return None

    def is_current_user(message):
        if not isinstance(message, dict):
            return False
        if not isinstance(message.get("author"), dict) or message["author"].get("role") != "user":
            return False
        if current_user_message_id and message.get("id") == current_user_message_id:
            return True
        return bool(
            expected_user_fingerprint
            and _normalized_text_fingerprint(_message_text(message) or "")
            == expected_user_fingerprint
        )

    if not mapping:
        user_index = next(
            (
                index for index, message in enumerate(messages)
                if is_current_user(message)
            ),
            None,
        )
        if user_index is None:
            return None
        candidates = [
            message for message in messages[user_index + 1:]
            if _is_final_visible_message(message) and _message_text(message)
        ]
        return _message_text(candidates[-1]) if candidates else None

    user_node = next(
        (
            node_id for node_id, message in (
                (str(node_id), node["message"])
                for node_id, node in mapping.items()
                if isinstance(node, dict) and isinstance(node.get("message"), dict)
            )
            if is_current_user(message)
        ),
        None,
    )
    if user_node is None:
        return None

    allowed_nodes = set()
    stack = [user_node]
    while stack and len(allowed_nodes) < 10000:
        node_id = stack.pop()
        if node_id in allowed_nodes:
            continue
        allowed_nodes.add(node_id)
        node = mapping.get(node_id) or {}
        children = node.get("children") if isinstance(node, dict) else None
        if isinstance(children, list):
            stack.extend(str(child) for child in children if child is not None)

    if isinstance(current_node, str) and current_node in allowed_nodes:
        node = mapping.get(current_node) or {}
        message = node.get("message") if isinstance(node, dict) else None
        if _is_final_visible_message(message) and _message_text(message):
            return _message_text(message)

    candidates = [
        mapping[node_id].get("message")
        for node_id in allowed_nodes
        if isinstance(mapping.get(node_id), dict)
        and isinstance(mapping[node_id].get("message"), dict)
        and _is_final_visible_message(mapping[node_id].get("message"))
        and _message_text(mapping[node_id].get("message"))
    ]
    if not candidates:
        return None
    def timestamp(message):
        values = []
        for key in ("update_time", "create_time"):
            try:
                values.append(float(message.get(key) or 0))
            except (TypeError, ValueError, OverflowError):
                values.append(0.0)
        return max(values)

    candidates.sort(key=timestamp)
    return _message_text(candidates[-1])


def _conversation_detail_shape_summary(
    body: bytes,
    current_user_message_id: Optional[str],
    expected_user_fingerprint: Optional[str],
) -> dict:
    """Summarize a detail response without exposing text, ids, or values."""
    try:
        root = json.loads(body.decode("utf-8-sig", errors="replace"))
    except Exception:
        return {"bytes": len(body), "json": False}
    messages, mapping, _current_node = _conversation_detail_messages(root)
    summaries = []
    current_user_found = False
    final_count = 0
    for message in messages:
        author = message.get("author")
        role = author.get("role") if isinstance(author, dict) else None
        is_current = bool(
            role == "user"
            and (
                (current_user_message_id and message.get("id") == current_user_message_id)
                or (
                    expected_user_fingerprint
                    and _normalized_text_fingerprint(_message_text(message) or "")
                    == expected_user_fingerprint
                )
            )
        )
        current_user_found = current_user_found or is_current
        final_visible = _is_final_visible_message(message) and bool(_message_text(message))
        if final_visible:
            final_count += 1
        metadata = message.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        channel = metadata.get("message_type") or metadata.get("channel") or message.get("channel")
        if isinstance(channel, dict):
            channel = channel.get("name") or channel.get("type")
        summaries.append({
            "role": role if role in {"assistant", "user", "system", "tool"} else None,
            "channel": str(channel)[:40] if isinstance(channel, str) else None,
            "status": str(message.get("status"))[:40] if isinstance(message.get("status"), str) else None,
            "endTurn": message.get("end_turn") is True,
            "recipientPresent": bool(message.get("recipient")),
            "textChars": len(_message_text(message) or ""),
            "currentUser": is_current,
            "finalVisibleCandidate": final_visible,
        })
    return {
        "bytes": len(body),
        "json": True,
        "topLevelKeys": sorted(
            key for key in root if isinstance(root, dict) and isinstance(key, str)
        )[:32] if isinstance(root, dict) else [],
        "conversationIdPresent": isinstance(root, dict) and isinstance(root.get("conversation_id"), str),
        "mappingCount": len(mapping),
        "messageCount": len(messages),
        "currentUserMessageFound": current_user_found,
        "finalVisibleCandidateCount": final_count,
        "messages": summaries[:16],
    }


def _response_shape_summary(body: bytes) -> dict:
    """Describe response structure without retaining or logging answer text."""
    patch_path_tokens = {
        "message", "messages", "author", "role", "channel", "metadata",
        "message_type", "status", "content", "content_type", "parts",
        "text", "final", "analysis", "recipient", "weight", "end_turn",
        "finish_details", "finish_reason", "citations", "audio", "image",
    }

    def safe_label(value):
        if isinstance(value, dict):
            value = value.get("name") or value.get("type")
        if isinstance(value, str):
            return value[:60]
        return None

    def value_shape(value, depth=0):
        if isinstance(value, dict):
            keys = sorted(
                key for key in value
                if isinstance(key, str) and len(key) <= 48
            )[:24]
            return {
                "type": "object",
                "keys": keys,
                "fields": (
                    {key: value_shape(value[key], depth + 1) for key in keys[:12]}
                    if depth < 2 else {}
                ),
            }
        if isinstance(value, list):
            return {
                "type": "array",
                "length": len(value),
                "sample": value_shape(value[0], depth + 1)
                if value and depth < 2 else None,
            }
        if isinstance(value, str):
            return {"type": "string", "chars": len(value)}
        if value is None:
            return {"type": "null"}
        if isinstance(value, bool):
            return {"type": "boolean"}
        if isinstance(value, (int, float)):
            return {"type": "number"}
        return {"type": "other"}

    def safe_patch_path(value):
        if not isinstance(value, str) or not value.startswith("/"):
            return None
        parts = value.split("/")[1:]
        return "/" + "/".join(
            part if part in patch_path_tokens or part.isdigit() else ":field"
            for part in parts[:12]
        )

    payloads = list(_event_payloads(body))
    event_types = sorted({
        str(payload.get("type"))
        for payload in payloads
        if isinstance(payload, dict) and payload.get("type") is not None
    })
    messages = []
    event_shapes = []
    patch_ops = []
    response_root = None
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        if "p" in payload and "v" in payload:
            response_root = _apply_chatgpt_stream_patch(response_root, payload)
        event_type = safe_label(payload.get("type")) or (
            "patch" if "p" in payload else None
        )
        if event_type and event_type not in {
            item.get("type") for item in event_shapes
        } and len(event_shapes) < 8:
            event_shapes.append({
                "type": event_type,
                "shape": value_shape(payload),
            })
        candidates = []
        if isinstance(payload.get("message"), dict):
            candidates.append(payload["message"])
        if isinstance(payload.get("input_message"), dict):
            candidates.append(payload["input_message"])
        if isinstance(payload.get("messages"), list):
            candidates.extend(
                item for item in payload["messages"] if isinstance(item, dict)
            )
        for message in candidates[:8]:
            author = message.get("author")
            metadata = message.get("metadata")
            content = message.get("content")
            metadata = metadata if isinstance(metadata, dict) else {}
            channel = (
                metadata.get("message_type")
                or metadata.get("channel")
                or message.get("channel")
            )
            messages.append({
                "role": safe_label(author.get("role")) if isinstance(author, dict) else None,
                "channel": safe_label(channel),
                "status": safe_label(message.get("status")),
                "contentType": safe_label(
                    content.get("content_type")
                    if isinstance(content, dict) else type(content).__name__
                ),
                "textChars": len(_message_text(message) or ""),
            })
        patch_value = payload.get("v")
        patch_path = safe_patch_path(payload.get("p"))
        if "p" in payload and len(patch_ops) < 24:
            raw_path = payload.get("p")
            path_parts = (
                [
                    str(part) if str(part) in patch_path_tokens or str(part).isdigit()
                    else ":field"
                    for part in raw_path[:12]
                ]
                if isinstance(raw_path, list) else None
            )
            patch_value = payload.get("v")
            patch_ops.append({
                "path": patch_path,
                "pathShape": value_shape(raw_path),
                "pathParts": path_parts,
                "op": safe_label(payload.get("o")),
                "valueType": type(patch_value).__name__,
                "valueChars": len(patch_value)
                if isinstance(patch_value, str) else None,
            })
    patched_message = (
        response_root.get("message")
        if isinstance(response_root, dict) else None
    )
    patched_message_shape = None
    if isinstance(patched_message, dict):
        author = patched_message.get("author")
        metadata = patched_message.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        content = patched_message.get("content")
        channel = (
            metadata.get("message_type")
            or metadata.get("channel")
            or patched_message.get("channel")
        )
        patched_message_shape = {
            "role": safe_label(author.get("role")) if isinstance(author, dict) else None,
            "channel": safe_label(channel),
            "status": safe_label(patched_message.get("status")),
            "endTurn": patched_message.get("end_turn") is True,
            "contentType": safe_label(
                content.get("content_type")
                if isinstance(content, dict) else type(content).__name__
            ),
            "textChars": len(_message_text(patched_message) or ""),
            "finalVisibleCandidate": _is_final_visible_message(patched_message),
        }
    return {
        "bytes": len(body),
        "lineCount": len(body.splitlines()),
        "hasDataLines": b"data:" in body,
        "payloadCount": len(payloads),
        "eventTypes": event_types[:12],
        "eventShapes": event_shapes,
        "patchOps": patch_ops,
        "patchedMessage": patched_message_shape,
        "topLevelKeys": sorted({
            key for payload in payloads if isinstance(payload, dict)
            for key in payload.keys()
        })[:24],
        "messages": messages[:12],
    }


class ChatGPTNetworkResponseCapture:
    """Capture a matching response body without persisting prompts or answers."""

    def __init__(self, page, expected_user_text=None):
        self.page = page
        self._expected_user_fingerprint = _normalized_text_fingerprint(
            expected_user_text or ""
        )
        try:
            self._current_conversation_id = _chatgpt_conversation_id_from_page_url(
                str(getattr(page, "url", "") or "")
            )
        except Exception:
            self._current_conversation_id = None
        self._current_user_message_id = None
        self._turn_started = False
        self._listener = self._on_response
        self._pending = set()
        self._expiry = None
        self.response_count = 0
        self.completed_count = 0
        self.response_status = None
        self.content_type = None
        self.body_bytes = 0
        self.error_type = None
        self._final_candidates = {}
        self._observed_backend_post_routes = {}
        self._observed_backend_request_routes = {}
        self._observed_backend_response_routes = {}
        self._cdp_all_request_routes = {}
        self._cdp_response_data_totals = {}
        self._cdp_websocket_routes = {}
        self._cdp_websocket_frame_totals = {}
        self._cdp = None
        self._cdp_ready = False
        self._cdp_error_type = None
        self._cdp_error_phase = None
        self._cdp_setup_task = None
        self._cdp_streams = {}
        self._cdp_request_routes = {}
        self._cdp_detail_request_ids = {}
        self._cdp_handlers = []
        self._cdp_data_event_count = 0
        self._cdp_data_encoded_bytes = 0
        self._cdp_buffered_bytes = 0
        self._cdp_response_body_bytes = 0
        self._response_shape = {}
        self._candidate_kinds = set()
        self._last_response_activity_at = None
        self._cdp_pending = set()
        self._activity = asyncio.Event()

    @property
    def has_active_response(self):
        """Whether a matched first-party response is still open in Chromium."""
        return self.response_count > self.completed_count

    def response_activity_idle_seconds(self):
        """Return idle age for a matched response without exposing its contents."""
        if self._last_response_activity_at is None:
            return None
        return max(0.0, time.monotonic() - self._last_response_activity_at)

    def has_recent_active_response(self, max_idle_seconds=30.0):
        if not self.has_active_response:
            return False
        idle = self.response_activity_idle_seconds()
        return idle is None or idle < max(0.0, float(max_idle_seconds))

    @classmethod
    def attach(cls, page, expected_user_text=None):
        previous = getattr(page, _CAPTURE_ATTRIBUTE, None)
        if previous is not None:
            previous.detach()
        capture = cls(page, expected_user_text=expected_user_text)
        setattr(page, _CAPTURE_ATTRIBUTE, capture)
        page.on("response", capture._listener)
        context = getattr(page, "context", None)
        if callable(getattr(context, "new_cdp_session", None)):
            try:
                capture._cdp_setup_task = asyncio.create_task(
                    capture._attach_cdp()
                )
            except Exception as error:
                capture._cdp_error_type = type(error).__name__
        try:
            capture._expiry = asyncio.get_running_loop().call_later(
                900.0, capture.detach
            )
        except Exception:
            pass
        return capture

    async def ready(self):
        """Finish optional CDP observation before the UI dispatches a prompt."""
        task = self._cdp_setup_task
        if task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=_CDP_SETUP_TIMEOUT_SEC
                )
            except asyncio.TimeoutError:
                self._cdp_error_type = "SetupTimeout"
                task.cancel()
                session = self._cdp
                self._cdp = None
                self._schedule_cdp_detach(session)
            except Exception as error:
                self._cdp_error_type = type(error).__name__
        return self._cdp_ready

    async def _attach_cdp(self):
        try:
            self._cdp = await self.page.context.new_cdp_session(self.page)
            handlers = (
                ("Network.requestWillBeSent", self._on_cdp_request),
                ("Network.responseReceived", self._on_cdp_response),
                ("Network.dataReceived", self._on_cdp_data),
                ("Network.loadingFinished", self._on_cdp_loading_finished),
                ("Network.loadingFailed", self._on_cdp_loading_failed),
                ("Network.webSocketCreated", self._on_cdp_websocket_created),
                ("Network.webSocketFrameReceived", self._on_cdp_websocket_frame_received),
                ("Network.webSocketFrameSent", self._on_cdp_websocket_frame_sent),
                ("Network.webSocketClosed", self._on_cdp_websocket_closed),
            )
            for name, handler in handlers:
                self._cdp.on(name, handler)
                self._cdp_handlers.append((name, handler))
            await self._cdp.send("Network.enable")
            self._cdp_ready = True
        except Exception as error:
            self._cdp_error_type = type(error).__name__
            self._cdp_ready = False
            session = self._cdp
            self._cdp = None
            if session is not None:
                try:
                    await session.detach()
                except Exception:
                    pass

    def _track_cdp_task(self, coroutine):
        try:
            task = asyncio.create_task(coroutine)
            self._cdp_pending.add(task)
            task.add_done_callback(self._cdp_task_finished)
        except Exception as error:
            self._cdp_error_type = type(error).__name__

    def _cdp_task_finished(self, task):
        self._cdp_pending.discard(task)
        try:
            task.result()
        except BaseException:
            pass

    def _update_current_turn_identifiers(self, body=None, request=None):
        if request is not None:
            conversation_id, user_message_id = _request_turn_identifiers(
                request, self._expected_user_fingerprint
            )
            if conversation_id:
                self._current_conversation_id = conversation_id
            if user_message_id:
                self._current_user_message_id = user_message_id
            self._turn_started = True
        if body:
            for payload in _event_payloads(body):
                for item in _walk_dicts(payload):
                    for key in ("conversation_id", "conversationId"):
                        candidate = item.get(key)
                        if isinstance(candidate, str) and candidate:
                            self._current_conversation_id = candidate
                    author = item.get("author")
                    if (
                        isinstance(author, dict)
                        and author.get("role") == "user"
                        and isinstance(item.get("id"), str)
                        and item.get("id")
                    ):
                        self._current_user_message_id = item["id"]

    def _extract_candidate(self, state, body):
        if state.get("kind") == "conversation_detail":
            self._candidate_kinds.add("conversation_detail")
            self._response_shape = _conversation_detail_shape_summary(
                body,
                self._current_user_message_id,
                self._expected_user_fingerprint,
            )
            text = extract_chatgpt_conversation_detail_text(
                body,
                self._current_user_message_id,
                self._expected_user_fingerprint,
            )
        else:
            self._candidate_kinds.add("conversation_stream")
            self._update_current_turn_identifiers(body=body)
            self._response_shape = _response_shape_summary(body)
            text = extract_chatgpt_final_text(body)
        if text:
            self._final_candidates[state["sequence"]] = text
            self.body_bytes = len(body)
            self._activity.set()

    @staticmethod
    async def _safe_cdp_detach(session):
        try:
            await session.detach()
        except BaseException:
            pass

    def _schedule_cdp_detach(self, session):
        if session is None:
            return
        try:
            task = asyncio.create_task(self._safe_cdp_detach(session))
            task.add_done_callback(self._cdp_task_finished)
        except Exception:
            pass

    def _on_cdp_request(self, event):
        try:
            request_id = event.get("requestId")
            request = event.get("request") or {}
            url = str(request.get("url") or "")
            method = str(request.get("method") or "")
            route = _safe_backend_route(url)
            request_id = event.get("requestId")
            if route:
                request_key = f"{method.upper() or 'UNKNOWN'} {route}"
                self._observed_backend_request_routes[request_key] = (
                    self._observed_backend_request_routes.get(request_key, 0) + 1
                )
                if request_id:
                    self._cdp_all_request_routes[request_id] = route
                if method.upper() == "POST":
                    self._observed_backend_post_routes[route] = (
                        self._observed_backend_post_routes.get(route, 0) + 1
                    )
            if request_id and is_chatgpt_conversation_response(url, method):
                self._cdp_request_routes[request_id] = route or "backend-api/conversation"
                if method.upper() == "POST":
                    self._update_current_turn_identifiers(request=request)
            elif request_id and is_chatgpt_conversation_detail_response(url, method):
                conversation_id = _chatgpt_conversation_id_from_url(url)
                if (
                    self._turn_started
                    and conversation_id
                    and self._current_conversation_id
                    and conversation_id == self._current_conversation_id
                ):
                    self._cdp_detail_request_ids[request_id] = conversation_id
        except Exception as error:
            self._cdp_error_type = type(error).__name__
            self._cdp_error_phase = "request_identity"

    def _on_cdp_response(self, event):
        try:
            request_id = event.get("requestId")
            response = event.get("response") or {}
            all_route = self._cdp_all_request_routes.get(request_id)
            if all_route:
                try:
                    status = int(response.get("status") or 0)
                except (TypeError, ValueError):
                    status = 0
                mime = str(response.get("mimeType") or "unknown").lower()
                response_key = f"{status} {mime} {all_route}"
                self._observed_backend_response_routes[response_key] = (
                    self._observed_backend_response_routes.get(response_key, 0) + 1
                )
            route = self._cdp_request_routes.get(request_id)
            is_detail = request_id in self._cdp_detail_request_ids
            if not route and not is_detail:
                return
            try:
                status = int(response.get("status") or 0)
            except (TypeError, ValueError):
                status = 0
            mime = str(response.get("mimeType") or "").lower()
            self.response_count += 1
            sequence = self.response_count
            self.response_status = status
            self.content_type = mime
            self._last_response_activity_at = time.monotonic()
            self._activity.set()
            if not 200 <= status < 300 or mime not in {
                "text/event-stream", "application/json"
            }:
                self.completed_count = max(self.completed_count, sequence)
                self._cdp_request_routes.pop(request_id, None)
                self._cdp_detail_request_ids.pop(request_id, None)
                return
            self._cdp_streams[request_id] = {
                "sequence": sequence,
                "buffer": bytearray(),
                "pending": [],
                "ready": False,
                "kind": "conversation_detail" if is_detail else "conversation_stream",
            }
            self._track_cdp_task(self._start_cdp_stream(request_id))
        except Exception as error:
            self._cdp_error_type = type(error).__name__
            self._cdp_error_phase = "response_event"

    async def _start_cdp_stream(self, request_id):
        state = self._cdp_streams.get(request_id)
        if state is None or self._cdp is None:
            return
        try:
            result = await self._cdp.send(
                "Network.streamResourceContent", {"requestId": request_id}
            )
            buffered = result.get("bufferedData") or ""
            if buffered:
                decoded = base64.b64decode(buffered)
                state["buffer"].extend(decoded)
                self._cdp_buffered_bytes += len(decoded)
            state["ready"] = True
            for chunk in state["pending"]:
                self._append_cdp_chunk(state, chunk)
            state["pending"].clear()
            self._extract_cdp_candidate(state)
        except Exception as error:
            self._cdp_error_type = type(error).__name__
            self._cdp_error_phase = "stream_resource_content"
            # The detail endpoint may have completed before CDP enables body
            # streaming. Keep its request state so loadingFinished can retrieve
            # the browser-owned body through getResponseBody.
            state["ready"] = True
            state["bodyFallbackOnly"] = True
            for chunk in state["pending"]:
                self._append_cdp_chunk(state, chunk)
            state["pending"].clear()

    def _append_cdp_chunk(self, state, encoded_chunk):
        try:
            chunk = base64.b64decode(encoded_chunk)
        except Exception as error:
            self._cdp_error_type = type(error).__name__
            self._cdp_error_phase = "decode_chunk"
            return
        if len(state["buffer"]) + len(chunk) > _MAX_CAPTURE_BYTES:
            state["buffer"].clear()
            state["overflow"] = True
            return
        state["buffer"].extend(chunk)
        if b"\n\n" in chunk or b"\r\n\r\n" in chunk:
            self._extract_cdp_candidate(state)

    def _extract_cdp_candidate(self, state):
        if state.get("overflow"):
            return
        body = bytes(state["buffer"])
        self._extract_candidate(state, body)

    def _on_cdp_data(self, event):
        request_id = event.get("requestId")
        route = self._cdp_all_request_routes.get(request_id)
        encoded = event.get("data")
        if route:
            totals = self._cdp_response_data_totals.setdefault(
                route, {"events": 0, "encodedChars": 0}
            )
            totals["events"] += 1
            totals["encodedChars"] += len(encoded or "")
        state = self._cdp_streams.get(request_id)
        if state is None:
            return
        self._last_response_activity_at = time.monotonic()
        self._cdp_data_event_count += 1
        self._cdp_data_encoded_bytes += len(encoded or "")
        if not encoded:
            return
        if not state.get("ready"):
            state["pending"].append(encoded)
        else:
            self._append_cdp_chunk(state, encoded)

    def _on_cdp_loading_finished(self, event):
        request_id = event.get("requestId")
        self._cdp_all_request_routes.pop(request_id, None)
        state = self._cdp_streams.pop(request_id, None)
        self._cdp_request_routes.pop(request_id, None)
        self._cdp_detail_request_ids.pop(request_id, None)
        if state is not None:
            self._extract_cdp_candidate(state)
            self.completed_count = max(self.completed_count, state["sequence"])
            self._activity.set()
            self._track_cdp_task(
                self._read_cdp_final_body(request_id, state)
            )

    async def _read_cdp_final_body(self, request_id, state):
        if state["sequence"] in self._final_candidates or self._cdp is None:
            return
        try:
            result = await self._cdp.send(
                "Network.getResponseBody", {"requestId": request_id}
            )
            raw = result.get("body") or ""
            body = (
                base64.b64decode(raw)
                if result.get("base64Encoded")
                else str(raw).encode("utf-8")
            )
            if len(body) > _MAX_CAPTURE_BYTES:
                return
            self._cdp_response_body_bytes += len(body)
            self._extract_candidate(state, body)
        except Exception as error:
            self._cdp_error_type = type(error).__name__
            self._cdp_error_phase = "get_response_body"

    def _on_cdp_loading_failed(self, event):
        request_id = event.get("requestId")
        self._cdp_all_request_routes.pop(request_id, None)
        state = self._cdp_streams.pop(request_id, None)
        self._cdp_request_routes.pop(request_id, None)
        self._cdp_detail_request_ids.pop(request_id, None)
        if state is not None:
            self.completed_count = max(self.completed_count, state["sequence"])
            self._activity.set()

    def _on_cdp_websocket_created(self, event):
        request_id = event.get("requestId")
        route = _safe_backend_route(str(event.get("url") or ""))
        if request_id and route:
            self._cdp_websocket_routes[request_id] = route

    def _record_cdp_websocket_frame(self, event, direction):
        request_id = event.get("requestId")
        route = self._cdp_websocket_routes.get(request_id)
        if not route:
            return
        frame = event.get("response") or {}
        payload = frame.get("payloadData")
        frame_key = f"{direction} {route}"
        totals = self._cdp_websocket_frame_totals.setdefault(
            frame_key, {"frames": 0, "payloadChars": 0}
        )
        totals["frames"] += 1
        totals["payloadChars"] += len(payload) if isinstance(payload, str) else 0

    def _on_cdp_websocket_frame_received(self, event):
        self._record_cdp_websocket_frame(event, "received")

    def _on_cdp_websocket_frame_sent(self, event):
        self._record_cdp_websocket_frame(event, "sent")

    def _on_cdp_websocket_closed(self, event):
        self._cdp_websocket_routes.pop(event.get("requestId"), None)

    def _on_response(self, response):
        try:
            request = response.request
            method = getattr(request, "method", "")
            is_detail = is_chatgpt_conversation_detail_response(response.url, method)
            if self._cdp_ready and not is_detail:
                return
            route = _safe_backend_route(response.url)
            if route:
                method_name = (method or "UNKNOWN").upper()
                request_key = f"{method_name} {route}"
                self._observed_backend_request_routes[request_key] = (
                    self._observed_backend_request_routes.get(request_key, 0) + 1
                )
                if method_name == "POST":
                    self._observed_backend_post_routes[route] = (
                        self._observed_backend_post_routes.get(route, 0) + 1
                    )
            if is_chatgpt_conversation_response(response.url, method):
                if method_name == "POST":
                    try:
                        post_data = getattr(request, "post_data", None)
                        if callable(post_data):
                            post_data = post_data()
                        self._update_current_turn_identifiers(request={
                            "url": response.url,
                            "postData": post_data,
                        })
                    except Exception:
                        self._turn_started = True
                kind = "conversation_stream"
            elif is_detail:
                conversation_id = _chatgpt_conversation_id_from_url(response.url)
                if not (
                    self._turn_started
                    and conversation_id
                    and self._current_conversation_id
                    and conversation_id == self._current_conversation_id
                ):
                    return
                kind = "conversation_detail"
            else:
                return
            self.response_count += 1
            sequence = self.response_count
            self._last_response_activity_at = time.monotonic()
            task = asyncio.create_task(self._read_response(response, sequence, kind))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
        except Exception as error:
            self.error_type = type(error).__name__

    async def _read_response(self, response, sequence: int, kind="conversation_stream"):
        try:
            self.response_status = response.status
            if not 200 <= int(response.status) < 300:
                self.completed_count = max(self.completed_count, sequence)
                return
            headers = await response.all_headers()
            self.content_type = headers.get("content-type", "")
            media_type = self.content_type.split(";", 1)[0].strip().lower()
            if media_type not in {"text/event-stream", "application/json"}:
                self.completed_count = max(self.completed_count, sequence)
                return
            content_length = headers.get("content-length", "")
            try:
                if int(content_length) > _MAX_CAPTURE_BYTES:
                    self.completed_count = max(self.completed_count, sequence)
                    return
            except (TypeError, ValueError):
                pass
            # Playwright emits `response` as soon as headers arrive. Wait for
            # the browser-owned transfer to finish before asking Chromium for
            # its body; otherwise a fast consumer can evict the body handle.
            await response.finished()
            body = await response.body()
            self.body_bytes = len(body)
            if self.body_bytes > _MAX_CAPTURE_BYTES:
                return
            self._extract_candidate({"sequence": sequence, "kind": kind}, body)
            self.completed_count = max(self.completed_count, sequence)
        except Exception as error:
            self.error_type = type(error).__name__
            self.completed_count = max(self.completed_count, sequence)

    async def final_text(self, timeout: float = 2.5) -> Optional[str]:
        if self._cdp_setup_task is not None and not self._cdp_setup_task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._cdp_setup_task),
                    timeout=max(0.0, timeout),
                )
            except Exception:
                pass
        deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
        while True:
            if self._final_candidates:
                return self._final_candidates[max(self._final_candidates)]
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            pending = tuple(self._pending) + tuple(self._cdp_pending)
            if pending:
                try:
                    done, _still_pending = await asyncio.wait(
                        pending, timeout=min(remaining, 0.2)
                    )
                    self._pending.difference_update(done)
                    self._cdp_pending.difference_update(done)
                except Exception as error:
                    self.error_type = type(error).__name__
                continue
            if self.response_count and self.completed_count < self.response_count:
                self._activity.clear()
                try:
                    await asyncio.wait_for(
                        self._activity.wait(), timeout=min(remaining, 0.2)
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            break
        if not self._final_candidates:
            return None
        return self._final_candidates[max(self._final_candidates)]

    def diagnostic(self) -> dict:
        return {
            "responseCount": self.response_count,
            "completedCount": self.completed_count,
            "responseStatus": self.response_status,
            "contentType": self.content_type,
            "bodyBytes": self.body_bytes,
            "finalTextChars": len(
                self._final_candidates[max(self._final_candidates)]
                if self._final_candidates
                else ""
            ),
            "errorType": self.error_type,
            "observedBackendPostRoutes": dict(
                list(self._observed_backend_post_routes.items())[:12]
            ),
            "observedBackendRequestRoutes": dict(
                list(self._observed_backend_request_routes.items())[:24]
            ),
            "observedBackendResponseRoutes": dict(
                list(self._observed_backend_response_routes.items())[:24]
            ),
            "cdpResponseDataTotals": dict(
                list(self._cdp_response_data_totals.items())[:24]
            ),
            "cdpWebSocketFrameTotals": dict(
                list(self._cdp_websocket_frame_totals.items())[:16]
            ),
            "captureMode": "cdp_stream" if self._cdp_ready else "playwright_body",
            "activeResponse": self.has_active_response,
            "responseActivityIdleSec": (
                round(self.response_activity_idle_seconds(), 3)
                if self.response_activity_idle_seconds() is not None else None
            ),
            "cdpErrorType": self._cdp_error_type,
            "cdpErrorPhase": self._cdp_error_phase,
            "candidateKinds": sorted(self._candidate_kinds),
            "currentTurnIdentity": {
                "conversationKnown": bool(self._current_conversation_id),
                "userMessageKnown": bool(self._current_user_message_id),
                "expectedUserFingerprintProvided": bool(self._expected_user_fingerprint),
            },
            "cdpDataEventCount": self._cdp_data_event_count,
            "cdpDataEncodedBytes": self._cdp_data_encoded_bytes,
            "cdpBufferedBytes": self._cdp_buffered_bytes,
            "cdpResponseBodyBytes": self._cdp_response_body_bytes,
            "responseShape": self._response_shape,
        }

    def detach(self):
        try:
            self.page.remove_listener("response", self._listener)
        except Exception:
            pass
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        try:
            if getattr(self.page, _CAPTURE_ATTRIBUTE, None) is self:
                delattr(self.page, _CAPTURE_ATTRIBUTE)
        except Exception:
            pass
        session = self._cdp
        self._cdp = None
        if session is not None:
            try:
                for name, handler in self._cdp_handlers:
                    session.remove_listener(name, handler)
            except Exception:
                pass
        if self._cdp_setup_task is not None and not self._cdp_setup_task.done():
            self._cdp_setup_task.cancel()
        for task in tuple(self._cdp_pending):
            task.cancel()
        self._schedule_cdp_detach(session)
        self._cdp_handlers.clear()
        self._cdp_streams.clear()
        self._cdp_request_routes.clear()
        self._cdp_detail_request_ids.clear()
