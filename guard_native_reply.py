"""AstrBot原生分段配置桥：白名单补丁、版本检查和失败恢复。"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
from pathlib import Path
import re

FIELDS = (
    "enable", "only_llm_result", "interval_method", "interval", "log_base",
    "words_count_threshold", "split_mode", "regex", "split_words", "content_cleanup_rule",
)
VIRTUAL_FIELDS = {"second_period", "split_words_enabled", "max_segments"}
MANAGED_RE = re.compile(r"^\(\?#at-guard-reply-v2;words=([01]);second=([01]);max=(\d+)\)")


def managed_regex(words_enabled=False, second=False, maximum=0):
    # 宿主对已经拆好的Plain逐段透传，仍由原生管线负责TTS、图片转换和发送。
    return f"(?#at-guard-reply-v2;words={int(words_enabled)};second={int(second)};max={maximum})(?s).+"


def managed_options(raw):
    match = MANAGED_RE.match(str(raw.get("regex", ""))) if raw.get("split_mode") == "regex" else None
    return ({"split_words_enabled": match[1] == "1", "second_period": match[2] == "1",
             "max_segments": int(match[3])} if match else None)
REMOVED_FIELDS = (
    "expression_persona_inject", "expression_quote_probability", "expression_quote_probability_max",
    "expression_quote_cooldown_seconds", "expression_quote_max_age_seconds",
    "expression_typo_probability", "expression_strip_trailing_period", "expression_flatten_newline",
    "expression_max_chars_per_bubble", "expression_forbid_phrases", "expression_self_repeat_guard",
    "expression_sticker_enabled", "expression_voice_enabled", "expression_tts_provider_id",
    "reply_max_bubbles_private", "reply_max_bubbles_group", "reply_bubble_interval_seconds",
)


class NativeReplyError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class NativeWriteError(RuntimeError):
    pass


class NativeRefreshError(RuntimeError):
    pass


def values(config):
    raw = config.get("platform_settings", {}).get("segmented_reply", {})
    if not isinstance(raw, dict) or any(key not in raw for key in FIELDS):
        raise NativeReplyError("当前AstrBot版本不支持原生分段同步", 503)
    result = copy.deepcopy({key: raw[key] for key in FIELDS})
    # 开关由原生正则反推，不另存一套插件分段设置；原生页面修改后立即可读。
    result["second_period"] = (
        raw["split_mode"] == "regex"
        and raw["regex"] == second_period_regex(raw["split_words"])
    )
    result.update(split_words_enabled=False, max_segments=0)
    result.update(managed_options(raw) or {})
    return result


def second_period_regex(words, second=True):
    """原生 findall 可直接执行：每段跳过第一个。；其他分段词立即结束。"""
    tokens = sorted(set(words), key=lambda word: (-len(word), word))
    all_words = "(?:" + "|".join(re.escape(word) for word in tokens) + ")"
    content = "(?:(?!" + all_words + ").)*"
    others = [word for word in tokens if word != "。"]
    # 多字分段词若以。开头，仍优先作为完整分段词，不能吞掉其首字符。
    first = "(?!" + "(?:" + "|".join(re.escape(word) for word in others) + "))" if others else ""
    skip = "(?:" + first + "。" + content + ")?" if second and "。" in tokens else ""
    marker = "(?#at-guard-second-period-v1)" if second else ""
    return "(?s)" + marker + "(?=.)" + content + skip + "(?:" + all_words + r"|\Z)"


def revision(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def validate(patch):
    if not isinstance(patch, dict) or not patch:
        raise NativeReplyError("没有要保存的分段字段")
    if set(patch) - (set(FIELDS) | VIRTUAL_FIELDS):
        raise NativeReplyError("只能修改原生分段回复字段")
    for key, value in patch.items():
        if key in {"enable", "only_llm_result", "second_period", "split_words_enabled"}:
            if not isinstance(value, bool):
                raise NativeReplyError(f"{key}必须是开关")
        elif key in {"interval_method", "split_mode"}:
            if value not in (("random", "log") if key == "interval_method" else ("regex", "words")):
                raise NativeReplyError(f"{key}选项无效")
        elif key == "interval":
            try:
                parts = [float(s.strip()) for s in value.split(",")]
                valid = len(parts) == 2 and all(math.isfinite(n) and n >= 0 for n in parts)
                valid = valid and parts[0] <= parts[1]
            except (AttributeError, ValueError, TypeError):
                valid = False
            if not valid:
                raise NativeReplyError("发送间隔需要两个非负秒数，最小值不能大于最大值，例如1.5,3.5")
        elif key == "log_base":
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 1):
                raise NativeReplyError("对数底数必须为大于1的有限数字")
        elif key in {"words_count_threshold", "max_segments"}:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise NativeReplyError("不分段字数阈值必须是非负整数")
            if key == "max_segments" and value > 20:
                raise NativeReplyError("最多分段数应为0至20，0表示不限")
        elif key == "split_words":
            if (not isinstance(value, list) or not value
                    or any(not isinstance(s, str) or not s for s in value)):
                raise NativeReplyError("分段词需要至少一个非空字符串")
        else:
            if not isinstance(value, str):
                raise NativeReplyError(f"{key}必须是字符串")
            try:
                compiled = re.compile(value)
            except re.error as exc:
                raise NativeReplyError(f"{key}正则无效：{exc}") from exc
            # 原生findall要求字符串；捕获分组会产生tuple，导致正文丢失。
            if key == "regex" and (not value or compiled.groups):
                raise NativeReplyError("分段正则不能为空或含捕获分组，请使用(?:...)")
    return copy.deepcopy(patch)


def migrate(config, native):
    """只删除已移除键；新回复头沿用当前宿主设置，已有新开关保持。"""
    changed = False
    for key in REMOVED_FIELDS:
        if key in config:
            del config[key]
            changed = True
    platform = native.get("platform_settings", {})
    for key, source, fallback in (
        ("reply_quote_enabled", "reply_with_quote", True),
        ("reply_mention_enabled", "reply_with_mention", False),
    ):
        if key not in config:
            config[key] = platform.get(source, fallback) is True
            changed = True
    return changed


class NativeReply:
    def __init__(self, context):
        self.context = context
        self.lock = asyncio.Lock()

    def manager(self):
        mgr = getattr(self.context, "astrbot_config_mgr", None)
        if mgr is None or not isinstance(getattr(mgr, "confs", None), dict):
            raise NativeReplyError("当前AstrBot版本不支持原生分段同步", 503)
        return mgr

    def read(self, config_id="default"):
        mgr = self.manager()
        if not isinstance(config_id, str) or config_id not in mgr.confs:
            raise NativeReplyError("所选原生配置不存在", 404)
        data = values(mgr.confs[config_id])
        profiles = [{"id": str(row["id"]), "name": str(row.get("name") or row["id"])}
                    for row in mgr.get_conf_list() if row["id"] in mgr.confs]
        return {"config_id": config_id, "profiles": profiles,
                "values": data, "revision": revision(data)}

    @staticmethod
    def service(request):
        try:
            svc = request._request.app.state.services.config_profiles
        except AttributeError as exc:
            raise NativeReplyError("当前AstrBot版本不支持原生分段同步", 503) from exc
        lifecycle = getattr(svc, "core_lifecycle", None)
        if not callable(getattr(lifecycle, "reload_pipeline_scheduler", None)):
            raise NativeReplyError("当前AstrBot版本不支持原生分段同步", 503)
        return svc

    async def persist(self, svc, config_id, patch):
        """复用宿主原子写入，只合并落盘分段字段，保留临时运行前缀与动态模型。"""
        config = self.manager().confs[config_id]
        try:
            disk = json.loads(Path(config.config_path).read_text(encoding="utf-8-sig"))
            disk["platform_settings"]["segmented_reply"].update(copy.deepcopy(patch))
            validate(values(disk))
            config["platform_settings"]["segmented_reply"].update(copy.deepcopy(patch))
            # 宿主快照生成只用于分配保存版本；不能把运行时临时项持久化。
            _, save_revision = config._prepare_config_snapshot(None)
            if not config._write_config_snapshot(disk, save_revision, 2):
                raise RuntimeError("原生配置已被更新的保存版本替代")
        except Exception as exc:
            raise NativeWriteError("原生配置落盘失败") from exc
        try:
            await svc.core_lifecycle.reload_pipeline_scheduler(config_id)
        except Exception as exc:
            raise NativeRefreshError("原生发送流程刷新失败") from exc

    async def save(self, request, body):
        if not isinstance(body, dict) or set(body) - {"config_id", "expected_revision", "values"}:
            raise NativeReplyError("原生分段请求格式无效")
        patch = validate(body.get("values"))
        config_id = body.get("config_id", "default")
        svc = self.service(request)
        async with self.lock:
            current = self.read(config_id)
            if body.get("expected_revision") != current["revision"]:
                raise NativeReplyError("原生分段配置已被修改，请重新读取后再保存；当前未保存内容已保留", 409)
            config = self.manager().confs[config_id]
            if (not getattr(config, "config_path", None)
                    or not callable(getattr(config, "_prepare_config_snapshot", None))
                    or not callable(getattr(config, "_write_config_snapshot", None))):
                raise NativeReplyError("当前AstrBot版本不支持分段字段的独立原生保存", 503)
            disk = values(json.loads(Path(config.config_path).read_text(encoding="utf-8-sig")))
            if disk != current["values"]:
                raise NativeReplyError("原生运行与落盘分段设置不一致，请重新读取或检查AstrBot配置", 409)
            # 将第二句号开关编译成原生正则，宿主仍负责断句、TTS及最终发送。
            # 新页面的三个细化字段编码在同一份原生正则中，各配置独立保存。
            managed = managed_options(config["platform_settings"]["segmented_reply"])
            use_managed = (managed is not None or bool(
                set(patch) & (VIRTUAL_FIELDS | {"only_llm_result", "enable", "split_words"})))
            controls = {key: patch.pop(key, current["values"][key]) for key in VIRTUAL_FIELDS}
            second_period = controls["second_period"]
            if use_managed and not ("regex" in patch or patch.get("split_mode") == "words"):
                patch["split_mode"] = "regex"
                patch["regex"] = managed_regex(controls["split_words_enabled"], second_period,
                                                controls["max_segments"])
            elif second_period and (
                body["values"].get("second_period") is True
                or "split_words" in patch
            ):
                patch["split_mode"] = "regex"
                patch["regex"] = second_period_regex(
                    patch.get("split_words", current["values"]["split_words"]))
            elif body["values"].get("second_period") is False and current["values"]["second_period"]:
                patch["split_mode"] = "regex"
                patch["regex"] = second_period_regex(
                    patch.get("split_words", current["values"]["split_words"]), second=False)
            elif "split_words" in patch and not second_period and "split_mode" not in patch:
                patch["split_mode"] = "words"
            if not patch:
                return current
            merged = copy.deepcopy(dict(config))
            merged["platform_settings"]["segmented_reply"].update(patch)
            validate(values(merged))
            if all(current["values"][k] == v for k, v in patch.items()):
                return current
            try:
                await self.persist(svc, config_id, patch)
                saved = self.read(config_id)
                if any(saved["values"][k] != v for k, v in patch.items()):
                    raise RuntimeError("原生保存后的运行配置不一致")
                # 宿主有路径时同时确认落盘，路径不对外返回。
                path = getattr(self.manager().confs[config_id], "config_path", None)
                if path:
                    disk = values(json.loads(Path(path).read_text(encoding="utf-8-sig")))
                    if disk != saved["values"]:
                        raise RuntimeError("原生运行与落盘配置不一致")
                return saved
            except Exception as exc:
                # 用最新完整配置只恢复本次字段，保留失败期间其他设置修改。
                latest = self.manager().confs[config_id]
                target = copy.deepcopy(latest["platform_settings"]["segmented_reply"])
                for key, value in patch.items():
                    if target.get(key) == value:
                        target[key] = current["values"][key]
                restore_patch = {k: target[k] for k in patch}
                try:
                    await self.persist(svc, config_id, restore_patch)
                except Exception:
                    # 恢复仍使用分段字段补丁；不能通过整份保存带入临时运行项。
                    try:
                        await self.persist(svc, config_id, restore_patch)
                    except NativeWriteError as restore_exc:
                        raise NativeReplyError(
                            "保存失败，本次设置恢复未能落盘，请检查AstrBot日志", 500
                        ) from restore_exc
                    except NativeRefreshError as restore_exc:
                        raise NativeReplyError(
                            "保存失败，配置已恢复但原生发送流程刷新失败，请检查AstrBot日志", 500
                        ) from restore_exc
                raise NativeReplyError("原生分段保存失败，已恢复本次设置", 500) from exc
