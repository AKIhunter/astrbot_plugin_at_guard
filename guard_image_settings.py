"""LivingHello 独立生图：复用已注册连接、统一额度和当前会话投递。"""
from __future__ import annotations

import asyncio
import base64
import copy
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from urllib.parse import urlsplit
from types import SimpleNamespace

try:
    from .guard_native_reply import NativeReplyError, revision
    from .guard_router_connections import router_connections, CHAT_TYPES
    from .guard_request_trace import emit, TRACE_KEY, touch
except ImportError:
    from guard_native_reply import NativeReplyError, revision
    from guard_router_connections import router_connections, CHAT_TYPES
    from guard_request_trace import emit, TRACE_KEY, touch

TOOL_NAME = "livinghello_generate_image"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_EVENT = ContextVar("livinghello_image_event", default=None)
FIELDS = {
    "enabled": ("启用AI生图", "bool", "主模型可按需要调用文生图或图生图工具，关闭后不提供工具。"),
    "provider_id": ("生图模型 / 路由上游", "provider", "复用AstrBot已注册模型或多API路由的具体上游连接，不读取BiliBot。所选API必须实际支持生图。"),
    "model": ("生图模型名称（可选）", "string", "留空使用所选模型名；同一API的生图模型名可在此指定。不会更改主对话模型。"),
    "protocol": ("生图接口", "protocol", "标准图片接口使用images/generations和images/edits；对话生图接口使用chat/completions的image模态。"),
    "daily_limit": ("每日生图次数上限", "int", "全插件共用，按服务器本地自然日重置。每次上游请求计一次，失败和结果未知也计数；0禁止生图，重载不清零。"),
    "size": ("生成尺寸", "size", "使用服务支持的尺寸，建议分辨率范围512x512~2048x2048；可选自定义宽高。每次只请求1张图片。"),
    "timeout_seconds": ("生图超时（秒）", "int", "10~500秒，超时不会换上游补发，防止重复收费。原自主回复时效仍生效。"),
}
DEFAULTS = dict(enabled=False, provider_id="", model="", protocol="images",
                daily_limit=20, size="1024x1024", timeout_seconds=90)


class ImageSettings:
    def __init__(self, plugin):
        self.plugin = plugin
        self.context = plugin.context
        self.lock = asyncio.Lock()
        data_dir = getattr(plugin, "data_dir", None)
        self.directory = Path(data_dir) if data_dir else None
        self.path = self.directory / "image_generation.json" if self.directory else None
        self.ledger = self.directory / "image_generation_quota.sqlite3" if self.directory else None
        self.values = copy.deepcopy(DEFAULTS)
        self.error = ""
        if self.path and self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or set(loaded) != set(DEFAULTS):
                    raise ValueError("invalid settings")
                self._validate(loaded)
                self.values = loaded
            except Exception:
                self.error = "生图配置损坏，请恢复备份；当前禁止生图。"
        self._tool = None
        self._closed = False
        self._tasks = set()

    def targets(self):
        """连接秘密仅在服务端使用，返回浏览器的列表只保留ID和模型名。"""
        targets = {}
        getter = getattr(self.context, "get_all_providers", None)
        for provider in getter() if callable(getter) else []:
            config = getattr(provider, "provider_config", {}) or {}
            pid = str(config.get("id") or "")
            if not pid or config.get("enable") is False:
                continue
            upstreams = getattr(provider, "all_upstreams", None) or getattr(provider, "upstreams", None)
            if config.get("type") == "multi_api_router" and upstreams:
                parent = str(config.get("parent_provider_id") or pid.split(":upstream_", 1)[0])
                for upstream in upstreams:
                    if getattr(upstream, "api_type", "chat") not in CHAT_TYPES | {"image_generation"}:
                        continue
                    client = getattr(upstream, "client", None)
                    slot = getattr(upstream, "slot", 0)
                    if client is not None and slot:
                        targets[f"{parent}:upstream_{slot}"] = (
                            client, str(getattr(upstream, "name", "") or f"上游 {slot}"), "router_upstream")
            elif config.get("type") != "multi_api_router":
                targets[pid] = (provider, str(config.get("display_name") or pid), "provider")
        for pid, config in router_connections(self.context).items():
            if config["api_type"] not in CHAT_TYPES | {"image_generation"}:
                continue
            # image_generation不在get_all_providers内；这里只复用连接，不创建聊天客户端。
            connection = SimpleNamespace(provider_config=config,
                                         get_model=lambda model=config["model"]: model)
            targets[pid] = (connection, config["display"], "router_upstream")
        return targets

    def _public_targets(self):
        return [dict(id=pid, display=label, model=str(provider.get_model() or ""), kind=kind)
                for pid, (provider, label, kind) in self.targets().items()]

    @staticmethod
    def today():
        return datetime.now().date().isoformat()

    @contextmanager
    def _connect(self):
        if not self.ledger:
            raise NativeReplyError("生图数据目录不可用，不能保存额度", 503)
        db = sqlite3.connect(self.ledger, timeout=5)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS calls (request_id TEXT PRIMARY KEY, "
                       "day TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL)")
            with db:
                yield db
        finally:
            db.close()

    def used(self):
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM calls WHERE day=?", (self.today(),)).fetchone()[0]

    def read(self):
        providers = self._public_targets()
        try:
            used = self.used()
            quota_error = ""
        except Exception:
            used, quota_error = 0, "无法读取持久额度，暂时禁止生图。"
        return dict(available=self.directory is not None and not self.error and not quota_error,
                    scope="LivingHello独立能力；全插件共用每日额度",
                    reason=self.error or quota_error, values=copy.deepcopy(self.values),
                    fields=[dict(key=k, title=v[0], kind=v[1], help=v[2]) for k, v in FIELDS.items()],
                    providers=providers, revision=revision(self.values),
                    usage=dict(day=self.today(), used=used, limit=self.values["daily_limit"]),
                    tool_name=TOOL_NAME, tool_ready=self._tool is not None)

    @staticmethod
    def _validate(values):
        for key, value in values.items():
            valid = False
            if key == "enabled":
                valid = isinstance(value, bool)
            elif key in {"daily_limit", "timeout_seconds"}:
                low, high = (0, 1000) if key == "daily_limit" else (10, 500)
                valid = isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
            elif key == "protocol":
                valid = isinstance(value, str) and value in {"images", "chat_images"}
            elif key == "size":
                valid = isinstance(value, str) and value in {"auto", "1024x1024", "1536x1024", "1024x1536"}
                if not valid and isinstance(value, str):
                    match = re.fullmatch(r"(\d{3,4})x(\d{3,4})", value)
                    valid = bool(match) and 512 <= int(match.group(1)) <= 2048 and 512 <= int(match.group(2)) <= 2048
            else:
                valid = isinstance(value, str) and len(value) <= 200
            if not valid:
                raise NativeReplyError(f"{FIELDS[key][0]}的值无效")

    async def save(self, body):
        if not isinstance(body, dict) or set(body) != {"expected_revision", "values"}:
            raise NativeReplyError("生图配置请求格式无效")
        async with self.lock:
            if not self.path or self.error:
                raise NativeReplyError(self.error or "生图配置目录不可用", 503)
            if body["expected_revision"] != revision(self.values):
                raise NativeReplyError("生图设置已变化，请重新读取；草稿保留", 409)
            try:
                if self.path.exists() and json.loads(self.path.read_text()) != self.values:
                    raise NativeReplyError("生图磁盘配置已变化，请重新读取", 409)
            except (ValueError, OSError) as exc:
                raise NativeReplyError("生图磁盘配置不可读，请恢复备份", 503) from exc
            patch = body["values"]
            if not isinstance(patch, dict) or not patch or set(patch) - set(FIELDS):
                raise NativeReplyError("只能保存独立生图公开字段")
            values = {**self.values, **patch}
            self._validate(values)
            if values["provider_id"] and values["provider_id"] not in self.targets():
                raise NativeReplyError("生图模型当前不可用，请选择已注册模型或路由上游")
            if values["enabled"]:
                if not values["provider_id"]:
                    raise NativeReplyError("启用前请选择生图模型")
                if not self.ensure_tool():
                    raise NativeReplyError("当前宿主不支持生图工具，未开启生图", 503)
                self.connection(values)  # 保存前验证真实连接，不发API请求。
            try:
                self._write(values)
            except OSError as exc:
                raise NativeReplyError("生图配置保存失败，运行设置未改变", 500) from exc
            self.values = values
            return self.read()

    def _write(self, values):
        fd, temporary = tempfile.mkstemp(prefix=".image-settings-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(values, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def connection(self, values):
        target = self.targets().get(values["provider_id"])
        if not target:
            raise NativeReplyError("生图模型或路由上游不可用", 503)
        provider = target[0]
        config = getattr(provider, "provider_config", {}) or {}
        base = str(config.get("api_base") or config.get("base_url") or "").strip().rstrip("/")
        key = config.get("key") or config.get("api_key")
        key = next((v for v in key if isinstance(v, str) and v), "") if isinstance(key, list) else key
        model = values["model"].strip() or str(provider.get_model() or "")
        if urlsplit(base).scheme not in {"https", "http"} or not urlsplit(base).netloc:
            raise NativeReplyError("所选模型没有可用的OpenAI兼容API地址", 503)
        if urlsplit(base).username or urlsplit(base).password or urlsplit(base).query or urlsplit(base).fragment:
            raise NativeReplyError("生图API地址不能包含凭据、查询参数或片段", 503)
        if not isinstance(key, str) or not key or not model or model == "automatic":
            raise NativeReplyError("生图连接缺少密钥或真实模型名称", 503)
        return base, key, model

    def ensure_tool(self):
        if self._tool is not None:
            return self._tool
        try:
            try:
                from .guard_image_tool import GenerateImageTool
            except ImportError:
                from guard_image_tool import GenerateImageTool
            self._tool = GenerateImageTool(service=self)
        except ImportError:
            return None
        return self._tool

    def attach(self, event, req):
        if self._closed or self.error or not self.values["enabled"] or self.values["daily_limit"] == 0:
            emit(event, "image_tool_attachment", "disabled", reason="image_unavailable")
            return
        tool = self.ensure_tool()
        if tool is None:
            emit(event, "image_tool_attachment", "failed", reason="tool_import_failed")
            return
        from astrbot.core.agent.tool import ToolSet
        # 复制工具集，避免在共享人格工具集中污染其他请求。
        tools = ToolSet()
        for existing in getattr(getattr(req, "func_tool", None), "tools", []):
            tools.add_tool(existing)
        if not any(t.name == TOOL_NAME for t in tools.tools):
            tools.add_tool(tool)
        req.func_tool = tools
        emit(event, "image_tool_attachment", "success", tool=TOOL_NAME)

    def blocked(self, event):
        if self._closed or event.get_extra("_at_guard_delivery_blocked"):
            return True
        if self.plugin._delivery().recalled(event):
            return True
        if self.plugin._judged_event(event) and self.plugin._gate_mode() == "enforce":
            return bool(self.plugin._stale_reason(event, self.plugin._group_key(event)))
        return False

    def reserve(self, event, prompt, use_reference):
        scope = str(getattr(event, "unified_msg_origin", "") or "")
        mid = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        # 缺少平台ID时只在事件生命周期内去重，不混用不同用户请求。
        nonce = event.get_extra("_livinghello_image_nonce")
        if not nonce:
            nonce = os.urandom(16).hex()
            event.set_extra("_livinghello_image_nonce", nonce)
        request_id = hashlib.sha256(json.dumps(
            [scope, mid or nonce, prompt, use_reference], ensure_ascii=False).encode()).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM calls WHERE request_id=?", (request_id,)).fetchone():
                return None
            used = db.execute("SELECT COUNT(*) FROM calls WHERE day=?", (self.today(),)).fetchone()[0]
            if used >= self.values["daily_limit"]:
                raise NativeReplyError("今日生图次数已用完，请勿重复调用", 429)
            db.execute("INSERT INTO calls VALUES (?, ?, 'reserved', ?)", (request_id, self.today(), time.time()))
            cutoff = (datetime.now().date() - timedelta(days=7)).isoformat()
            db.execute("DELETE FROM calls WHERE day<?", (cutoff,))
        return request_id

    def mark(self, request_id, status):
        try:
            with self._connect() as db:
                db.execute("UPDATE calls SET status=? WHERE request_id=?", (status, request_id))
        except (sqlite3.Error, OSError):
            # 已预占的记录仍计数；完成标记故障不能触发重试或退回额度。
            self.plugin.logger.warning("[LivingHello] 生图完成状态未能更新，保留预占额度")

    @staticmethod
    def references(event):
        """只接受本次输入或引用的真实图片，不接受模型提交任意URL/文件路径。"""
        result = []
        sources = event.get_extra("_at_guard_fragment_sources", ()) or (event,)
        for source in sources:
            for component in source.get_messages():
                if type(component).__name__ == "Image":
                    result.append(component)
                elif type(component).__name__ == "Reply":
                    result.extend(c for c in (getattr(component, "chain", None) or [])
                                  if type(c).__name__ == "Image")
        return result[:1]

    async def generate(self, event, prompt, use_reference=False):
        service = event.get_extra(TRACE_KEY)
        # 同步工具内部步骤关联宿主span，次数仍只在实际调用边界累计。
        parent_span = next(reversed(event.get_extra("_livinghello_tool_spans", {}).values()), None)
        span = service.start(event, "image_tool", tool=TOOL_NAME, parent_span=parent_span) if service else None
        # 生图内部调用与宿主tool-start共用请求，但不重复累计工具次数。
        if service and not event.get_extra("_livinghello_tool_spans"):
            service.increment(event, "tool_calls")
        emit(event, "image_api", "skipped", calls=0, reason="preflight")
        def outcome(status, reason):
            emit(event, "image_preflight", status, reason=reason)
            if service:
                service.end(event, span, status, reason=reason)
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000 or not isinstance(use_reference, bool):
            outcome("failed", "invalid_parameters")
            return "生图参数无效：需要1~4000字的描述及图生图开关。"
        if self._closed or self.error or not self.values["enabled"]:
            outcome("disabled", "image_disabled")
            return "AI生图未启用。"
        if self.blocked(event):
            reason = event.get_extra("_at_guard_delivery_blocked") or self.plugin._stale_reason(
                event, self.plugin._group_key(event)) or "reply_cancelled"
            outcome("cancelled", reason)
            return "当前回复已取消，未请求生图。"
        values = copy.deepcopy(self.values)
        request_id = None
        task = asyncio.current_task()
        self._tasks.add(task)
        token = IMAGE_EVENT.set(event)
        terminal_status, terminal_reason = "failed", "preflight_failed"
        try:
            connection = self.connection(values)
            refs = self.references(event) if use_reference else []
            if use_reference and not refs:
                terminal_reason = "reference_missing"
                return "图生图需要本次消息或引用中的图片，请用户补充图片。"
            # 上传前读来源；无效图片不计生图额度。
            reference = await self._reference_data(refs[0], values) if refs else None
            async with self.lock:
                if self.blocked(event) or self.values != values or not self.values["enabled"]:
                    terminal_status, terminal_reason = "cancelled", "reply_or_config_changed"
                    return "回复或生图配置已变化，未请求生图。"
                # 参考图下载可能跨过模型重载；请求前重新解析当前注册连接。
                connection = self.connection(values)
                request_id = self.reserve(event, prompt.strip(), use_reference)
            if not request_id:
                terminal_status, terminal_reason = "skipped", "duplicate_image_call"
                return "本条消息已请求过相同生图，结果可能已发送或未知，请勿重复调用。"
            emit(event, "image_quota", "success", reason="reserved")
            emit(event, "image_preflight", "success")
            image = await asyncio.wait_for(self._request(
                values, connection, prompt.strip(), reference),values["timeout_seconds"])
            if self.blocked(event) or not self.values["enabled"]:
                self.mark(request_id, "cancelled")
                terminal_status, terminal_reason = "cancelled", "reply_cancelled_after_generation"
                emit(event, "image_delivery", "cancelled", reason=terminal_reason)
                return "图片已生成但回复已取消，未发送；本次仍计额度。"
            # 沿用事件原生发送；图片先发送，工具结果再回主模型组织简短说明。
            from astrbot.api.message_components import Image
            await event.send(event.chain_result([Image.fromFileSystem(str(image))]))
            if event.get_extra("_at_guard_delivery_blocked"):
                self.mark(request_id, "cancelled")
                terminal_status, terminal_reason = "cancelled", "reply_cancelled_before_image_send"
                emit(event, "image_delivery", "cancelled", reason=terminal_reason)
                return "发送前回复已取消，未发送图片；本次仍计额度。"
            self.mark(request_id, "sent")
            terminal_status, terminal_reason = "success", "image_sent"
            emit(event, "image_delivery", "success")
            return "图片已发送到当前会话。请简短回应，不要再次生成或伪造图片链接。"
        except asyncio.CancelledError:
            terminal_status, terminal_reason = "cancelled", "task_cancelled"
            if request_id:
                self.mark(request_id, "unknown")
            raise
        except Exception as exc:
            terminal_status = "timeout" if isinstance(exc, asyncio.TimeoutError) else "failed"
            terminal_reason = "quota_exhausted" if isinstance(exc, NativeReplyError) and getattr(
                exc, "status_code", None) == 429 else "image_exception"
            if isinstance(exc, NativeReplyError) and "今日生图次数" in str(exc):
                terminal_reason = "quota_exhausted"
                emit(event, "image_quota", "skipped", reason=terminal_reason)
            emit(event, "image_error", terminal_status, reason=terminal_reason, error_type=type(exc).__name__)
            if service:
                row = service.rows.get(event.get_extra("_livinghello_request_id"), {})
                if row.get("features", {}).get("image_api", {}).get("status") == "running":
                    emit(event, "image_api", terminal_status, reason=terminal_reason)
            if request_id:
                self.mark(request_id, "unknown")
            # 错误只展示受控提示，不将URL、密钥或服务响应正文送给模型。
            message = str(exc) if isinstance(exc, NativeReplyError) else "接口异常或超时"
            return f"生图未完成：{message}。已请求的次数仍计额度；结果未知时不要自动重试。"
        finally:
            if service:
                service.end(event, span, terminal_status, reason=terminal_reason)
            IMAGE_EVENT.reset(token)
            self._tasks.discard(task)

    @staticmethod
    def _image_bytes(value):
        if not isinstance(value, str) or len(value) > MAX_IMAGE_BYTES * 4 // 3 + 200:
            raise NativeReplyError("图片大小超出限制")
        if value.startswith("data:"):
            if not value.startswith("data:image/") or ";base64," not in value:
                raise NativeReplyError("图片编码格式不支持")
            value = value.split(",", 1)[1]
        try:
            data = base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise NativeReplyError("图片编码无效") from exc
        return ImageSettings._check_image(data)

    @staticmethod
    def _check_image(data):
        if not 0 < len(data) <= MAX_IMAGE_BYTES:
            raise NativeReplyError("图片为空或超过10MiB")
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return data, "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return data, "image/jpeg"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return data, "image/webp"
        raise NativeReplyError("只支持PNG、JPEG或WebP图片")

    async def _download(self, url, timeout):
        event = IMAGE_EVENT.get()
        started = time.monotonic()
        if event:
            emit(event, "image_download", "running")
        try:
            data = await self._download_raw(url, timeout)
            if event:
                emit(event, "image_download", "success", ms=round((time.monotonic()-started)*1000, 2))
            return data
        except BaseException as exc:
            if event:
                emit(event, "image_download", "cancelled" if isinstance(exc, asyncio.CancelledError) else
                     "timeout" if isinstance(exc, asyncio.TimeoutError) else "failed",
                     error_type=type(exc).__name__)
            raise

    async def _download_raw(self, url, timeout):
        import aiohttp
        if urlsplit(url).scheme not in {"https", "http"}:
            raise NativeReplyError("图片URL格式不支持")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout),
                                        trust_env=True) as session:
            async with session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    raise NativeReplyError(f"图片下载HTTP {response.status}")
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        raise NativeReplyError("图片超过10MiB")
                    chunks.append(chunk)
                return self._check_image(b"".join(chunks))

    async def _reference_data(self, image, values):
        value = str(getattr(image, "url", "") or getattr(image, "file", "") or getattr(image, "path", ""))
        if value.startswith(("https://", "http://")):
            return await self._download(value, values["timeout_seconds"])
        if value.startswith("data:"):
            return self._image_bytes(value)
        if value.startswith("base64://"):
            return self._image_bytes(value[9:])
        # 平台提供的组件路径可以使用，工具参数不能指定任意本地文件。
        path = Path(value.removeprefix("file://"))
        if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
            raise NativeReplyError("当前图片文件不可用或过大")
        return self._check_image(path.read_bytes())

    async def _request(self, values, connection, prompt, reference):
        import aiohttp
        base, key, model = connection
        headers = {"Authorization": f"Bearer {key}"}
        request = dict(model=model, prompt=prompt, n=1, size=values["size"])
        protocol = values["protocol"]
        if protocol == "chat_images":
            endpoint = "/chat/completions"
            content = [dict(type="text", text=prompt)]
            if reference:
                data, mime = reference
                content.append(dict(type="image_url", image_url={
                    "url": f"data:{mime};base64," + base64.b64encode(data).decode()}))
            body = {"json": dict(model=model, messages=[dict(role="user", content=content)],
                                 modalities=["image"], n=1)}
        elif reference:
            endpoint = "/images/edits"
            form = aiohttp.FormData()
            for k, v in request.items():
                form.add_field(k, str(v))
            data, mime = reference
            form.add_field("image", data, filename="reference." + mime.split("/")[1], content_type=mime)
            body = {"data": form}
        else:
            endpoint = "/images/generations"
            body = {"json": request}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=values["timeout_seconds"]),
                                        trust_env=True) as session:
            event = IMAGE_EVENT.get()
            started = time.monotonic()
            if event:
                service = event.get_extra(TRACE_KEY)
                if service:
                    service.increment(event, "image_api_calls")
                emit(event, "image_api", "running", calls=1, model=model)
            async with session.post(base + endpoint, headers=headers, allow_redirects=False, **body) as response:
                if response.status not in {200, 201}:
                    raise NativeReplyError(f"生图接口HTTP {response.status}")
                if response.content_length and response.content_length > MAX_IMAGE_BYTES * 2:
                    raise NativeReplyError("生图响应过大")
                chunks, length = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    length += len(chunk)
                    if length > MAX_IMAGE_BYTES * 2:
                        raise NativeReplyError("生图响应过大")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                payload = json.loads(raw)
        if event:
            emit(event, "image_api", "success", calls=1, ms=round((time.monotonic()-started)*1000, 2))
        image_value = self.extract(payload)
        data, mime = (await self._download(image_value, values["timeout_seconds"])
                      if image_value.startswith(("https://", "http://")) else self._image_bytes(image_value))
        folder = self.directory / "generated_images"
        folder.mkdir(exist_ok=True)
        # 有界每日额度配合7天清理，避免临时媒体长期累积。
        for old in folder.glob("image-*"):
            if old.stat().st_mtime < time.time() - 7 * 86400:
                old.unlink()
        fd, filename = tempfile.mkstemp(prefix="image-", suffix="." + mime.split("/")[1], dir=folder)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        return Path(filename)

    @staticmethod
    def extract(payload):
        if not isinstance(payload, dict) or payload.get("error"):
            raise NativeReplyError("生图服务返回错误或无效响应")
        # 保留线上阿里百炼兼容响应：output.choices[].message.content[].image。
        out = payload.get("output")
        if isinstance(out, dict) and not payload.get("data") and not payload.get("choices"):
            payload = out
        rows = payload.get("data", [])
        choices = payload.get("choices", [])
        message = (choices[0].get("message", {}) if choices and isinstance(choices[0], dict) else {})
        message = message if isinstance(message, dict) else {}
        rows = rows or message.get("images", []) or message.get("content", [])
        if isinstance(rows, str):
            rows = [rows]
        for item in rows if isinstance(rows, list) else []:
            if isinstance(item, dict):
                nested = item.get("image_url", {})
                value = item.get("b64_json") or item.get("url") or item.get("image") or (
                    nested.get("url") if isinstance(nested, dict) else nested)
            else:
                value = item
            if isinstance(value, str) and value:
                inline = re.search(r"data:image/[\w.+-]+;base64,[A-Za-z0-9+/=\r\n]+",value)
                if inline:
                    return inline[0].replace("\r","").replace("\n","")
                markdown = re.search(r"!\[[^\]]*\]\((https?://[^)\s]+)\)",value)
                if markdown:
                    return markdown[1]
                return value
        raise NativeReplyError("生图响应中没有图片")

    async def close(self):
        self._closed = True
        current = asyncio.current_task()
        tasks = [t for t in self._tasks if t is not current and not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
