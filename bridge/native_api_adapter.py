"""
Native OpenAI / DeepSeek API Adapter for Multi-Provider Web Bridge.

Provides:
1. NativeSessionTracker: Stable session fingerprinting, metadata tracking, and context window auto-rotation.
2. Tool Calling Adapter: Converts OpenAI tools schema into model instructions and parses <tool_call> tags from responses.
3. Smart Context Packaging (compile_native_turn): Seed markdown packaging for new sessions, delta extraction for resumed sessions.
4. SSE Stream Formatter (format_sse_response_chunks): Formats OpenAI-compatible chat.completion.chunk SSE streams.
"""

import os
import re
import json
import time
import uuid
import hashlib
import tempfile
import threading
from typing import Dict, List, Optional, Any, Union, Tuple, AsyncGenerator

DEFAULT_MAX_SESSION_TURNS = 30
DEFAULT_MAX_SESSION_CHARS = 180000
DEFAULT_SEED_FILE_THRESHOLD_CHARS = 6000
DEFAULT_PROMPT_FILE_THRESHOLD_CHARS = 20000

MAX_SESSION_TURNS = int(os.environ.get("BRIDGE_MAX_SESSION_TURNS", str(DEFAULT_MAX_SESSION_TURNS)))
MAX_SESSION_CHARS = int(os.environ.get("BRIDGE_MAX_SESSION_CHARS", str(DEFAULT_MAX_SESSION_CHARS)))
SEED_FILE_THRESHOLD_CHARS = int(os.environ.get("BRIDGE_SEED_FILE_THRESHOLD_CHARS", str(DEFAULT_SEED_FILE_THRESHOLD_CHARS)))


def extract_message_text(content: Any) -> str:
    """Extracts raw text string from diverse message content forms (str, list of dicts, parts)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part.strip())
            elif isinstance(part, dict):
                p_type = part.get("type")
                if p_type == "text":
                    parts.append(str(part.get("text", "")).strip())
            elif hasattr(part, "type"):
                if getattr(part, "type") == "text" and getattr(part, "text", None):
                    parts.append(str(part.text).strip())
        return " ".join(p for p in parts if p).strip()
    return str(content).strip()


def format_message(msg: Any) -> str:
    """Formats a single Message into a clear, legible conversational turn."""
    role = (getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else "") or "").lower().strip()
    content = getattr(msg, "content", None) if hasattr(msg, "content") else (msg.get("content") if isinstance(msg, dict) else None)
    text = extract_message_text(content)
    name = getattr(msg, "name", None) if hasattr(msg, "name") else (msg.get("name") if isinstance(msg, dict) else None)
    tool_call_id = getattr(msg, "tool_call_id", None) if hasattr(msg, "tool_call_id") else (msg.get("tool_call_id") if isinstance(msg, dict) else None)
    tool_calls = getattr(msg, "tool_calls", None) if hasattr(msg, "tool_calls") else (msg.get("tool_calls") if isinstance(msg, dict) else None)

    if role in ("system", "developer"):
        return f"System: {text}"
    if role == "user":
        prefix = f"User ({name}):" if name else "User:"
        return f"{prefix} {text}"
    if role == "assistant":
        tool_calls_text = ""
        if tool_calls and isinstance(tool_calls, list):
            calls_list = []
            for tc in tool_calls:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                fn_name = fn.get("name", "")
                fn_args = fn.get("arguments", "{}")
                if isinstance(fn_args, str):
                    try:
                        args_val = json.loads(fn_args)
                    except Exception:
                        args_val = fn_args
                else:
                    args_val = fn_args
                call_json = json.dumps({"name": fn_name, "arguments": args_val}, ensure_ascii=False)
                calls_list.append(f"<tool_call>\n{call_json}\n</tool_call>")
            tool_calls_text = "\n".join(calls_list)
        if text and tool_calls_text:
            return f"Assistant: {text}\n{tool_calls_text}"
        elif tool_calls_text:
            return f"Assistant:\n{tool_calls_text}"
        else:
            return f"Assistant: {text}"
    if role == "tool":
        name_str = f" [{name}]" if name else ""
        call_id_str = f" (call_id: {tool_call_id})" if tool_call_id else ""
        return f"Tool{name_str}{call_id_str}:\n{text}"
    return f"{role.capitalize()}: {text}"


class NativeSessionTracker:
    """
    Manages session fingerprinting and state across multi-turn interactions.
    Provides stable session IDs for external clients (OpenAI SDK, Claude Code, Cline, etc.)
    and handles automatic rotation when sessions reach turn or character limits.
    """
    def __init__(self, store_path: Optional[str] = None):
        self.store_path = store_path
        self._lock = threading.Lock()
        self._sessions: Dict[str, Dict[str, Any]] = {}
        if self.store_path and os.path.exists(self.store_path):
            self._load()

    def _load(self) -> None:
        try:
            with open(self.store_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._sessions = data.get("sessions", {})
        except Exception:
            self._sessions = {}

    def _save(self) -> None:
        if not self.store_path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.store_path)), exist_ok=True)
            tmp = f"{self.store_path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            payload = json.dumps({"version": 1, "sessions": self._sessions}, ensure_ascii=False)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.store_path)
        except Exception:
            pass

    def compute_fingerprint(self, provider: str, messages: List[Any], user: Optional[str] = None) -> str:
        """
        Computes a stable base session ID.
        If user is provided: 'native-user-{sanitized_user}'
        Otherwise computes SHA-256 over (provider, first_system_text, first_user_text).
        """
        if user and str(user).strip():
            sanitized = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(user).strip())[:48]
            return f"native-user-{sanitized}"

        first_system = ""
        first_user = ""
        first_any = ""

        for m in messages:
            role = (getattr(m, "role", None) or (m.get("role") if isinstance(m, dict) else "") or "").lower().strip()
            content = getattr(m, "content", None) if hasattr(m, "content") else (m.get("content") if isinstance(m, dict) else "")
            text = extract_message_text(content)
            if not text:
                continue
            if not first_any:
                first_any = text
            if role in ("system", "developer") and not first_system:
                first_system = text
            elif role == "user" and not first_user:
                first_user = text

        norm_sys = re.sub(r"\s+", " ", first_system).strip()[:4000]
        norm_user = re.sub(r"\s+", " ", first_user or first_any).strip()[:4000]

        fingerprint_raw = f"{provider}:{norm_sys}:{norm_user}"
        digest = hashlib.sha256(fingerprint_raw.encode("utf-8")).hexdigest()
        return f"native-{digest[:16]}"

    def resolve_session_id(self, provider: str, req: Any) -> Tuple[str, bool]:
        """
        Resolves session_id and determines if rotation is needed.
        Returns: (session_id, is_rotated)
        """
        with self._lock:
            messages = getattr(req, "messages", []) or []
            user = getattr(req, "user", None)
            base_id = self.compute_fingerprint(provider, messages, user=user)

            state = self._sessions.get(base_id)
            if state is None:
                state = {
                    "base_id": base_id,
                    "session_id": base_id,
                    "provider": provider,
                    "system_hash": "",
                    "turn_count": 0,
                    "total_chars": 0,
                    "gen": 1,
                    "created_at": time.time(),
                    "updated_at": time.time(),
                }
                self._sessions[base_id] = state
                self._save()
                return base_id, False

            max_turns = int(os.environ.get("BRIDGE_MAX_SESSION_TURNS", str(DEFAULT_MAX_SESSION_TURNS)))
            max_chars = int(os.environ.get("BRIDGE_MAX_SESSION_CHARS", str(DEFAULT_MAX_SESSION_CHARS)))
            is_rotated = False

            if state.get("turn_count", 0) >= max_turns or state.get("total_chars", 0) >= max_chars:
                state["gen"] = state.get("gen", 1) + 1
                state["turn_count"] = 0
                state["total_chars"] = 0
                state["session_id"] = f"{base_id}-g{state['gen']}"
                state["updated_at"] = time.time()
                is_rotated = True
                self._save()

            return state["session_id"], is_rotated

    def record_turn(self, session_id: str, prompt_chars: int, response_chars: int, system_hash: Optional[str] = None) -> None:
        """Updates turn count, total chars, and system hash for the session."""
        with self._lock:
            target_state = None
            for state in self._sessions.values():
                if state.get("session_id") == session_id or state.get("base_id") == session_id:
                    target_state = state
                    break
                if session_id.startswith(state.get("base_id", "___")):
                    target_state = state
                    break

            if target_state is None:
                target_state = {
                    "base_id": session_id,
                    "session_id": session_id,
                    "provider": "unknown",
                    "system_hash": system_hash or "",
                    "turn_count": 0,
                    "total_chars": 0,
                    "gen": 1,
                    "created_at": time.time(),
                    "updated_at": time.time(),
                }
                self._sessions[session_id] = target_state

            target_state["turn_count"] = target_state.get("turn_count", 0) + 1
            target_state["total_chars"] = target_state.get("total_chars", 0) + (prompt_chars + response_chars)
            if system_hash:
                target_state["system_hash"] = system_hash
            target_state["updated_at"] = time.time()
            self._save()

    def get_system_hash(self, session_id: str) -> Optional[str]:
        with self._lock:
            for state in self._sessions.values():
                if state.get("session_id") == session_id or session_id.startswith(state.get("base_id", "___")):
                    return state.get("system_hash")
            return None

    def get_session_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for state in self._sessions.values():
                if state.get("session_id") == session_id or session_id.startswith(state.get("base_id", "___")):
                    return dict(state)
            return None


def build_tools_system_instruction(tools: Optional[List[Dict[str, Any]]], tool_choice: Any = None) -> str:
    """Converts OpenAI tools schema list into a clear instruction block telling the model how to invoke tools."""
    if not tools:
        return ""

    normalized_tools = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function", tool)
        if isinstance(fn, dict) and "name" in fn:
            normalized_tools.append({
                "name": fn["name"],
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
            })

    tools_json = json.dumps(normalized_tools, indent=2, ensure_ascii=False)

    instruction = (
        "## Tools & Available Functions\n"
        "You have access to the following tools:\n"
        f"```json\n{tools_json}\n```\n\n"
        "### Tool Calling Convention\n"
        "To invoke one or more tools, emit `<tool_call>` tags formatted as strictly valid JSON:\n"
        "<tool_call>\n"
        '{"name": "function_name", "arguments": {"param_key": "param_value"}}\n'
        "</tool_call>\n\n"
        "Rules:\n"
        "1. Multiple parallel tool calls are supported by outputting consecutive `<tool_call>...</tool_call>` blocks.\n"
        "2. Do NOT wrap `<tool_call>` inside markdown code fences (no ```).\n"
        "3. Provide valid JSON arguments according to the parameter schema.\n"
        "4. If no tool is needed, answer the user request directly."
    )

    if tool_choice:
        if tool_choice == "none":
            instruction += "\n\n[CRITICAL TOOL_CHOICE]: 'none'. Do NOT call any tools under any circumstances. Reply directly using natural text."
        elif tool_choice == "required":
            instruction += "\n\n[CRITICAL TOOL_CHOICE]: 'required'. You MUST emit at least one `<tool_call>` in your response."
        elif isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
            fn_name = tool_choice.get("function", {}).get("name", "")
            if fn_name:
                instruction += f"\n\n[CRITICAL TOOL_CHOICE]: You MUST invoke the tool '{fn_name}' using a `<tool_call>` block."
        elif isinstance(tool_choice, str) and tool_choice not in ("auto", "none", "required"):
            instruction += f"\n\n[CRITICAL TOOL_CHOICE]: You MUST invoke the tool '{tool_choice}' using a `<tool_call>` block."

    return instruction


def parse_tool_calls_from_response(
    text: str,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Optional[str], Optional[List[Dict[str, Any]]]]:
    """
    Parses <tool_call>...</tool_call> blocks (and fallback fenced ```tool_call or ```json).
    Normalizes each extracted call into OpenAI format:
    {"id": "call_...", "type": "function", "function": {"name": ..., "arguments": ...}}
    Strips blocks from assistant text. If remaining text is empty, returns content = None.
    """
    if not text:
        return None, None

    declared_names = set()
    if tools:
        for t in tools:
            if isinstance(t, dict):
                fn = t.get("function", t)
                if isinstance(fn, dict) and "name" in fn:
                    declared_names.add(fn["name"])

    tool_calls: List[Dict[str, Any]] = []
    working_text = text

    # 1. Parse standard <tool_call>...</tool_call> blocks
    tool_call_pattern = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
    matches = list(tool_call_pattern.finditer(working_text))

    for m in matches:
        raw_body = m.group(1).strip()
        try:
            call_dict = json.loads(raw_body)
            if isinstance(call_dict, dict) and "name" in call_dict:
                fn_name = call_dict["name"]
                args_obj = call_dict.get("arguments", call_dict.get("parameters", {}))
                args_str = json.dumps(args_obj, ensure_ascii=False) if isinstance(args_obj, (dict, list)) else str(args_obj if args_obj is not None else "{}")
                tool_calls.append({
                    "id": f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {
                        "name": fn_name,
                        "arguments": args_str,
                    }
                })
        except Exception:
            pass

    if matches:
        working_text = tool_call_pattern.sub("", working_text)

    # 2. Fallback: fenced ```tool_call or ```json containing {"name": ..., "arguments": ...}
    # only when declared tools are active and no tool calls found or fallback matches declared tools
    if not tool_calls and declared_names:
        fenced_pattern = re.compile(r"```(?:tool_call|json)?\s*(\{[\s\S]*?\})\s*```")
        fenced_matches = list(fenced_pattern.finditer(working_text))
        for fm in fenced_matches:
            raw_json = fm.group(1).strip()
            try:
                call_dict = json.loads(raw_json)
                if isinstance(call_dict, dict) and call_dict.get("name") in declared_names:
                    fn_name = call_dict["name"]
                    args_obj = call_dict.get("arguments", call_dict.get("parameters", {}))
                    args_str = json.dumps(args_obj, ensure_ascii=False) if isinstance(args_obj, (dict, list)) else str(args_obj if args_obj is not None else "{}")
                    tool_calls.append({
                        "id": f"call_{uuid.uuid4().hex[:24]}",
                        "type": "function",
                        "function": {
                            "name": fn_name,
                            "arguments": args_str,
                        }
                    })
                    working_text = working_text.replace(fm.group(0), "")
            except Exception:
                pass

    if tool_calls:
        remaining_content = working_text.strip()
        final_content = remaining_content if remaining_content else None
        return final_content, tool_calls

    return text, None


def compile_native_turn(
    req: Any,
    session_id: str,
    *,
    is_resumed_session: bool = False,
    known_system_hash: Optional[str] = None,
    temp_dir: Optional[str] = None,
) -> Tuple[str, Optional[str], bool, str]:
    """
    Smart Context & History Packaging:
    - Case A (New session): Packages prior turns or large system/tools prompt into context_seed_<session_id>.md attachment.
    - Case B (Resumed session): Sends ONLY delta messages since last assistant turn without repeating system prompt or prior turns.
    Returns: (prompt_to_send, seed_file_path, is_json_requested, system_hash)
    """
    messages = getattr(req, "messages", []) or []
    tools = getattr(req, "tools", None)
    tool_choice = getattr(req, "tool_choice", None)

    system_directives: List[str] = []
    for msg in messages:
        role = (getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else "") or "").lower().strip()
        if role in ("system", "developer"):
            content = getattr(msg, "content", None) if hasattr(msg, "content") else (msg.get("content") if isinstance(msg, dict) else None)
            text = extract_message_text(content)
            if text:
                system_directives.append(text)

    tools_instruction = build_tools_system_instruction(tools, tool_choice) if tools else ""

    combined_system_parts: List[str] = []
    if system_directives:
        combined_system_parts.append("\n\n".join(system_directives))
    if tools_instruction:
        combined_system_parts.append(tools_instruction)

    system_body = "\n\n".join(combined_system_parts).strip()
    system_hash = hashlib.sha256(system_body.encode("utf-8")).hexdigest()

    # Detect JSON request
    is_json_requested = False
    response_format = getattr(req, "response_format", None)
    if response_format:
        if isinstance(response_format, dict):
            rf_type = str(response_format.get("type", "")).lower()
            if any(k in rf_type for k in ("json", "schema", "object")) or "json_schema" in response_format:
                is_json_requested = True
        elif isinstance(response_format, str) and "json" in response_format.lower():
            is_json_requested = True

    non_sys = [
        m for m in messages
        if (getattr(m, "role", None) or (m.get("role") if isinstance(m, dict) else "") or "").lower().strip() not in ("system", "developer")
    ]

    # Find the last assistant message index to separate prior history from trailing delta
    last_asst_idx = -1
    for i in range(len(non_sys) - 1, -1, -1):
        m_role = (getattr(non_sys[i], "role", None) or (non_sys[i].get("role") if isinstance(non_sys[i], dict) else "") or "").lower().strip()
        if m_role == "assistant":
            last_asst_idx = i
            break

    if last_asst_idx != -1:
        prior_msgs = non_sys[:last_asst_idx + 1]
        delta_msgs = non_sys[last_asst_idx + 1:]
        if not delta_msgs:
            # Assistant is the very last message in the input list
            prior_msgs = non_sys[:-1]
            delta_msgs = [non_sys[-1]]
    else:
        prior_msgs = non_sys[:-1]
        delta_msgs = [non_sys[-1]] if non_sys else []

    formatted_prior = [format_message(m) for m in prior_msgs]
    formatted_delta = [format_message(m) for m in delta_msgs]

    if len(delta_msgs) == 1 and (getattr(delta_msgs[0], "role", None) or (delta_msgs[0].get("role") if isinstance(delta_msgs[0], dict) else "") or "").lower().strip() == "user":
        content = getattr(delta_msgs[0], "content", None) if hasattr(delta_msgs[0], "content") else (delta_msgs[0].get("content") if isinstance(delta_msgs[0], dict) else None)
        latest_delta_text = extract_message_text(content)
    else:
        latest_delta_text = "\n\n".join(formatted_delta)

    # Check JSON cues in prompt text
    all_text_probe = (system_body + " " + latest_delta_text).lower()
    if any(k in all_text_probe for k in ("strictly valid json", "valid raw json", "format: json", "return json", "json only", "định dạng json", "trả về json", "json object")):
        is_json_requested = True

    seed_file_threshold = int(os.environ.get("BRIDGE_SEED_FILE_THRESHOLD_CHARS", str(DEFAULT_SEED_FILE_THRESHOLD_CHARS)))

    # --- CASE A: New Session / Seed Turn ---
    if not is_resumed_session:
        history_text = "\n\n".join(formatted_prior)
        total_seed_chars = len(system_body) + len(history_text)

        if formatted_prior or (total_seed_chars > seed_file_threshold):
            clean_sid = re.sub(r"[^a-zA-Z0-9_\-]", "_", session_id)
            seed_file_content = (
                f"# Conversation Seed Context & Directives\n\n"
                f"## System Directives & Tool Schemas\n{system_body if system_body else 'None'}\n\n"
                f"## Prior Conversation History\n{history_text if history_text else 'None'}\n"
            )

            fd, seed_file_path = tempfile.mkstemp(
                prefix=f"context_seed_{clean_sid}-",
                suffix=".md",
                dir=temp_dir,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(seed_file_content)
                f.flush()
                os.fsync(f.fileno())

            file_basename = os.path.basename(seed_file_path)
            prompt_to_send = (
                f"[CONTEXT ATTACHED]: Please review the attached context file `{file_basename}` "
                f"which contains complete system instructions, tool definitions, and conversation history. "
                f"Follow all directives and continue the conversation seamlessly.\n\n"
                f"[CURRENT REQUEST]:\n{latest_delta_text}"
            )
            if is_json_requested:
                prompt_to_send += (
                    "\n\n[CRITICAL FORMAT INSTRUCTION]: Your output MUST be strictly valid raw JSON. "
                    "Do NOT include markdown code blocks (no ```json or ```), no pleasantries, and no explanations. Return JSON only."
                )
            return prompt_to_send, seed_file_path, is_json_requested, system_hash

        # Simple initial turn within inline threshold
        sections = []
        if system_body:
            sections.append(f"### [SYSTEM INSTRUCTIONS]:\n{system_body}")
        if is_json_requested:
            sections.append(
                "[CRITICAL FORMAT INSTRUCTION]: Your output MUST be strictly valid raw JSON. "
                "Do NOT include markdown code blocks or explanations. Return JSON only."
            )
        sections.append(f"### [CURRENT REQUEST]:\n{latest_delta_text}" if sections else latest_delta_text)
        return "\n\n".join(sections), None, is_json_requested, system_hash

    # --- CASE B: Resumed Session ---
    sections = []
    # Only re-send system directives if system_hash actually changed
    if known_system_hash and system_hash != known_system_hash:
        sections.append(f"### [UPDATED SYSTEM INSTRUCTIONS & TOOLS]:\n{system_body}")
    if is_json_requested:
        sections.append(
            "[CRITICAL FORMAT INSTRUCTION]: Your output MUST be strictly valid raw JSON. "
            "Return ONLY the JSON payload without any markdown blocks or conversational text."
        )
    sections.append(f"### [CURRENT REQUEST]:\n{latest_delta_text}" if sections else latest_delta_text)
    return "\n\n".join(sections), None, is_json_requested, system_hash


async def format_sse_response_chunks(
    response_id: str,
    model: str,
    *,
    content: Optional[str] = None,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    reasoning_content: Optional[str] = None,
    stream_options: Optional[Dict[str, Any]] = None,
    usage: Optional[Dict[str, Any]] = None,
    content_stream: Optional[AsyncGenerator[str, None]] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> AsyncGenerator[str, None]:
    """
    Async generator that streams OpenAI-compatible chat.completion.chunk SSE events:
    - Initial role chunk
    - Live content deltas (or chunked content + reasoning_content)
    - Tool calls chunk(s) with finish_reason: None, then finish_reason: "tool_calls"
    - Otherwise finish_reason: "stop"
    - Usage chunk if stream_options.include_usage: True
    - data: [DONE]\n\n
    """
    now = int(time.time())

    # 1. Initial role chunk
    role_chunk = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": now,
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"role": "assistant", "content": ""},
            "finish_reason": None,
        }]
    }
    yield f"data: {json.dumps(role_chunk, ensure_ascii=False)}\n\n"

    # 2. Reasoning content if any
    if reasoning_content:
        reasoning_chunk = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": now,
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {"reasoning_content": reasoning_content},
                "finish_reason": None,
            }]
        }
        yield f"data: {json.dumps(reasoning_chunk, ensure_ascii=False)}\n\n"

    # 3. Content and Tool Calls resolution
    active_content = content
    active_tool_calls = tool_calls

    if content_stream is not None:
        if tools:
            # Buffer stream to parse tool call tags reliably
            accumulated = []
            async for chunk in content_stream:
                accumulated.append(chunk)
            full_text = "".join(accumulated)
            parsed_content, parsed_tool_calls = parse_tool_calls_from_response(full_text, tools)
            active_content = parsed_content
            active_tool_calls = parsed_tool_calls or tool_calls
        else:
            async for chunk in content_stream:
                if chunk:
                    c_payload = {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {"content": chunk},
                            "finish_reason": None,
                        }]
                    }
                    yield f"data: {json.dumps(c_payload, ensure_ascii=False)}\n\n"
            active_content = None
    else:
        if content and tools and tool_calls is None:
            parsed_content, parsed_tool_calls = parse_tool_calls_from_response(content, tools)
            active_content = parsed_content
            active_tool_calls = parsed_tool_calls

    if active_content:
        content_chunk = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {"content": active_content},
                "finish_reason": None,
            }]
        }
        yield f"data: {json.dumps(content_chunk, ensure_ascii=False)}\n\n"

    if active_tool_calls:
        for idx, tc in enumerate(active_tool_calls):
            fn_dict = tc.get("function", {}) if isinstance(tc, dict) else {}
            tc_chunk = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
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
            yield f"data: {json.dumps(tc_chunk, ensure_ascii=False)}\n\n"

        final_chunk = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "tool_calls",
            }]
        }
        yield f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n"
    else:
        final_chunk = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }]
        }
        yield f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n"

    # Optional usage chunk
    include_usage = False
    if isinstance(stream_options, dict) and stream_options.get("include_usage"):
        include_usage = True

    if include_usage and usage:
        usage_chunk = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [],
            "usage": usage,
        }
        yield f"data: {json.dumps(usage_chunk, ensure_ascii=False)}\n\n"

    yield "data: [DONE]\n\n"
