"""事件局部分段：原生配置为唯一来源，模型预分段和多消息发送独立控制。"""
from __future__ import annotations

import re

from astrbot.api.message_components import Plain
from astrbot.core.agent.message import TextPart

try:
    from .guard_native_reply import managed_options, second_period_regex
except ImportError:
    from guard_native_reply import managed_options, second_period_regex


class ReplySegments:
    def __init__(self, plugin):
        self.plugin = plugin

    def config(self, event):
        getter = getattr(self.plugin.context, "get_config", None)
        try:
            config = getter(event.unified_msg_origin) if callable(getter) else {}
            return config.get("platform_settings", {}).get("segmented_reply", {})
        except Exception:
            return {}

    def attach(self, event, req):
        raw = self.config(event)
        if managed_options(raw) is None or event.get_extra("_at_guard_segment_prompt"):
            return
        if raw.get("only_llm_result") is True:
            text = ("请先组织完整答案，再按完整意思自行分段，每段之间用空行分隔。"
                    "同一段内不要仅因出现句号就换行；不要输出分段编号或分隔标签。")
        else:
            text = "普通聊天回复请使用一个连贯段落，不要用换行隔断；代码、表格等必要格式仍保留。"
        req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())
        event.set_extra("_at_guard_segment_prompt", True)

    def active(self, event):
        return managed_options(self.config(event)) is not None

    @staticmethod
    def structured(text):
        return ("```" in text or "~~~" in text
                or len(re.findall(r"(?m)^\s*\|.+\|\s*$", text)) >= 2)

    @staticmethod
    def paragraphs(text, options):
        # 围栏代码是一个完整段，内部换行和标点不能被聊天断句规则破坏。
        parts = []
        for block in re.split(r"(```[\s\S]*?```|~~~[\s\S]*?~~~)", text):
            if not block:
                continue
            if block.startswith(("```", "~~~")):
                parts.append(block)
                continue
            if ReplySegments.structured(block):
                parts.extend(value.strip() for value in re.split(r"\n\s*\n", block) if value.strip())
                continue
            for paragraph in re.split(r"\r?\n+", block):
                if not paragraph.strip():
                    continue
                words = list(options.get("split_words", [])) if options["split_words_enabled"] else []
                if options["second_period"] and "。" not in words:
                    words.append("。")
                chunks = (re.findall(second_period_regex(words, options["second_period"]), paragraph)
                          if words else [paragraph])
                parts.extend(chunk.strip() for chunk in chunks if chunk.strip())
        return parts

    def prepare(self, event, result, *, streaming=False):
        raw = self.config(event)
        options = managed_options(raw)
        if options is None or (not streaming and not result.is_llm_result()):
            return False
        if getattr(result, "_at_guard_segments_prepared", False):
            return True
        split = raw.get("enable") is True
        options["split_words"] = raw.get("split_words", [])
        chain = []
        # 流式每个token不是一个模型段落；相邻Plain先合成完整模型正文。
        pending = ""
        def flush():
            nonlocal pending
            if not pending:
                return
            text = pending
            if not split:
                if raw.get("only_llm_result") is not True and not self.structured(text):
                    text = re.sub(r"\s*\r?\n\s*", " ", text)
                chain.append(Plain(text))
            else:
                chain.extend(Plain(value) for value in self.paragraphs(text, options))
            pending = ""
        for component in result.chain or []:
            if isinstance(component, Plain):
                pending += component.text
            else:
                flush()
                chain.append(component)
        flush()
        maximum = options["max_segments"] if split else 0
        text_count = sum(isinstance(c, Plain) for c in chain)
        if maximum and text_count > maximum:
            # 按相邻段均匀合并，保留全部正文；媒体所在的位置不能被挪走。
            index, last_bucket, compact = 0, -1, []
            for component in chain:
                if not isinstance(component, Plain):
                    compact.append(component)
                    continue
                bucket = index * maximum // text_count
                if compact and isinstance(compact[-1], Plain) and bucket == last_bucket:
                    joiner = "\n\n" if self.structured(compact[-1].text + component.text) else " "
                    compact[-1].text += joiner + component.text
                else:
                    compact.append(component)
                index += 1
                last_bucket = bucket
            chain = compact
        cleanup = raw.get("content_cleanup_rule", "")
        if cleanup and streaming:
            for component in chain:
                if isinstance(component, Plain):
                    component.text = re.sub(cleanup, "", component.text)
        result.chain = chain
        result._at_guard_segments_prepared = True
        return True

    def stream_packets(self, event, result):
        self.prepare(event, result, streaming=True)
        raw = self.config(event)
        if managed_options(raw) is None or raw.get("enable") is not True:
            return [result]
        if event.get_platform_name() in {"qq_official_webhook", "weixin_official_account", "dingtalk"}:
            return [result]
        return [result.derive([component]) for component in result.chain]
