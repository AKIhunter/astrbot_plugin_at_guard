"""智能过滤插件的控制台后端。

前端页面在 pages/guard/index.html，通过 AstrBot 插件页桥接调用这里的接口。
所有接口都挂在 /<插件名>/console/* 下，写入前统一做白名单与范围校验，
避免前端传什么就往配置文件里塞什么。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.api.web import error_response, json_response, request

PLUGIN_NAME = "astrbot_plugin_at_guard"
SCHEMA_PATH = Path(__file__).resolve().parent / "_conf_schema.json"

# key -> (类型, 下限, 上限)。字符串型没有范围，单独处理。
NUMERIC_FIELDS: dict[str, tuple[str, float, float]] = {
    "participation_confidence_threshold": ("int", 0, 100),
    "classifier_timeout_seconds": ("float", 0.5, 8.0),
    "gate_min_length": ("int", 0, 20),
    "gate_hourly_budget": ("int", 0, 600),
    "gate_context_messages": ("int", 0, 30),
    "gate_context_max_age_seconds": ("float", 30, 3600),
    "gate_reply_max_age_seconds": ("float", 5, 120),
    "gate_memory_timeout_seconds": ("float", 0.2, 5),
    "participation_context_messages": ("int", 2, 30),
    "fragment_wait_seconds": ("float", 0, 5.0),
    "proactive_cooldown_seconds": ("float", 0, 60.0),
    "duplicate_window_seconds": ("int", 0, 3600),
}
BOOL_FIELDS = {"apply_to_private"}
STRING_FIELDS = {
    "gate_mode",
    "classifier_provider_id",
    "empty_mention_mode",
    "empty_mention_reply",
    "empty_mention_persona_prompt",
}
LIST_FIELDS = {"bot_names"}
EDITABLE = set(NUMERIC_FIELDS) | BOOL_FIELDS | STRING_FIELDS | LIST_FIELDS

MODE_LABELS = {
    "enforce": "硬闸门（判完就拦，低分不生成）",
    "shadow": "观察模式（只判定不拦截，用于校准阈值）",
    "legacy": "旧行为（先生成再丢弃，仅回滚用）",
    "off": "已解耦（插件完全不介入）",
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
    - 路由上游（``id=provider_id::model``）：锁定某个具体上游模型；
    - 原生 provider（``id=provider_id``）：选它就是该 provider 自家模型。
    """
    items: dict[str, dict[str, Any]] = {}
    try:
        for provider in plugin.context.get_all_providers():
            config = getattr(provider, "provider_config", {}) or {}
            provider_id = str(config.get("id", "") or "")
            if not provider_id:
                continue
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
                kind = "router"
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
                    if upstream_model:
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
            if entry is None:
                items[provider_id] = {
                    "id": provider_id,
                    "model": model,
                    "kind": kind,
                    "upstreams": names,
                    "instances": 1,
                }
            else:
                entry["instances"] += 1
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
                items[pinned_id] = {
                    "id": pinned_id,
                    "model": upstream_model,
                    "kind": "router_upstream",
                    "upstreams": [],
                    "instances": 1,
                    "parent": provider_id,
                    "display": display,
                }
    except Exception as exc:
        logger.debug(f"[智能过滤] 读取模型列表失败: {exc!r}")
    return sorted(items.values(), key=lambda item: item["id"])


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
        "today": engine.snapshot(),
        "lifetime": {
            "total": int(participation.get("total", 0) or 0),
            "allowed": int(participation.get("allowed", 0) or 0),
            "blocked": int(participation.get("blocked", 0) or 0),
            "average": float(participation.get("average", 0.0) or 0.0),
        },
        "budget": engine.budget_usage(),
        "providers": await _providers(plugin),
        "distribution": engine.score_distribution(engine.provider_label()),
        "decisions": engine.log.recent(100),
    }


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
        if key == "gate_mode" and text not in {"enforce", "shadow", "legacy", "off"}:
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
    if number < low or number > high:
        raise ValueError(f"{key} 超出范围 {low}~{high}")
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

    provider_id = updates.get("classifier_provider_id")
    if provider_id:
        known = {item["id"] for item in await _providers(plugin)}
        if provider_id not in known:
            return _failure(f"模型 {provider_id} 不在可用列表中")

    for key, value in updates.items():
        try:
            plugin.config[key] = value
        except Exception as exc:
            return _failure(f"写入 {key} 失败: {exc}", 500)
    try:
        plugin.config.save_config()
    except Exception as exc:
        logger.exception(f"[智能过滤] 保存配置失败: {exc}")
        return _failure(f"配置未落盘: {exc}", 500)

    logger.info(f"[智能过滤] 控制台更新 {len(updates)} 项: {sorted(updates)}")
    return _response(await _state(plugin), f"已保存 {len(updates)} 项")


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
    text = str((body or {}).get("text", "") or "").strip()
    if not text:
        return _failure("请输入要试跑的消息")
    bot_line = str((body or {}).get("bot_line", "") or "").strip()
    is_private = bool((body or {}).get("private", False))

    async def resolve_provider():
        provider_id = plugin.engine.provider_id()
        if provider_id:
            provider = plugin.context.get_provider_by_id(provider_id)
            if provider is not None:
                return provider
        return await plugin.context.get_using_provider_async(None)

    try:
        decision = await plugin.engine.decide(
            group_key="#probe",
            current_text=text,
            context_lines=[],
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
        }
    )


def register_webui(plugin: Any, context: Context) -> None:
    routes: list[tuple[str, str, Handler, str]] = [
        ("console/state", "GET", handle_state, "智能过滤控制台状态"),
        ("console/config", "POST", handle_config, "保存智能过滤参数"),
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
    _write_schema_options(plugin, last_options)


def _write_schema_options(plugin: Any, options: list[str]) -> None:
    try:
        metadata = plugin.context.get_registered_star(PLUGIN_NAME)
        config = getattr(metadata, "config", None)
        schema = getattr(config, "schema", None)
        if not isinstance(schema, dict):
            logger.warning("[智能过滤] 未找到插件配置 schema，判定模型下拉未注入")
            return
        field = schema.get("classifier_provider_id")
        if not isinstance(field, dict):
            logger.warning("[智能过滤] schema 缺少 classifier_provider_id 字段")
            return
        # 首项放空串代表「跟随会话当前模型」，与 default="" 对齐，避免下拉
        # 里没有默认值导致宿主前端显示异常。
        field["options"] = [""] + options
        logger.info(
            f"[智能过滤] 已把 {len(options)} 个判定模型写入配置 schema 下拉："
            f"{', '.join(options)}"
        )
    except Exception as exc:
        logger.warning(f"[智能过滤] 写入判定模型下拉失败: {exc!r}")
