"""智能过滤插件的控制台后端。

前端页面在 pages/guard/index.html，通过 AstrBot 插件页桥接调用这里的接口。
所有接口都挂在 /<插件名>/console/* 下，写入前统一做白名单与范围校验，
避免前端传什么就往配置文件里塞什么。
"""

from __future__ import annotations

import asyncio
import html
import inspect
import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.api.web import error_response, json_response, request
try:
    from .guard_media import image_target
    from .guard_expression_help import HELP as EXPRESSION_HELP
    from .guard_understanding import audio_capable
    from .guard_native_reply import NativeReply, NativeReplyError
    from .guard_native_settings import NativeSettings
    from .guard_image_settings import ImageSettings
    from .guard_flow import build_flow
except ImportError:
    from guard_media import image_target
    from guard_expression_help import HELP as EXPRESSION_HELP
    from guard_understanding import audio_capable
    from guard_native_reply import NativeReply, NativeReplyError
    from guard_native_settings import NativeSettings
    from guard_image_settings import ImageSettings
    from guard_flow import build_flow

PLUGIN_NAME = "astrbot_plugin_at_guard"
SCHEMA_PATH = Path(__file__).resolve().parent / "_conf_schema.json"

# key -> (类型, 下限, 上限)。字符串型没有范围，单独处理。
NUMERIC_FIELDS: dict[str, tuple[str, float, float]] = {
    "participation_confidence_threshold": ("int", 0, 100),
    "classifier_timeout_seconds": ("float", 0.5, 8.0),
    "vision_timeout_seconds": ("float", 0.5, 12),
    "gate_image_cache_ttl_seconds": ("int", 0, 1800),
    "gate_main_fallback_timeout_seconds": ("float", 0.5, 12),
    "gate_total_timeout_seconds": ("float", 1, 25),
    "gate_vision_hourly_budget": ("int", 0, 120),
    "gate_min_length": ("int", 0, 20),
    "gate_hourly_budget": ("int", 0, 600),
    "gate_context_messages": ("int", 0, 30),
    "gate_context_max_age_seconds": ("float", 0, 3600),
    "gate_reply_max_age_seconds": ("float", 5, 120),
    "gate_memory_timeout_seconds": ("float", 0.2, 5),
    "participation_context_messages": ("int", 2, 30),
    "fragment_wait_seconds": ("float", 0, 5.0),
    "proactive_cooldown_seconds": ("float", 0, 60.0),
    "duplicate_window_seconds": ("int", 0, 3600),
}
BOOL_FIELDS = {
    "apply_to_private", "gate_main_fallback_enabled", "gate_congling_prepare_compat",
    "fragment_wait_private", "reply_quote_enabled", "reply_mention_enabled",
}
STRING_FIELDS = {
    "gate_mode",
    "classifier_provider_id",
    "vision_provider_id",
    "empty_mention_mode",
    "empty_mention_reply",
    "empty_mention_persona_prompt",
}
LIST_FIELDS = {"bot_names"}
# 新表达/理解配置直接从宿主schema派生白名单和数值范围，避免UI与后端漏项。
_expression_schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8-sig"))
for _key, _field in _expression_schema.items():
    if not _key.startswith(("expression_", "understand_", "reply_typing_", "reply_recall_")):
        continue
    _type = _field["type"]
    if _type in {"int", "float"}:
        _bounds = _field["slider"]
        NUMERIC_FIELDS[_key] = (_type, _bounds["min"], _bounds["max"])
    elif _type == "bool":
        BOOL_FIELDS.add(_key)
    elif _type == "string":
        STRING_FIELDS.add(_key)
EDITABLE = set(NUMERIC_FIELDS) | BOOL_FIELDS | STRING_FIELDS | LIST_FIELDS
POLICY_FIELDS = {
    "gate_mode", "classifier_provider_id", "classifier_timeout_seconds",
    "participation_confidence_threshold", "gate_min_length",
    "vision_provider_id", "gate_main_fallback_enabled", "gate_main_fallback_timeout_seconds",
    "vision_timeout_seconds", "gate_image_cache_ttl_seconds",
    "gate_context_messages", "gate_context_max_age_seconds",
    "gate_memory_timeout_seconds", "participation_context_messages",
}

MODE_LABELS = {
    "enforce": "硬闸门（判完就拦，低分不生成）",
    "shadow": "观察模式（同步判断并记录，不执行参与拦截）",
}

Handler = Callable[[Any], Awaitable[Any]]


def _response(data: Any = None, message: str | None = None):
    payload: dict[str, Any] = {"status": "ok"}
    if data is not None:
        payload["data"] = data
    if message:
        payload["message"] = message
    return json_response(payload)


def _failure(message: str, status_code: int = 400):
    return error_response(message, status_code=status_code)


def _bind(plugin: Any, handler: Handler):
    async def bound_handler():
        return await handler(plugin)

    bound_handler.__name__ = f"at_guard_{handler.__name__}"
    return bound_handler


def _current(plugin: Any, key: str, default: Any = None) -> Any:
    try:
        value = plugin.config.get(key, default)
    except Exception:
        return default
    return default if value is None else value


async def _providers(plugin: Any) -> list[dict[str, Any]]:
    """列出可选的判定模型。

    除了 AstrBot 原生 provider，插件的 provider（例如「多 API 模型路由」）
    也会出现在 get_all_providers() 里。路由内部是"一个 provider、多个上游、
    按优先级自动降级"。为了让用户能锁定路由下的某个具体上游模型（而不是只能
    选「路由」这个笼统名字），这里把路由的每个上游展开成一个独立可选项，
    id 用 ``provider::model`` 编码，选中即锁定该具体模型。

    三类项：
    - 路由本身（``id=provider_id``）：走优先级自动降级；
    - 已注册路由上游（``id=provider_id:upstream_N``）：锁定具体API；
      旧版其它路由仍兼容``provider_id::model``虚拟项；
    - 原生 provider（``id=provider_id``）：选它就是该 provider 自家模型。
    """
    try:
        from .guard_router_connections import router_connections, CHAT_TYPES
    except ImportError:
        from guard_router_connections import router_connections, CHAT_TYPES
    connections = router_connections(plugin.context)
    items: dict[str, dict[str, Any]] = {}
    try:
        for provider in plugin.context.get_all_providers():
            config = getattr(provider, "provider_config", {}) or {}
            provider_id = str(config.get("id", "") or "")
            if not provider_id:
                continue
            if config.get("enable") is False:
                continue
            connection = connections.get(provider_id)
            if connection and connection["api_type"] not in CHAT_TYPES:
                continue  # 某些路由版本会注册生图占位Provider，不能供主对话或判定调用。
            provider_type = str(config.get("type", "") or "")
            display_name = str(
                config.get("display_name")
                or config.get("label")
                or config.get("name")
                or ""
            ).strip()
            try:
                pinned_slot = int(config.get("upstream_slot") or 0)
            except (TypeError, ValueError):
                pinned_slot = 0
            parent_provider_id = str(
                config.get("parent_provider_id") or ""
            ).strip()
            model = ""
            try:
                model = str(provider.get_model() or "")
            except Exception:
                model = str(config.get("model", "") or "")

            kind = "provider"
            names: list[str] = []
            # 路由上游的 (label, model) 对，用于生成「锁定具体模型」的虚拟项。
            upstream_pairs: list[tuple[str, str]] = []
            raw_upstreams = getattr(provider, "upstreams", None)
            if isinstance(raw_upstreams, (list, tuple)) and raw_upstreams:
                kind = "router_upstream" if pinned_slot else "router"
                for item in raw_upstreams:
                    label = str(getattr(item, "name", "") or "").strip()
                    upstream_model = ""
                    client = getattr(item, "client", None)
                    if client is not None:
                        getter = getattr(client, "get_model", None)
                        if callable(getter):
                            try:
                                upstream_model = str(getter() or "").strip()
                            except Exception:
                                upstream_model = ""
                    if label and upstream_model and label != upstream_model:
                        names.append(f"{label} → {upstream_model}")
                    elif label or upstream_model:
                        names.append(label or upstream_model)
                    # multi_api_router 已注册真实的指定上游 Provider，不再生成
                    # provider::model 虚拟项，避免选择器出现重复和内部前缀。
                    if upstream_model and provider_type != "multi_api_router":
                        upstream_pairs.append((label, upstream_model))

            if not names:
                get_models = getattr(provider, "get_models", None)
                if callable(get_models):
                    try:
                        result = get_models()
                        if inspect.isawaitable(result):
                            result = await result
                        if isinstance(result, (list, tuple)):
                            names = [
                                str(item).strip()
                                for item in result
                                if str(item).strip()
                            ]
                    except Exception:
                        names = []

            entry = items.get(provider_id)
            try:
                image_target(provider, provider_id)
                vision_supported = True
            except Exception:
                vision_supported = False
            if entry is None:
                items[provider_id] = {
                    "id": provider_id,
                    "model": model,
                    "kind": kind,
                    "display": display_name,
                    "upstreams": names,
                    "instances": 1,
                    "supports_images": vision_supported,
                }
                if kind == "router_upstream" and parent_provider_id:
                    items[provider_id]["parent"] = parent_provider_id
            else:
                entry["instances"] += 1
                entry["supports_images"] = entry.get("supports_images", False) or vision_supported
                if kind == "router":
                    entry["kind"] = "router"
                for name in names:
                    if name not in entry["upstreams"]:
                        entry["upstreams"].append(name)

            # 路由上游：每个具体模型追加一个「锁定」虚拟项。
            for label, upstream_model in upstream_pairs:
                pinned_id = f"{provider_id}::{upstream_model}"
                if pinned_id in items:
                    continue
                display = f"{label} → {upstream_model}" if label and label != upstream_model else upstream_model
                try:
                    image_target(provider, pinned_id)
                    pinned_vision = True
                except Exception:
                    pinned_vision = False
                items[pinned_id] = {
                    "id": pinned_id,
                    "model": upstream_model,
                    "kind": "router_upstream",
                    "upstreams": [],
                    "instances": 1,
                    "parent": provider_id,
                    "display": display,
                    "supports_images": pinned_vision,
                }
    except Exception as exc:
        logger.debug(f"[智能过滤] 读取模型列表失败: {exc!r}")
    for item in items.values():
        item["role"] = "chat"
        try:
            provider = plugin.context.get_provider_by_id(item.get("parent", item["id"]))
            if getattr(provider, "upstreams", None):
                item["supports_audio"] = any(
                    getattr(u, "client", None) is not None and audio_capable(u.client)
                    and (item["kind"] != "router_upstream" or u.client.get_model() == item["model"])
                    for u in provider.upstreams)
            else:
                item["supports_audio"] = audio_capable(provider)
        except Exception:
            item["supports_audio"] = False
    for role in ("stt", "tts"):
        getter = getattr(plugin.context, "get_all_" + role + "_providers", None)
        for provider in getter() if callable(getter) else []:
            config = getattr(provider, "provider_config", {}) or {}
            provider_id = str(config.get("id", "") or "")
            if provider_id:
                display = str(config.get("display_name") or config.get("label")
                              or config.get("name") or "").strip()
                items[provider_id] = {"id": provider_id, "model": str(
                    getattr(provider, "get_model", lambda: "")() or ""),
                    "kind": "provider", "role": role, "display": display,
                    "upstreams": [], "instances": 1,
                    "supports_images": False, "supports_audio": role == "stt"}
    for pid, connection in connections.items():
        if pid in items and connection["api_type"] in CHAT_TYPES:
            items[pid]["display"] = connection["display"]
            items[pid]["model"] = connection["model"]
            items[pid]["api_type"] = connection["api_type"]
    return sorted(items.values(), key=lambda item: item["id"])


_CONGLING_PERCEIVED_MARKER = re.compile(
    r"^\s*<!--congling:perceived:[^>]*-->\s*", re.IGNORECASE)
_CONGLING_PERCEIVED_TITLE = "以下是这个会话里最近的几条消息"
_CONGLING_PERCEIVED_END = "【补充结束】"
_OMISSION_LINE = re.compile(r"^\s*(?:[.…·]{2,}|…)\s*[（(]?.*略.*[）)]?\s*$")
_MESSAGE_AGE = re.compile(r"^\[(\d+(?:\.\d+)?)秒前\]\s*")
_MESSAGE_SENDER = re.compile(r"^([^:：\n]{1,80})\s*[:：]\s*(.+)$", re.DOTALL)


def _display_text(value: Any, limit: int = 600) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()[:limit]


def _display_message(value: Any, source: str) -> dict[str, Any]:
    """把历史记录的对象/字符串转为只读展示对象，不回写原日志。"""
    if isinstance(value, dict):
        message = dict(value)
        message["text"] = _display_text(message.get("text"))
        if message.get("sender_name") is not None:
            message["sender_name"] = _display_text(message["sender_name"], 80)
        message["source"] = source
        return message

    raw = html.unescape(str(value or "")).strip()
    age_match = _MESSAGE_AGE.match(raw)
    if age_match:
        raw = raw[age_match.end():].strip()
    message: dict[str, Any] = {
        "text": _display_text(raw),
        "sender_name": "",
        "source": source,
    }
    if age_match:
        message["age_seconds"] = float(age_match.group(1))
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("text"), str):
            message.update(parsed)
            message["text"] = _display_text(parsed["text"])
            message["source"] = source
            return message
    if raw.startswith("[我]:"):
        message["sender_name"] = "机器人"
        message["text"] = _display_text(raw[4:])
        return message
    sender = _MESSAGE_SENDER.match(raw)
    if sender:
        message["sender_name"] = _display_text(sender.group(1), 80)
        message["text"] = _display_text(sender.group(2))
    return message


def _message_key(message: dict[str, Any]) -> tuple[str, str]:
    sender = _display_text(
        message.get("sender_name") or message.get("sender_id"), 80).casefold()
    return sender, _display_text(message.get("text")).casefold()


def _congling_perceived(block: Any) -> tuple[list[dict[str, Any]], bool]:
    """只解析从绫稳定的感知块；格式未知时返回原块给背景资料展示。"""
    if not isinstance(block, str) or not _CONGLING_PERCEIVED_MARKER.match(block):
        return [], False
    lines = block.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    title_at = next(
        (index for index, line in enumerate(lines)
         if _CONGLING_PERCEIVED_TITLE in line),
        None,
    )
    if title_at is None:
        return [], False

    messages: list[dict[str, Any]] = []
    for line in lines[title_at + 1:]:
        text = html.unescape(line).strip()
        if text == _CONGLING_PERCEIVED_END:
            break
        if not text:
            continue
        if _OMISSION_LINE.match(text):
            messages.append({
                "text": _display_text(text),
                "sender_name": "",
                "source": "congling_perceived",
                "is_omission": True,
            })
            continue
        messages.append(_display_message(text, "congling_perceived"))
    # 已识别但没有任何可展示行时保留原块，避免静默丢失格式变化。
    return messages, bool(messages)


def _display_chat_data(chat_data: Any) -> Any:
    if not isinstance(chat_data, dict):
        return chat_data
    chat = dict(chat_data)
    reference: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(message: dict[str, Any]) -> None:
        if len(reference) >= 30:
            return
        if message.get("is_omission"):
            reference.append(message)
            return
        key = _message_key(message)
        if key in seen:
            return
        seen.add(key)
        reference.append(message)

    local = chat.get("recent_messages_old_to_new")
    if isinstance(local, list):
        for item in local:
            add(_display_message(item, "livinghello"))

    background: list[Any] = []
    scoped_memory = chat.get("scoped_memory")
    if isinstance(scoped_memory, list):
        for block in scoped_memory:
            perceived, parsed = _congling_perceived(block)
            if not parsed:
                background.append(block)
                continue
            for message in perceived:
                add(message)
    elif scoped_memory is not None:
        background.append(scoped_memory)

    chat["reference_messages_old_to_new"] = reference
    chat["reference_count"] = sum(
        1 for message in reference if not message.get("is_omission"))
    chat["background_memory"] = background
    return chat


async def _decision_rows(plugin: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched = await plugin._trace().enrich(rows)
    result = []
    for row in enriched:
        display_row = dict(row)
        display_row["chat_data"] = _display_chat_data(row.get("chat_data"))
        result.append(display_row)
    return result


async def _state(plugin: Any) -> dict[str, Any]:
    engine = plugin.engine
    participation = getattr(plugin, "participation_stats", {}) or {}
    schema = _load_schema_subset()
    return {
        "version": "2.7.0",
        "mode": engine.mode(),
        "modeLabels": MODE_LABELS,
        "config": {key: _current(plugin, key, schema.get(key, {}).get("default")) for key in EDITABLE},
        "schema": schema,
        "fieldHelp": {key: {"desc": values[0], "examples": list(values[1:])}
                      for key, values in EXPRESSION_HELP.items()},
        "today": engine.snapshot(),
        "lifetime": {
            "total": int(participation.get("total", 0) or 0),
            "allowed": int(participation.get("allowed", 0) or 0),
            "blocked": int(participation.get("blocked", 0) or 0),
            "average": float(participation.get("average", 0.0) or 0.0),
        },
        "budget": engine.budget_usage(),
        "image_cache": plugin._image_observation_cache().snapshot(),
        "congling_compat": _congling_compat(plugin),
        "providers": await _providers(plugin),
        "distribution": engine.score_distribution(engine.provider_label()),
        # 概览只给出最近10条；完整排查仍通过搜索接口覆盖内存中的600条。
        "decisions": await _decision_rows(plugin, engine.log.recent(10)),
        "listeners": plugin._trace().listeners(),
    }


def _congling_compat(plugin: Any) -> dict[str, Any]:
    """只检查已加载插件元数据，不导入或修改从绫，也不凭版本号猜测事件计时支持。"""
    result = {
        "detected": False,
        "active": False,
        "order": "unknown",
        "prepare_accounting": _current(plugin, "gate_congling_prepare_compat", True) is True,
    }
    try:
        metadata = plugin.context.get_registered_star("astrbot_plugin_congling")
        if metadata is None:
            metadata = plugin.context.get_registered_star("congling")
        if metadata is None:
            return result
        result["detected"] = True
        result["active"] = bool(getattr(metadata, "activated", False))
        result["version"] = str(getattr(metadata, "version", "") or "")
        instance = getattr(metadata, "star_cls", None)
        module = sys.modules.get(type(instance).__module__) if instance is not None else None
        priority = getattr(module, "PRIO_PERSONA", None)
        if isinstance(priority, int) and not isinstance(priority, bool):
            result["persona_priority"] = priority
            result["order"] = "after_guard" if priority < 10000 else "before_or_equal_guard"
    except Exception:
        pass
    return result


def _load_schema_subset() -> dict[str, Any]:
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    allowed = EDITABLE | {"participation_confidence_threshold"}
    return {key: value for key, value in schema.items() if key in allowed}


def _coerce(key: str, value: Any) -> Any:
    if key in LIST_FIELDS:
        if isinstance(value, (list, tuple)):
            parts = [str(item) for item in value]
        else:
            parts = re.split(r"[,，、\s]+", str(value or ""))
        names = [part.strip() for part in parts if part and part.strip()]
        if not names:
            raise ValueError("机器人称呼不能为空")
        if len(names) > 8:
            raise ValueError("机器人称呼最多 8 个")
        return names[:8]
    if key in BOOL_FIELDS:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if key in STRING_FIELDS:
        text = str(value or "").strip()
        if key == "gate_mode" and text not in {"enforce", "shadow"}:
            raise ValueError(f"未知模式: {text}")
        if key == "empty_mention_mode" and text not in {"fixed", "persona"}:
            raise ValueError(f"未知的 @ 回复方式: {text}")
        if key == "empty_mention_reply":
            if not text:
                raise ValueError("固定文案不能为空")
            return text[:200]
        if key == "empty_mention_persona_prompt":
            if not text:
                raise ValueError("人格回复要求不能为空")
            return text[:2000]
        return text
    kind, low, high = NUMERIC_FIELDS[key]
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} 需要数字") from exc
    if not math.isfinite(number) or number < low or number > high:
        raise ValueError(f"{key} 超出范围 {low}~{high}")
    if key == "gate_context_max_age_seconds" and 0 < number < 30:
        raise ValueError("上下文有效期应为0（不限时间）或30~3600秒")
    return int(round(number)) if kind == "int" else round(number, 2)


async def handle_state(plugin: Any):
    try:
        return _response(await _state(plugin))
    except Exception as exc:
        logger.exception(f"[智能过滤] 控制台状态读取失败: {exc}")
        return _failure(str(exc), 500)


async def handle_config(plugin: Any):
    try:
        body = await request.json(default={})
    except Exception:
        return _failure("请求体必须是 JSON")
    if not isinstance(body, dict) or not body:
        return _failure("没有要保存的字段")

    updates: dict[str, Any] = {}
    for key, value in body.items():
        if key not in EDITABLE:
            return _failure(f"不允许修改的配置项: {key}")
        try:
            updates[key] = _coerce(key, value)
        except ValueError as exc:
            return _failure(str(exc))

    providers = await _providers(plugin)
    known = {item["id"] for item in providers}
    for key in ("classifier_provider_id", "vision_provider_id", "understand_image_provider_id",
                "understand_voice_provider_id"):
        provider_id = updates.get(key)
        if provider_id and provider_id not in known:
            return _failure(f"模型 {provider_id} 不在可用列表中")
        if key in {"vision_provider_id", "understand_image_provider_id"} and provider_id and not any(
            item["id"] == provider_id and item.get("supports_images") for item in providers
        ):
            return _failure(f"模型 {provider_id} 无法确认支持图片，请选择视觉模型")
        if key == "understand_voice_provider_id" and provider_id and not any(
                item["id"] == provider_id and item.get("supports_audio") for item in providers):
            return _failure("语音理解需要原生STT或支持音频输入的模型")

    missing = object()
    before = {key: plugin.config.get(key, missing) for key in updates}
    changed = [key for key, value in updates.items() if before[key] != value]
    try:
        for key, value in updates.items():
            plugin.config[key] = value
        plugin.config.save_config()
    except Exception as exc:
        # 保存失败恢复运行中配置，不让“保存失败”实际改变判定逻辑。
        for key, value in before.items():
            if value is missing:
                plugin.config.pop(key, None)
            else:
                plugin.config[key] = value
        logger.exception(f"[智能过滤] 保存配置失败: {exc}")
        return _failure(f"配置未落盘: {exc}", 500)

    logger.info(f"[智能过滤] 控制台更新 {len(updates)} 项: {sorted(updates)}")
    data = await _state(plugin)
    data["save_notice"] = ("判定口径已变化，旧统计与运行记录仍保留，分析时请结合修改时间。"
                           if POLICY_FIELDS.intersection(changed)
                           else "运行统计与判定记录已保留。")
    return _response(data, f"已保存 {len(changed)} 项")


async def handle_decisions(plugin: Any):
    # AstrBot插件请求使用query；args仅兼容旧宿主，不能假定当前框架提供它。
    params = getattr(request, "query", None)
    if params is None:
        params = getattr(request, "args", {})
    query = str(params.get("query", "") or "")[:120]
    category = str(params.get("category", "") or "")
    if category not in {"", "private", "group", "public"}:
        return _failure("判定分类无效", 400)
    return _response({
        "decisions": await _decision_rows(
            plugin, plugin.engine.log.recent(100, query=query, category=category)),
        "category": category,
        "query": query, "retained": len(plugin.engine.log.ring), "limit": 100,
    })


async def handle_request_trace(plugin):
    try:
        from .guard_request_trace import ID_RE
    except ImportError:
        from guard_request_trace import ID_RE
    params = getattr(request, "query", None)
    if params is None:
        params = getattr(request, "args", {})
    rid = str(params.get("request_id", "") or "")
    if not ID_RE.fullmatch(rid):
        return _failure("request_id须为32位小写十六进制ID", 400)
    service = plugin._trace()
    data = await service.get(rid)
    if data is None:
        return _failure("追踪存储不可用" if service.health["storage"] == "unavailable"
                        else "未找到此请求追踪（可能是旧记录或已超过保留范围）",
                        503 if service.health["storage"] == "unavailable" else 404)
    return _response(data)


async def handle_listeners(plugin):
    return _response(plugin._trace().listeners())


def _native(plugin):
    if not hasattr(plugin, "_native_reply"):
        plugin._native_reply = NativeReply(plugin.context)
    return plugin._native_reply


def _native_settings(plugin):
    if not hasattr(plugin, "_native_settings"):
        plugin._native_settings = NativeSettings(plugin, _native(plugin))
    return plugin._native_settings


def _image_settings(plugin):
    # 与工具共用同一独立能力实例；不再读取或写入 BiliBot。
    return plugin._image_generation()


async def handle_native_settings(plugin):
    try:
        data = _native_settings(plugin).read(request.query.get("config_id", "default"))
        data["providers"] = [p for p in await _providers(plugin)
                             if p.get("role") in {"chat", "tts"} and "::" not in p["id"]]
        return _response(data)
    except NativeReplyError as exc:
        return _failure(str(exc), exc.status)


async def handle_native_profile(plugin):
    try:
        return _response(_native_settings(plugin).selected_profile())
    except NativeReplyError as exc:
        return _failure(str(exc), exc.status)


async def handle_save_native_profile(plugin):
    try:
        return _response(await _native_settings(plugin).select_profile(await request.json(default={})))
    except NativeReplyError as exc:
        return _failure(str(exc), exc.status)


async def handle_save_native_settings(plugin):
    try:
        return _response(await _native_settings(plugin).save(
            request, await request.json(default={}), await _providers(plugin)))
    except NativeReplyError as exc:
        return _failure(str(exc), exc.status)


async def handle_image_settings(plugin):
    return _response(_image_settings(plugin).read())


async def handle_save_image_settings(plugin):
    try:
        return _response(await _image_settings(plugin).save(await request.json(default={})))
    except NativeReplyError as exc:
        return _failure(str(exc), exc.status)


async def handle_flow(plugin):
    try:
        native = _native_settings(plugin).read(request.query.get("config_id", "default"))
        return _response(build_flow(plugin.config, native))
    except NativeReplyError as exc:
        return _failure(str(exc), exc.status)


async def handle_native_segments(plugin):
    try:
        return _response(_native(plugin).read(request.query.get("config_id", "default")))
    except NativeReplyError as exc:
        return _failure(str(exc), exc.status)


async def handle_save_native_segments(plugin):
    try:
        return _response(await _native(plugin).save(request, await request.json(default={})))
    except NativeReplyError as exc:
        return _failure(str(exc), exc.status)


async def handle_reset(plugin: Any):
    try:
        plugin.engine.reset_stats()
        plugin.participation_stats = plugin._empty_participation_stats()
        path = getattr(plugin, "participation_stats_path", None)
        if path is not None:
            path.write_text(
                json.dumps(plugin.participation_stats, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return _response(await _state(plugin), "统计已清零")
    except Exception as exc:
        logger.exception(f"[智能过滤] 重置统计失败: {exc}")
        return _failure(str(exc), 500)


async def handle_probe(plugin: Any):
    try:
        body = await request.json(default={})
    except Exception:
        return _failure("请求体必须是 JSON")
    if not isinstance(body, dict):
        return _failure("请求体必须是JSON对象")
    text = str(body.get("text", "") or "").strip()
    if not text:
        return _failure("请输入要试跑的消息")
    bot_line = str((body or {}).get("bot_line", "") or "").strip()
    is_private = bool((body or {}).get("private", False))
    if len(text) > 800 or len(bot_line) > 2000:
        return _failure("待判定消息最多800字，上一句最多2000字")
    context_lines = body.get("context_lines", [])
    if (not isinstance(context_lines, list) or len(context_lines) > 30 or
            any(not isinstance(line, str) or len(line) > 800 for line in context_lines)):
        return _failure("上下文最多30条，每条最多800字")
    try:
        native = _native_settings(plugin).read(body.get("config_id", "default"))
    except NativeReplyError as exc:
        return _failure(str(exc), exc.status)

    async def resolve_provider():
        provider_id = plugin.engine.provider_id()
        if provider_id:
            provider = plugin.context.get_provider_by_id(provider_id)
            if provider is not None:
                return provider
        provider_id = native["values"].get("provider_id")
        if provider_id:
            return plugin.context.get_provider_by_id(provider_id)
        return await plugin.context.get_using_provider_async(None)

    try:
        decision = await plugin.engine.decide(
            group_key="#probe",
            current_text=text,
            context_lines=context_lines,
            bot_line=bot_line,
            is_private=is_private,
            resolve_provider=resolve_provider,
            dry_run=True,
        )
    except Exception as exc:
        logger.exception(f"[智能过滤] 试跑失败: {exc}")
        return _failure(f"试跑失败: {exc}", 500)

    return _response(
        {
            "decision": decision.as_dict(),
            "raw": decision.raw,
            "checkedAt": int(time.time()),
            "flow": build_flow(plugin.config, native, decision, {
                "消息": text, "上一句": bot_line, "上下文": context_lines,
                "场景": "私聊" if is_private else "群聊", "原生配置": native["config_id"]}),
        }
    )


def register_webui(plugin: Any, context: Context) -> None:
    routes: list[tuple[str, str, Handler, str]] = [
        ("console/state", "GET", handle_state, "智能过滤控制台状态"),
        ("console/decisions", "GET", handle_decisions, "搜索群ID、私聊QQ及判定内容"),
        ("console/request-trace", "GET", handle_request_trace, "读取单次请求的监听与执行追踪"),
        ("console/listeners", "GET", handle_listeners, "核查当前实例激活监听与采集健康"),
        ("console/config", "POST", handle_config, "保存智能过滤参数"),
        ("console/native-segments", "GET", handle_native_segments, "读取AstrBot原生分段配置"),
        ("console/native-segments", "POST", handle_save_native_segments, "同步AstrBot原生分段配置"),
        ("console/native-settings", "GET", handle_native_settings, "读取原生模型、人格与语音配置"),
        ("console/native-profile", "GET", handle_native_profile, "读取控制台选中的原生配置"),
        ("console/native-profile", "POST", handle_save_native_profile, "保存控制台选中的原生配置"),
        ("console/native-settings", "POST", handle_save_native_settings, "保存原生模型、固定人格与语音配置"),
        ("console/image-settings", "GET", handle_image_settings, "读取独立AI生图配置与每日额度"),
        ("console/image-settings", "POST", handle_save_image_settings, "保存独立AI生图设置"),
        ("console/flow", "GET", handle_flow, "读取判定与生成流程"),
        ("console/reset", "POST", handle_reset, "清零智能过滤统计"),
        ("console/probe", "POST", handle_probe, "试跑一次参与判定"),
    ]
    for endpoint, method, handler, description in routes:
        route = f"/{PLUGIN_NAME}/{endpoint}"
        try:
            context.register_web_api(
                route, _bind(plugin, handler), [method], description
            )
            logger.info(f"[智能过滤] 已注册 {method} {route}")
        except Exception as exc:
            logger.error(f"[智能过滤] 注册 {method} {route} 失败: {exc}")

    # 把可用的判定模型动态写进插件的内存 schema，让 AstrBot 配置页的
    #「判定用的小模型」从文本框变成真正的下拉框。宿主配置页读的是
    # plugin_md.config.schema（加载时读进内存），所以改这里即可，无需重启。
    try:
        asyncio.get_running_loop().create_task(_refresh_provider_schema(plugin))
    except RuntimeError:
        # initialize() 一定在事件循环内，走到这里属异常兜底，忽略即可。
        pass


async def _refresh_provider_schema(plugin: Any) -> None:
    """把 provider 列表同步进插件 schema 的 classifier_provider_id.options。

    配置页里「判定用的小模型」这个字段此前是 string 文本框，因为 options 是空的。
    这里在插件加载后异步填充 options，宿主前端就会把它渲染成下拉框。

    注意插件加载顺序：本插件的 initialize() 可能跑在原生 provider（deepseek 等）
    注册之前，第一次拉到的列表只有插件 provider（如 multi_api_router）。所以这里
    带重试——只要列表还在变少/没补齐，就隔几秒再拉一次，直到稳定或放弃。
    """
    last_options: list[str] = []
    for attempt in range(6):
        try:
            providers = await _providers(plugin)
        except Exception as exc:
            logger.warning(f"[智能过滤] 拉取判定模型列表失败: {exc!r}")
            return
        options = [item["id"] for item in providers]
        if not options:
            return
        if options == last_options:
            break  # 列表已稳定，无需再等
        last_options = options
        if attempt < 5:
            await asyncio.sleep(3.0)
    _write_schema_options(plugin, last_options,
                          [item["id"] for item in providers if item.get("supports_images")])


def _write_schema_options(plugin: Any, options: list[str], vision_options: list[str] | None = None) -> None:
    try:
        metadata = plugin.context.get_registered_star(PLUGIN_NAME)
        config = getattr(metadata, "config", None)
        schema = getattr(config, "schema", None)
        if not isinstance(schema, dict):
            logger.warning("[智能过滤] 未找到插件配置 schema，判定模型下拉未注入")
            return
        for key in ("classifier_provider_id", "vision_provider_id"):
            field = schema.get(key)
            if isinstance(field, dict):
                field["options"] = [""] + (options if key == "classifier_provider_id" else (vision_options or []))
        logger.info(
            f"[智能过滤] 已把 {len(options)} 个判定模型写入配置 schema 下拉："
            f"{', '.join(options)}"
        )
    except Exception as exc:
        logger.warning(f"[智能过滤] 写入判定模型下拉失败: {exc!r}")
