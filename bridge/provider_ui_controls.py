"""Read and apply model controls exposed by the provider's signed-in web UI.

This module deliberately uses only visible UI state. It does not invent model
IDs or claim that a requested selection succeeded when the provider page did
not confirm it.
"""

import json
import re
import unicodedata
from urllib.parse import urljoin, urlparse
from typing import Any, Dict, List, Optional

from playwright.async_api import TimeoutError as PlaywrightTimeoutError


class ProviderUiError(RuntimeError):
    pass


_MODEL_BUTTON = re.compile(
    r"(?:select|choose|pick|chọn|mô hình|model|modelo|mod[eè]le|modell|模型|モデル)",
    re.IGNORECASE,
)
_REASONING_CONTROL = re.compile(
    r"(?:reason|think|energy|effort|năng lượng|suy luận|思考)", re.IGNORECASE
)
_REASONING_STATUS = re.compile(r"^(.+?),\s*\d+\s*(?:of|trên)\s*\d+\.?$", re.I)

_PROVIDER_PICKER_NAMES = {
    "claude": r"(?:model|claude|sonnet|opus|haiku)",
    "deepseek": r"(?:model|deepseek|reasoner|chat)",
    "gemini": r"(?:model|gemini|flash|pro)",
    "grok": r"(?:model|grok|auto|expert|think)",
    "copilot": r"(?:model|think deeper|quick response|smart|mode)",
    "perplexity": r"(?:model|sonar|gpt|claude|auto)",
    "kimi": r"(?:model|kimi|moonshot|thinking)",
    "huggingchat": r"(?:model|huggingchat|inference|deepseek|qwen)",
}


def normalize_label(value: str) -> str:
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _is_chatgpt_picker_control_label(value: str) -> bool:
    """Exclude picker headings and reasoning controls from model choices."""
    normalized = normalize_label(value)
    return (
        normalized.startswith(("chonmohinh", "selectchatgptmodel"))
        or normalized in {
            "nangluong", "reasoning", "reasoningeffort", "energy", "suyluan"
        }
    )


# Dola AI chỉ có một mô hình mặc định.
SINGLE_MODEL_PROVIDERS = {"dola": "Dola AI"}


def _deepseek_model_state(reasoner_selected: bool = False) -> Dict[str, Any]:
    return {
        "models": [
            {
                "id": "deepseek-chat",
                "display_name": "DeepSeek-V3",
                "description": "Mô hình V3 chuẩn, phản hồi siêu nhanh và thông minh.",
                "selected": not reasoner_selected,
                "supportsImageInput": False,
                "supported_reasoning_levels": ["none"],
            },
            {
                "id": "deepseek-reasoner",
                "display_name": "DeepSeek-R1 (DeepThink)",
                "description": "Mô hình suy luận sâu R1 qua chế độ DeepThink.",
                "selected": reasoner_selected,
                "supportsImageInput": False,
                "supported_reasoning_levels": ["low", "medium", "high"],
            }
        ],
        "selected_model": "deepseek-reasoner" if reasoner_selected else "deepseek-chat",
        "supported_reasoning_levels": ["none", "low", "medium", "high"],
        "reasoning_effort": "high" if reasoner_selected else "none",
        "reasoning_label": "DeepThink (R1)" if reasoner_selected else None,
    }


def _single_model_state(provider: str) -> Dict[str, Any]:
    name = SINGLE_MODEL_PROVIDERS[provider]
    return {
        "models": [
            {
                "id": name,
                "display_name": name,
                "description": "Mô hình mặc định trên trang web",
                "selected": True,
                "supportsImageInput": False,
            }
        ],
        "selected_model": name,
        "supported_reasoning_levels": [],
        "reasoning_effort": "none",
        "reasoning_label": None,
    }


def _model_picker(page: Any, provider: str) -> Any:
    if provider == "chatgpt":
        return page.get_by_role(
            "button",
            name=re.compile(
                r"^(?:Chọn mô hình ChatGPT|Select ChatGPT model|"
                r"GPT[- ]?\d+(?:\.\d+)?(?:\s+.*)?|ChatGPT(?:\s+.*)?)$",
                re.IGNORECASE,
            ),
        ).first
    if provider == "qwen":
        return page.get_by_role("button", name=re.compile(r"^Select Model$", re.I)).first
    name = _PROVIDER_PICKER_NAMES.get(provider, _MODEL_BUTTON.pattern)
    return page.get_by_role("button", name=re.compile(name, re.I)).first


async def _model_picker_failure_diagnostic(page: Any, provider: str) -> Dict[str, Any]:
    """Capture only UI-control metadata when a picker cannot be located.

    Never read conversation text or message contents into Bridge logs.
    """
    result: Dict[str, Any] = {"provider": provider}
    try:
        url = str(getattr(page, "url", "") or "")
        result["page_kind"] = (
            "conversation" if re.search(r"/c/[a-f0-9-]{20,}", url, re.I)
            else "project" if "/project" in url
            else "other"
        )
        controls = await page.locator(
            "button, [role='button'], [role='option'], [role='menuitem'], "
            "[role='menuitemradio'], [role='combobox'], [aria-haspopup]"
        ).evaluate_all(
            """els => els.filter(e => {
              const style = getComputedStyle(e);
              const r = e.getBoundingClientRect();
              return style.visibility !== 'hidden' && style.display !== 'none' &&
                r.width > 0 && r.height > 0;
            }).map(e => ({
              tag: e.tagName.toLowerCase(),
              role: e.getAttribute('role') || '',
              label: e.getAttribute('aria-label') || '',
              title: e.getAttribute('title') || '',
              testId: e.getAttribute('data-testid') || '',
              disabled: e.disabled === true || e.getAttribute('aria-disabled') === 'true'
            })).slice(0, 60)"""
        )
        result["visible_interactive_controls"] = controls
        composer = page.locator("#prompt-textarea, div[contenteditable='true']").first
        result["composer_visible"] = await composer.is_visible()
        result["composer_controls"] = await composer.evaluate(
            """input => {
              const visible = node => {
                if (!node) return false;
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 0 && rect.height > 0 &&
                  style.visibility !== 'hidden' && style.display !== 'none';
              };
              const form = input.closest('form');
              const root = form || input.parentElement?.parentElement?.parentElement;
              return {
                formFound: !!form,
                controls: Array.from(root?.querySelectorAll(
                  'button, [role="button"], [role="option"], [role="menuitem"], '
                  '[role="menuitemradio"], [role="combobox"], [aria-haspopup], [data-testid*="model"]'
                ) || []).filter(visible).slice(0, 30).map(e => ({
                  tag: e.tagName.toLowerCase(),
                  role: e.getAttribute('role') || '',
                  label: e.getAttribute('aria-label') || '',
                  title: e.getAttribute('title') || '',
                  testId: e.getAttribute('data-testid') || '',
                  disabled: e.disabled === true || e.getAttribute('aria-disabled') === 'true'
                }))
              };
            }"""
        )
        result["visible_model_option_count"] = await page.locator(
            "[role='menuitemradio'], [role='option'], [role='menuitem'], "
            "[role='menu'] button, [role='listbox'] button"
        ).count()
    except Exception as error:
        result["diagnostic_error"] = type(error).__name__
    return result


async def _open_picker(page: Any, provider: str) -> None:
    visible_options = page.locator(
        "[role='menuitemradio'], [role='option'], [role='menuitem'], "
        "[role='menu'] button, [role='listbox'] button"
    )
    for option in await visible_options.all():
        if await option.is_visible():
            return
    picker = _model_picker(page, provider)
    try:
        await picker.wait_for(state="visible", timeout=15000)
    except PlaywrightTimeoutError as error:
        diagnostic = await _model_picker_failure_diagnostic(page, provider)
        print("BRIDGE_MODEL_PICKER_NOT_FOUND " + json.dumps(diagnostic, ensure_ascii=False))
        raise ProviderUiError(
            f"{provider}: model picker not found in the signed-in web UI"
        ) from error
    await picker.click()
    try:
        await page.locator(
            "[role='menuitemradio'], [role='option'], [role='menuitem'], "
            "[role='menu'] button, [role='listbox'] button"
        ).first.wait_for(state="visible", timeout=15000)
    except PlaywrightTimeoutError as error:
        await page.keyboard.press("Escape")
        raise ProviderUiError(
            f"{provider}: model picker opened without selectable model options"
        ) from error


async def _visible_model_options(page: Any, provider: str) -> List[Dict[str, Any]]:
    if provider == "chatgpt":
        selector = (
            "[role='menuitemradio'], [role='option'], [role='menuitem'], "
            "[role='menu'] button, [role='listbox'] button"
        )
    elif provider == "qwen":
        selector = "[role='option']"
    else:
        selector = (
            "[role='menuitemradio'], [role='option'], [role='menuitem'], "
            "[role='menu'] button, [role='listbox'] button"
        )
    options = page.locator(selector)
    rows: List[Dict[str, Any]] = []
    for option in await options.all():
        try:
            if not await option.is_visible():
                continue
            full_text = (await option.inner_text()).strip()
            lines = [line.strip() for line in full_text.splitlines() if line.strip()]
            label = (await option.get_attribute("aria-label") or "").strip()
            name = label or (lines[0] if lines else full_text)
            if not name:
                continue
            if provider == "chatgpt" and _is_chatgpt_picker_control_label(name):
                continue
            checked = await option.get_attribute("aria-checked")
            selected = await option.get_attribute("aria-selected")
            if provider == "chatgpt":
                checked = checked == "true" or selected == "true"
            else:
                checked = checked == "true" or selected == "true"
            rows.append(
                {
                    "id": name,
                    "display_name": name,
                    "description": " ".join(lines[1:])[:400],
                    "selected": checked,
                    "supportsImageInput": _supports_images(provider, name, lines[1:]),
                    "option": option,
                }
            )
        except Exception:
            continue
    if not rows:
        raise ProviderUiError(f"{provider}: web UI exposed no model choices")
    return rows


def _supports_images(provider: str, model: str, description: List[str]) -> bool:
    if provider == "chatgpt":
        # ChatGPT's account model picker contains the current multimodal chat
        # models; it does not expose separate image capability metadata.
        return True
    if provider == "gemini":
        return not re.search(r"embedding|text-embedding", model, re.I)
    if provider == "claude":
        return bool(re.search(r"claude|sonnet|opus|haiku", model, re.I))
    if provider in {"grok", "copilot"}:
        # These web model pickers expose product modes rather than a complete
        # public model identifier; their signed-in chat pages accept images.
        return True
    text = f"{model} {' '.join(description)}".casefold()
    return bool(
        re.search(
            r"image|vision|visual|multimodal|omni|\bvl\b|llava|pixtral|idefics|"
            r"gpt[- ]?4o|claude|gemini|ảnh|hình ảnh|thị giác",
            text,
        )
    )


async def _reasoning_slider(page: Any) -> Optional[Dict[str, Any]]:
    data = await page.locator("[role='slider']").evaluate_all(
        """els => els.map(e => {
          const owner = e.closest('[data-reasoning-slider], [role=menuitem]');
          const label = e.getAttribute('aria-label') || owner?.getAttribute('aria-label') || '';
          const marked = !!e.closest('[data-reasoning-slider]');
          return {label, marked, value:e.getAttribute('aria-valuenow'),
            min:e.getAttribute('aria-valuemin'), max:e.getAttribute('aria-valuemax')};
        })"""
    )
    for row in data:
        if row.get("marked") or _REASONING_CONTROL.search(row.get("label", "")):
            try:
                minimum = int(row["min"])
                maximum = int(row["max"])
                current = int(row["value"])
            except (TypeError, ValueError, KeyError):
                continue
            return {
                "min": minimum,
                "max": maximum,
                "value": current,
                "label": row.get("label", ""),
            }
    # ChatGPT currently exposes one unlabeled slider only while its model menu
    # is open. Do not infer a reasoning control from multiple unrelated sliders.
    if len(data) == 1:
        row = data[0]
        try:
            minimum = int(row["min"])
            maximum = int(row["max"])
            current = int(row["value"])
        except (TypeError, ValueError, KeyError):
            return None
        return {
            "min": minimum,
            "max": maximum,
            "value": current,
            "label": row.get("label", ""),
        }
    return None


async def _reasoning_label(page: Any) -> Optional[str]:
    for text in await page.locator("[role='status']").all_text_contents():
        match = _REASONING_STATUS.match(" ".join(text.split()))
        if match:
            return match.group(1).strip()
    return None


def _supported_efforts(slider: Optional[Dict[str, Any]]) -> List[str]:
    if slider and slider["max"] - slider["min"] == 2:
        return ["low", "medium", "high"]
    return []


async def inspect_provider_ui(page: Any, provider: str) -> Dict[str, Any]:
    """Return the current model list and visible reasoning controls without
    changing the user's selection or sending a message.
    """
    if provider == "deepseek":
        return _deepseek_model_state()
    if provider in SINGLE_MODEL_PROVIDERS:
        return _single_model_state(provider)
    await _open_picker(page, provider)
    try:
        models = await _visible_model_options(page, provider)
        slider = await _reasoning_slider(page)
        current = next((item["id"] for item in models if item["selected"]), None)
        if current is None:
            picker_text = (await _model_picker(page, provider).inner_text()).strip()
            normalized = normalize_label(picker_text)
            current = next(
                (item["id"] for item in models if normalize_label(item["id"]) in normalized),
                None,
            )
        return {
            "models": [
                {key: value for key, value in item.items() if key != "option"}
                for item in models
            ],
            "selected_model": current,
            "supported_reasoning_levels": _supported_efforts(slider),
            "reasoning_effort": _effort_for_slider(slider),
            "reasoning_label": await _reasoning_label(page),
        }
    finally:
        await page.keyboard.press("Escape")


def _effort_for_slider(slider: Optional[Dict[str, Any]]) -> str:
    if not slider or slider["max"] - slider["min"] != 2:
        return "none"
    offset = slider["value"] - slider["min"]
    return {0: "low", 1: "medium", 2: "high"}.get(offset, "none")


def _match_model_choice(requested: str, models: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not models:
        return None
    req_clean = (requested or "").strip().casefold()
    if not req_clean or req_clean in {"auto", "default", "latest", "chatgpt-web-latest"}:
        return next((item for item in models if item.get("selected")), models[0])

    req_norm = normalize_label(req_clean)
    for item in models:
        if normalize_label(item["id"]) == req_norm or item["id"].strip().casefold() == req_clean:
            return item

    # Match by specific version number (e.g., "5.6", "6", "4")
    req_nums = re.findall(r"\d+(?:\.\d+)?", req_clean)
    if req_nums:
        for item in models:
            item_nums = re.findall(r"\d+(?:\.\d+)?", item["id"])
            if any(num in item_nums for num in req_nums):
                return item

    # Match by word tokens (e.g., "sol", "codex", "code")
    req_words = [w for w in re.findall(r"[a-z]+", req_clean) if w not in {"gpt", "model", "chatgpt"}]
    if req_words:
        for item in models:
            item_words = re.findall(r"[a-z]+", item["id"].casefold())
            if any(w in item_words for w in req_words):
                return item

    # Partial substring match
    for item in models:
        norm_item = normalize_label(item["id"])
        if req_norm in norm_item or norm_item in req_norm:
            return item

    return next((item for item in models if item.get("selected")), models[0])


async def _apply_deepseek_ui_settings(
    page: Any,
    model: str,
    reasoning_effort: str = "none",
) -> Dict[str, Any]:
    want_reasoner = (
        "reasoner" in (model or "").lower()
        or "r1" in (model or "").lower()
        or reasoning_effort in ("low", "medium", "high")
    )
    try:
        selectors = [
            "button:has-text('DeepThink')",
            "div[role='button']:has-text('DeepThink')",
            "span:has-text('DeepThink')",
            "button:has-text('深度思考')",
            "div[role='button']:has-text('深度思考')",
            "[class*='deepthink']",
            "[data-testid*='deepthink']",
        ]
        deepthink_btn = None
        for sel in selectors:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                deepthink_btn = loc
                break

        if deepthink_btn:
            is_active = await deepthink_btn.evaluate("""el => {
                const target = el.closest('button, [role="button"]') || el;
                const cls = (target.className || '').toLowerCase();
                const aria = target.getAttribute('aria-pressed') || target.getAttribute('aria-checked') || '';
                return cls.includes('active') || cls.includes('selected') || cls.includes('checked')
                    || aria === 'true' || target.getAttribute('data-active') === 'true';
            }""")
            if want_reasoner and not is_active:
                await deepthink_btn.click()
                await page.wait_for_timeout(300)
            elif not want_reasoner and is_active:
                await deepthink_btn.click()
                await page.wait_for_timeout(300)
    except Exception as e:
        print(f"⚠️ [DEEPSEEK_UI] Không thể điều chỉnh toggle DeepThink: {e}")

    return _deepseek_model_state(reasoner_selected=want_reasoner)


async def apply_provider_ui_settings(
    page: Any,
    provider: str,
    model: str,
    reasoning_effort: str = "none",
) -> Dict[str, Any]:
    """Select one exact model and, when supported, one exact web-UI effort."""
    if reasoning_effort not in {"none", "low", "medium", "high"}:
        raise ProviderUiError(f"{provider}: unsupported reasoning effort {reasoning_effort}")

    if provider == "deepseek":
        return await _apply_deepseek_ui_settings(page, model, reasoning_effort)

    if provider in SINGLE_MODEL_PROVIDERS:
        only_model = SINGLE_MODEL_PROVIDERS[provider]
        accepted = {"", "auto", "default", "latest", "chatgpt-web-latest", only_model.casefold()}
        if model.strip().casefold() not in accepted:
            raise ProviderUiError(f"{provider}: web UI has only one model, {only_model}")
        if reasoning_effort != "none":
            raise ProviderUiError(f"{provider}: web UI has no reasoning slider; use the default effort")
        return _single_model_state(provider)

    await _open_picker(page, provider)
    target = None
    try:
        models = await _visible_model_options(page, provider)
        target = _match_model_choice(model, models)

        picker_text = ""
        try:
            if hasattr(page, "get_by_role"):
                btn = _model_picker(page, provider)
                if hasattr(btn, "inner_text"):
                    picker_text = (await btn.inner_text()).strip()
        except Exception:
            picker_text = ""

        current_norm = normalize_label(picker_text) if picker_text else ""
        should_click = False
        if target:
            if not target.get("selected"):
                should_click = True
            elif current_norm and normalize_label(target["id"]) not in current_norm:
                should_click = True

        if should_click and target and "option" in target:
            if hasattr(target["option"], "click"):
                try:
                    await target["option"].click(force=True, timeout=2000)
                except Exception:
                    pass
                try:
                    if hasattr(target["option"], "evaluate"):
                        await target["option"].evaluate("e => { e.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true })); }")
                except Exception:
                    pass
                await page.wait_for_timeout(500)
    finally:
        try:
            if hasattr(page, "locator"):
                is_open = await page.locator("[role='menuitemradio'], [role='menu']").count() > 0
                if is_open:
                    await page.keyboard.press("Escape")
            else:
                await page.keyboard.press("Escape")
        except Exception:
            pass

    if reasoning_effort and reasoning_effort != "none":
        await _open_picker(page, provider)
        try:
            slider = await _reasoning_slider(page)
            efforts = _supported_efforts(slider)
            if slider and reasoning_effort in efforts:
                target_value = {
                    "low": slider["min"],
                    "medium": slider["min"] + 1,
                    "high": slider["max"],
                }.get(reasoning_effort, slider["value"])
                effort_control = page.locator("[role='slider']").first
                await effort_control.focus()
                current = slider["value"]
                for _ in range(abs(target_value - current) + 1):
                    if current == target_value:
                        break
                    await effort_control.press("ArrowRight" if current < target_value else "ArrowLeft")
                    await page.wait_for_timeout(100)
                    val_str = await page.locator("[role='slider']").first.get_attribute("aria-valuenow")
                    try:
                        current = int(val_str)
                    except (ValueError, TypeError):
                        break
        except Exception:
            pass  # Nếu không chỉnh được thanh trượt thì giữ nguyên mức mặc định của web, không làm đứt request
        finally:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

    state = await inspect_provider_ui(page, provider)
    if target and target.get("id"):
        if not state.get("selected_model"):
            state["selected_model"] = target["id"]
    return state


async def begin_temporary_chat(page: Any) -> bool:
    """Activate a provider's explicit temporary-chat mode when available."""
    button = page.get_by_role(
        "button",
        name=re.compile(
            r"temporary|incognito|private chat|anonymous chat|"
            r"tạm thời|ẩn danh|cuộc trò chuyện riêng tư|chat tempor|chat secreto|临时聊天|臨時聊天",
            re.I,
        ),
    ).first
    if not await button.count() or not await button.is_visible():
        return False
    label = (await button.get_attribute("aria-label") or "").casefold()
    pressed = (await button.get_attribute("aria-pressed") or "").casefold()
    if any(value in label for value in ("turn off", "disable", "exit", "tắt")) or pressed == "true":
        return True
    await button.click()
    await page.wait_for_timeout(250)
    next_label = (await button.get_attribute("aria-label") or "").casefold()
    next_pressed = (await button.get_attribute("aria-pressed") or "").casefold()
    return (
        any(value in next_label for value in ("turn off", "disable", "exit", "tắt"))
        or next_pressed == "true"
        or bool(re.search(r"temporary|incognito", page.url, re.I))
    )


async def delete_current_chat_if_explicit(page: Any) -> bool:
    """Delete only when the current page exposes an explicit delete action.

    The Bridge never guesses which history row to delete. A provider whose UI
    does not expose an unambiguous current-chat delete action is reported as
    retained, rather than risking removal of another conversation.
    """
    delete_name = re.compile(
        r"^(?:(?:delete|remove|xóa|supprimer|eliminar|löschen|删除|削除)"
        r"(?: this| the| current)?(?: chat| conversation| thread| topic|"
        r" cuộc trò chuyện| hội thoại)?|xóa cuộc trò chuyện)$",
        re.I,
    )
    action = page.get_by_role("button", name=delete_name)
    visible = []
    for item in await action.all():
        try:
            if await item.is_visible():
                visible.append(item)
        except Exception:
            pass
    if len(visible) == 1:
        await visible[0].click()
        if await _confirm_delete_if_shown(page):
            return True

    # Some web apps put Delete under the current conversation's own history-row
    # menu. Only open that menu if its anchor exactly matches this URL and the
    # menu trigger is unique inside that row.
    current_url = urlparse(page.url)
    current_path = current_url.path.rstrip("/")
    segments = [part for part in current_path.split("/") if part]
    if len(segments) < 2 or segments[-1].casefold() in {"new", "chat", "app"}:
        return False
    matches = []
    for anchor in await page.locator("a[href]").all():
        try:
            if not await anchor.is_visible():
                continue
            href = await anchor.get_attribute("href") or ""
            candidate_url = urlparse(urljoin(page.url, href))
            if (
                candidate_url.path.rstrip("/") == current_path
                and candidate_url.query == current_url.query
            ):
                matches.append(anchor)
        except Exception:
            continue
    if len(matches) != 1:
        return False
    anchor = matches[0]
    row = None
    for depth in range(1, 7):
        ancestor = anchor.locator(f"xpath=ancestor::*[{depth}]")
        triggers = ancestor.locator(
            "button[aria-label*='more' i], button[aria-label*='option' i], "
            "button[aria-label*='menu' i]"
        )
        if await triggers.count() == 1 and await triggers.first.is_visible():
            row = ancestor
            break
    if row is None:
        return False
    await row.locator(
        "button[aria-label*='more' i], button[aria-label*='option' i], "
        "button[aria-label*='menu' i]"
    ).first.click()
    delete_item = page.get_by_role(
        "menuitem",
        name=re.compile(
            r"^(?:delete|remove|xóa|supprimer|eliminar|löschen|删除|削除)"
            r"(?: chat| conversation| thread| topic| cuộc trò chuyện)?$",
            re.I,
        ),
    )
    if await delete_item.count() != 1 or not await delete_item.first.is_visible():
        await page.keyboard.press("Escape")
        return False
    await delete_item.first.click()
    return await _confirm_delete_if_shown(page)


async def _confirm_delete_if_shown(page: Any) -> bool:
    dialog = page.get_by_role("dialog").last
    try:
        await dialog.wait_for(state="visible", timeout=1500)
    except PlaywrightTimeoutError:
        # Some providers delete immediately; avoid claiming success without a
        # visible route change away from a conversation URL.
        await page.wait_for_timeout(400)
        return not bool(re.search(r"/c/|/chat/|/thread/|/conversation/", page.url))
    confirm = dialog.get_by_role(
        "button", name=re.compile(r"^(?:delete|remove|xóa|supprimer|eliminar|löschen|删除|削除)$", re.I)
    )
    if await confirm.count() != 1:
        await page.keyboard.press("Escape")
        return False
    await confirm.click()
    try:
        await dialog.wait_for(state="hidden", timeout=5000)
    except PlaywrightTimeoutError:
        return False
    return True

