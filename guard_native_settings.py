"""LivingHello原生模型/人格/语音桥：只合并公开字段，不落盘运行时临时配置。"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile

try:
    from .guard_native_reply import NativeReply, NativeReplyError, revision
except ImportError:
    from guard_native_reply import NativeReply, NativeReplyError, revision


FIELDS = {
    "provider_id": ("agent_runner.config.model.provider_id", "模型", "主对话模型", "provider",
                    "与所选AstrBot原生配置共用。留空沿用宿主默认模型；不改变判定小模型。"),
    "fallback_provider_ids": ("agent_runner.config.model.fallback_provider_ids", "模型", "回退模型（按顺序）", "providers",
                             "主对话模型接口失败时按顺序回退。这里只能选择已注册的对话模型。"),
    "request_max_retries": ("agent_runner.config.model.request_max_retries", "模型", "异常最大尝试次数", "int",
                           "一次模型请求的尝试上限，重复尝试可能增加等待和费用。允许1至10。"),
    "streaming_response": ("provider_settings.streaming_response", "模型", "流式回复", "bool",
                          "沿用AstrBot流式发送机制。与多消息分段同时开启时，请以原生流水线的实际行为为准。"),
    "tts_enabled": ("provider_tts_settings.enable", "语音回复", "使用语音回复", "bool",
                    "同步所选AstrBot原生配置的语音回复开关。实际转换还需要有效TTS模型、会话允许且触发概率通过。"),
    "tts_provider_id": ("provider_tts_settings.provider_id", "语音回复", "默认 TTS 模型", "tts",
                       "同步所选AstrBot原生配置的默认文本转语音模型。留空时可由会话单独选择。"),
    "persona_id": ("agent_runner.config.persona.persona_id", "人格", "人格（保存后固定）", "persona",
                   "手动选择并保存后固定此配置的人格，覆盖会话旧人格和从绫人格选择；不会因刷新或试跑自动换人。"),
    "t2i": ("t2i", "文本排版转图", "启用文本排版转图", "bool",
            "把回复文字排版成图片，适合长回复；不是AI文生图。"),
    "t2i_word_threshold": ("t2i_word_threshold", "文本排版转图", "转图字数阈值", "int",
                         "回复超过此字数后由原生渲染器转成图片。允许0至100000。"),
    "t2i_strategy": ("t2i_strategy", "文本排版转图", "渲染方式", "strategy",
                    "remote使用原生配置的远程排版服务；local使用宿主本地渲染。"),
    "t2i_endpoint": ("t2i_endpoint", "文本排版转图", "远程渲染地址", "string",
                    "AstrBot原生排版服务地址，仅在远程渲染时使用。"),
    "t2i_active_template": ("t2i_active_template", "文本排版转图", "当前排版模板", "string",
                          "使用AstrBot已配置的模板名称，例如base。"),
}


def get_path(config, path):
    node = config
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            raise KeyError(path)
        node = node[key]
    return copy.deepcopy(node)


def put_path(config, path, value):
    keys = path.split(".")
    node = config
    for key in keys[:-1]:
        node = node[key]
    node[keys[-1]] = copy.deepcopy(value)


class NativeSettings(NativeReply):
    def __init__(self, plugin, native_reply):
        super().__init__(plugin.context)
        self.plugin = plugin
        self.lock = native_reply.lock  # 分段和模型保存共用一把锁，避免同配置相互覆盖。
        self.pin_path = Path(plugin.data_dir) / "native_persona_pins.json" if plugin.data_dir else None
        self.profile_path = Path(plugin.data_dir) / "native_selected_profile.json" if plugin.data_dir else None
        self.pins = {}
        self.pin_error = False
        if self.pin_path and self.pin_path.exists():
            try:
                pins = json.loads(self.pin_path.read_text(encoding="utf-8"))
                if not isinstance(pins, dict) or any(not isinstance(k, str) or
                        not isinstance(v, str) or not v for k, v in pins.items()):
                    raise ValueError("invalid pins")
                self.pins = pins
            except Exception:
                self.pin_error = True
                plugin.logger.error("[LivingHello] 固定人格文件损坏，禁止自动回退人格，请恢复备份")
        self._wrapper = None
        self._original = None

    def selected_profile(self):
        config_id = "default"
        if self.profile_path and self.profile_path.exists():
            try:
                config_id = json.loads(self.profile_path.read_text(encoding="utf-8"))["config_id"]
                if not isinstance(config_id, str):
                    raise ValueError("invalid profile")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise NativeReplyError("控制台配置选择记录不可读，请恢复备份", 503) from exc
        # 已删除的配置明确提示，不能悄悄把后续修改写到default。
        if config_id not in self.manager().confs:
            raise NativeReplyError("上次选择的AstrBot配置已不存在，请手动选择配置", 404)
        return {"config_id": config_id, "saved": bool(self.profile_path and self.profile_path.exists())}

    async def select_profile(self, body):
        if not isinstance(body, dict) or set(body) != {"config_id"} or not isinstance(body["config_id"], str):
            raise NativeReplyError("原生配置选择格式无效")
        async with self.lock:
            self.read(body["config_id"])
            if not self.profile_path:
                raise NativeReplyError("控制台配置选择目录不可用", 503)
            fd, temporary = tempfile.mkstemp(prefix=".native-profile-", dir=self.profile_path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(body, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.profile_path)
            except OSError as exc:
                raise NativeReplyError("配置选择保存失败，请重试", 500) from exc
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return body

    def personas(self):
        manager = getattr(self.context, "persona_manager", None)
        rows = getattr(manager, "personas_v3", [])
        return [{"id": row["name"], "name": row["name"]} for row in rows
                if isinstance(row, dict) and isinstance(row.get("name"), str)]

    def read(self, config_id="default"):
        result = super().read(config_id)
        config = self.manager().confs[config_id]
        local = config.get("agent_runner", {}).get("runner_type") == "local"
        data, fields = {}, []
        for key, (path, group, title, kind, help_text) in FIELDS.items():
            if path.startswith("agent_runner.") and not local:
                continue
            try:
                data[key] = get_path(config, path)
            except KeyError:
                continue
            fields.append(dict(key=key, group=group, title=title, kind=kind, help=help_text))
        pin = self.pins.get(config_id)
        result.update(values=data, fields=fields, personas=self.personas(),
                      persona_fixed=pin is not None, fixed_persona_id=pin,
                      persona_error=self.pin_error, local_runner=local)
        result["revision"] = revision({"values": data, "pin": pin, "pin_error": self.pin_error})
        return result

    def fixed_persona_id(self, umo):
        if self.pin_error:
            raise NativeReplyError("固定人格文件损坏，请恢复备份", 503)
        mgr = self.manager()
        config = mgr.get_conf(umo)
        config_id = next((key for key, value in mgr.confs.items() if value is config), None)
        return self.pins.get(config_id)

    def install(self):
        manager = getattr(self.context, "persona_manager", None)
        resolver = getattr(manager, "resolve_selected_persona", None)
        if not callable(resolver):
            return
        # 清除已停用实例的包装，重载不会形成多层选择器。
        original = getattr(resolver, "__dict__", {}).get("_livinghello_original", resolver)
        async def selected(*, umo, conversation_persona_id, platform_name, provider_settings=None):
            pid = self.fixed_persona_id(umo)
            if pid is None:
                return await original(umo=umo, conversation_persona_id=conversation_persona_id,
                                      platform_name=platform_name, provider_settings=provider_settings)
            if pid == "[%None]":
                return pid, None, pid, False
            persona = next((p for p in manager.personas_v3 if p["name"] == pid), None)
            if persona is None:
                raise NativeReplyError("已固定的人格不存在，请手动选择有效人格并保存", 409)
            return pid, persona, pid, False
        selected._livinghello_original = original
        self._original, self._wrapper = original, selected
        manager.resolve_selected_persona = selected

    def close(self):
        manager = getattr(self.context, "persona_manager", None)
        if self._wrapper and getattr(manager, "resolve_selected_persona", None) is self._wrapper:
            manager.resolve_selected_persona = self._original

    def _write_pins(self, pins):
        if self.pin_path is None:
            raise NativeReplyError("人格固定数据目录不可用", 503)
        fd, temporary = tempfile.mkstemp(prefix=".persona-pins-", dir=self.pin_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(pins, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.pin_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def validate(self, patch, current, providers):
        if not isinstance(patch, dict) or not patch or set(patch) - set(current["values"]):
            raise NativeReplyError("只能保存当前宿主支持的原生模型、人格、语音及排版字段")
        chat = {p["id"] for p in providers if p.get("role") == "chat" and "::" not in p["id"]}
        tts = {p["id"] for p in providers if p.get("role") == "tts"}
        vision = {p["id"] for p in providers if p.get("role") == "chat" and
                  p.get("supports_images") and p.get("kind") != "router_upstream"}
        personas = {p["id"] for p in current["personas"]} | {"[%None]"}
        for key, value in patch.items():
            kind = FIELDS[key][3]
            if kind == "bool":
                valid = isinstance(value, bool)
            elif kind == "int":
                maximum = 10 if key == "request_max_retries" else 100000
                minimum = 1 if key == "request_max_retries" else 0
                valid = isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum
            elif kind == "providers":
                valid = (isinstance(value, list) and len(value) <= 20 and
                         all(isinstance(v, str) and v in chat for v in value) and len(set(value)) == len(value))
            else:
                valid = isinstance(value, str) and len(value) <= (8000 if kind == "text" else 2000)
                if valid and kind in {"provider", "vision", "tts", "persona", "strategy"}:
                    options = {"provider": chat | {""}, "vision": vision | {""},
                               "tts": tts | {""}, "persona": personas,
                               "strategy": {"local", "remote"}}[kind]
                    valid = value in options
            if not valid:
                raise NativeReplyError(f"{FIELDS[key][2]}的值无效，请检查范围或可用选项")
        if "persona_id" in patch and (self.pin_error or self._wrapper is None or self.pin_path is None):
            raise NativeReplyError("当前人格固定入口不可用，未修改人格", 503)
        return copy.deepcopy(patch)

    def _persist_fields(self, config, patch):
        disk = json.loads(Path(config.config_path).read_text(encoding="utf-8-sig"))
        for key, value in patch.items():
            put_path(disk, FIELDS[key][0], value)
        _, save_revision = config._prepare_config_snapshot(None)
        if not config._write_config_snapshot(disk, save_revision, 2):
            raise RuntimeError("原生配置保存版本已被替代")
        for key, value in patch.items():
            put_path(config, FIELDS[key][0], value)

    async def save(self, request, body, providers):
        if not isinstance(body, dict) or set(body) - {"config_id", "expected_revision", "values"}:
            raise NativeReplyError("原生设置请求格式无效")
        svc = self.service(request)
        async with self.lock:
            config_id = body.get("config_id", "default")
            current = self.read(config_id)
            if body.get("expected_revision") != current["revision"]:
                raise NativeReplyError("原生设置已被修改，请重新读取；你的草稿仍保留", 409)
            patch = self.validate(body.get("values"), current, providers)
            config = self.manager().confs[config_id]
            try:
                disk = json.loads(Path(config.config_path).read_text(encoding="utf-8-sig"))
                if any(get_path(disk, FIELDS[key][0]) != current["values"][key] for key in patch):
                    raise NativeReplyError("运行与磁盘设置不一致，请重新读取", 409)
            except NativeReplyError:
                raise
            except Exception as exc:
                raise NativeReplyError("无法独立保存当前原生配置", 503) from exc
            before = {key: current["values"][key] for key in patch}
            old_pins = copy.deepcopy(self.pins)
            try:
                self._persist_fields(config, patch)
                if "persona_id" in patch:
                    new_pins = {**self.pins, config_id: patch["persona_id"]}
                    self._write_pins(new_pins)
                    self.pins = new_pins
                await svc.core_lifecycle.reload_pipeline_scheduler(config_id)
                if any(get_path(config, FIELDS[key][0]) != value for key, value in patch.items()):
                    raise RuntimeError("原生设置生效校验失败")
            except Exception as exc:
                try:
                    # 异步刷新期间若其他写入者更改了本字段，不能回退覆盖对方修改。
                    latest = json.loads(Path(config.config_path).read_text(encoding="utf-8-sig"))
                    if any(get_path(latest, FIELDS[key][0]) not in (value, before[key])
                           for key, value in patch.items()):
                        raise RuntimeError("原生设置发生并行修改")
                    self._persist_fields(config, before)
                    if self.pins != old_pins:
                        self._write_pins(old_pins)
                        self.pins = old_pins
                    await svc.core_lifecycle.reload_pipeline_scheduler(config_id)
                except Exception as recovery:
                    raise NativeReplyError("保存失败且恢复未完成，请检查原生配置与备份", 500) from recovery
                raise NativeReplyError("保存失败，原生字段和固定人格已恢复", 500) from exc
            return self.read(config_id)
