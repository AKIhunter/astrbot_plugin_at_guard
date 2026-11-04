"""桥接BiliBot已有AI文生图配置，不尝试生成图片或读取返回密钥。"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

try:
    from .guard_native_reply import NativeReplyError, revision
except ImportError:
    from guard_native_reply import NativeReplyError, revision

FIELDS = {
    "IMAGE_GEN_BACKEND": ("生图后端", "backend", "openai为兼容接口；novelai只用于BiliBot手动动态配图。"),
    "IMAGE_GEN_MODEL": ("文生图模型名称", "string", "填写生图服务支持的真实模型名称，不使用AstrBot对话模型列表。"),
    "IMAGE_GEN_API_BASE": ("生图服务地址", "string", "使用BiliBot独立生图服务地址，不修改对话模型地址。"),
    "IMAGE_GEN_WIDTH": ("NovelAI宽度", "int", "仅NovelAI生效，64至2048像素。"),
    "IMAGE_GEN_HEIGHT": ("NovelAI高度", "int", "仅NovelAI生效，64至2048像素。"),
    "IMAGE_GEN_STEPS": ("NovelAI采样步数", "int", "仅NovelAI生效，1至50。"),
    "IMAGE_GEN_SCALE": ("NovelAI引导强度", "float", "仅NovelAI生效，0至10。"),
    "IMAGE_GEN_SAMPLER": ("NovelAI采样器", "string", "使用服务支持的采样器名称，不确定时保持原值。"),
    "IMAGE_GEN_NEGATIVE_PROMPT": ("NovelAI负面提示词", "text", "仅NovelAI生效，填写不希望图片出现的内容。"),
}


class ImageSettings:
    def __init__(self, context, lock):
        self.context, self.lock = context, lock

    def config(self):
        try:
            metadata = self.context.get_registered_star("astrbot_plugin_bilibili_ai_bot")
        except (AttributeError, KeyError, ValueError) as exc:
            raise NativeReplyError("BiliBot未安装，暂无AI文生图配置入口", 503) from exc
        if not getattr(metadata, "activated", False):
            raise NativeReplyError("BiliBot未启用，暂无AI文生图配置入口", 503)
        config = getattr(getattr(metadata, "star_cls", None), "config", None)
        if not isinstance(config, dict) or any(key not in config for key in FIELDS):
            raise NativeReplyError("当前BiliBot版本不支持此文生图配置桥", 503)
        return config

    def read(self):
        try:
            config = self.config()
        except NativeReplyError as exc:
            return dict(available=False, reason=str(exc), values={}, fields=[])
        values = {key: copy.deepcopy(config[key]) for key in FIELDS}
        # 密钥只参与版本摘要，永远不传给浏览器，也不能通过此页覆盖。
        token = revision({"values": values, "secret_version": revision(config.get("IMAGE_GEN_API_KEY", ""))})
        return dict(available=True, scope="BiliBot全局配置（所有会话共用）", values=values,
                    fields=[dict(key=k, title=v[0], kind=v[1], help=v[2]) for k, v in FIELDS.items()],
                    key_configured=bool(config.get("IMAGE_GEN_API_KEY")), revision=token)

    async def save(self, body):
        if not isinstance(body, dict) or set(body) - {"expected_revision", "values"}:
            raise NativeReplyError("文生图请求格式无效")
        async with self.lock:
            current = self.read()
            if not current["available"]:
                raise NativeReplyError(current["reason"], 503)
            if body.get("expected_revision") != current["revision"]:
                raise NativeReplyError("BiliBot文生图设置已变化，请重新读取；草稿保留", 409)
            patch = body.get("values")
            if not isinstance(patch, dict) or not patch or set(patch) - set(FIELDS):
                raise NativeReplyError("只能保存BiliBot公开文生图字段")
            for key, value in patch.items():
                kind = FIELDS[key][1]
                if kind in {"int", "float"}:
                    bounds = (64, 2048) if key.endswith(("WIDTH", "HEIGHT")) else (
                        (1, 50) if key.endswith("STEPS") else (0, 10))
                    valid = (not isinstance(value, bool) and isinstance(value, int if kind == "int" else (int, float))
                             and math.isfinite(value) and bounds[0] <= value <= bounds[1])
                else:
                    valid = isinstance(value, str) and len(value) <= (8000 if kind == "text" else 2000)
                    if kind == "backend":
                        valid = valid and value in {"openai", "novelai"}
                if not valid:
                    raise NativeReplyError(f"{FIELDS[key][0]}的值无效")
            config = self.config()
            before = {key: copy.deepcopy(config[key]) for key in patch}
            try:
                disk = json.loads(Path(config.config_path).read_text(encoding="utf-8-sig"))
                if any(disk[key] != before[key] for key in patch):
                    raise NativeReplyError("文生图运行与磁盘设置不一致", 409)
                disk.update(copy.deepcopy(patch))
                _, version = config._prepare_config_snapshot(None)
                if not config._write_config_snapshot(disk, version, 2):
                    raise RuntimeError("保存版本被替代")
                config.update(copy.deepcopy(patch))
            except NativeReplyError:
                raise
            except Exception as exc:
                # 写入失败不能将草稿留在运行配置；宿主原子写避免部分文件。
                config.update(before)
                raise NativeReplyError("BiliBot文生图配置保存失败", 500) from exc
            return self.read()
