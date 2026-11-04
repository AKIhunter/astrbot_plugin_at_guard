"""发送节奏与撤回保护。只包装当前事件，不修改平台或其他会话。"""
from __future__ import annotations

import asyncio
import copy
import math
import re
import time
import weakref
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass

from astrbot.api.message_components import At, Plain, Reply

OWNER_KEY = "_at_guard_reply_owner"
RECALLED_KEY = "_at_guard_source_recalled"


@dataclass
class RecallRecord:
    created: float
    recalled: asyncio.Event
    batch: object = None

    def __getitem__(self, key):
        return getattr(self, key)


def number(config, key, default, low, high):
    try:
        value = float(config.get(key, default))
        if not math.isfinite(value):
            return default
        return max(low, min(high, value))
    except (TypeError, ValueError, OverflowError):
        return default


def enabled(config, key):
    return config.get(key, False) is True


class ReplyDelivery:
    TTL = 3600.0
    CAPACITY = 4096

    def __init__(self, config, logger):
        self.config = config
        self.logger = logger
        # key=(完整会话, 平台原消息ID)，notice中的随机事件ID不能用于取消。
        self.records = OrderedDict()
        # 容量清理仅清理索引；仍被事件持有的记录继续接收撤回，不漏取消。
        self.active_records = weakref.WeakValueDictionary()
        self.tasks = set()
        self.closed = False

    @staticmethod
    def raw(event):
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        return raw if isinstance(raw, Mapping) else {}

    def key(self, event, message_id=None):
        if message_id is None:
            message_id = getattr(getattr(event, "message_obj", None), "message_id", "")
        scope = str(getattr(event, "unified_msg_origin", "") or "")
        mid = str(message_id or "").strip()
        return (scope, mid) if scope and mid and mid not in {"0", "None", "null"} else None

    def _record(self, key):
        now = time.monotonic()
        while self.records:
            first, row = next(iter(self.records.items()))
            if now - row["created"] <= self.TTL:
                break
            self.records.pop(first)
        if key not in self.records:
            row = self.active_records.get(key)
            self.records[key] = row if row is not None else RecallRecord(now, asyncio.Event())
            self.active_records[key] = self.records[key]
        while len(self.records) > self.CAPACITY:
            self.records.popitem(last=False)
        return self.records[key]

    def notice(self, event):
        raw = self.raw(event)
        if raw.get("post_type") != "notice" or raw.get("notice_type") not in {
            "group_recall", "friend_recall",
        }:
            return False
        if not self.recall_enabled(event, raw.get("notice_type") == "group_recall"):
            # 通知始终停止，不把撤回通知送入人格或主模型。
            event.stop_event()
            return True
        key = self.key(event, raw.get("message_id"))
        if key:
            row = self.active_records.get(key)
            # 只认监听后且安静期截止前的撤回，不让生成/打字延迟延长保护。
            if row and row.batch is not None and row.batch.clock() < row.batch.deadline:
                row["recalled"].set()
        # 撤回通知不是一条新用户消息，不交给人格/记忆/主模型。
        event.stop_event()
        return True

    def recall_enabled(self, event, group=None):
        group = bool(event.get_group_id()) if group is None else group
        return enabled(self.config, "reply_recall_group" if group else "reply_recall_private")

    def owns_expression(self):
        return self.config.get("expression_takeover_enabled", True) is True

    def recalled(self, event):
        if event.get_extra(RECALLED_KEY, False):
            return True
        # 合并输入的任意原消息撤回，都必须取消整个尚未发送的回复。
        for source in (event, *event.get_extra("_at_guard_fragment_sources", ())):
            row = source.get_extra("_at_guard_recall_record")
            if source.get_extra(RECALLED_KEY, False) or (row and row["recalled"].is_set()):
                event.set_extra(RECALLED_KEY, True)
                return True
        return False

    def block(self, event):
        event.set_extra(RECALLED_KEY, True)
        event.set_extra("_at_guard_delivery_blocked", "message_recalled")
        result = event.get_result()
        if result is not None:
            result.chain = []
            if hasattr(result, "async_stream"):
                result.async_stream = None
        event.stop_event()

    async def pause(self, event, seconds):
        if self.closed:
            raise asyncio.CancelledError
        if self.recalled(event):
            self.block(event)
            return False
        if seconds > 0:
            rows = [source.get_extra("_at_guard_recall_record") for source in
                    (event, *event.get_extra("_at_guard_fragment_sources", ()))]
            waits = [asyncio.create_task(row["recalled"].wait()) for row in rows if row]
            if waits:
                try:
                    await asyncio.wait(waits, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in waits:
                        task.cancel()
                    await asyncio.gather(*waits, return_exceptions=True)
            else:
                await asyncio.sleep(seconds)
        if self.closed:
            raise asyncio.CancelledError
        if self.recalled(event):
            self.block(event)
            return False
        return True

    async def prepare(self, event):
        if self.notice(event):
            return False
        if self.raw(event).get("post_type") == "notice":
            return True
        if event.get_extra("_at_guard_received_at") is None:
            event.set_extra("_at_guard_received_at", time.monotonic())
        if event.get_extra("_at_guard_delivery_prepared", False):
            return not self.recalled(event)
        recall = self.recall_enabled(event)
        style = self.owns_expression()
        segments = getattr(self, "segments", None)
        segmenting = bool(segments and segments.active(event))
        if not recall and not style and not segmenting:
            return True
        event.set_extra("_at_guard_delivery_prepared", True)
        if recall and (key := self.key(event)):
            row = self._record(key)
            row.batch = getattr(event.get_extra("_at_guard_fragment_ticket"), "batch", None)
            event.set_extra("_at_guard_recall_record", row)
        event.set_extra("_at_guard_expression_received", time.monotonic())
        # 直接event.send只复核撤回；主模型最终输出才执行表达计划。
        if self.patch(event) and style:
            event.set_extra(OWNER_KEY, "at_guard")
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            return await self.pause(event, 0)
        finally:
            self.tasks.discard(task)

    @staticmethod
    def derive(message, chain):
        derive = getattr(message, "derive", None)
        if callable(derive):
            return derive(list(chain))
        result = copy.copy(message)
        result.chain = list(chain)
        return result

    def typing_seconds(self, chain, event=None):
        if not enabled(self.config, "reply_typing_enabled") or not self.owns_expression():
            return 0.0
        text = "".join(c.text for c in chain if isinstance(c, Plain))
        chars = len(re.sub(r"\s", "", text))
        return chars / number(self.config, "reply_typing_chars_per_second", 2.5, .5, 20)

    async def typing_pause(self, event, seconds):
        started = time.monotonic()
        previous = event.get_extra("_at_guard_typing_seconds", 0)
        event.set_extra("_at_guard_typing_started", started)
        try:
            return await self.pause(event, seconds)
        finally:
            event.set_extra("_at_guard_typing_seconds", previous + max(0, time.monotonic() - started))
            event.set_extra("_at_guard_typing_started", None)

    def arm(self, event, result, *, streaming=False):
        """登记结果身份；派生链继承局部标记，宿主可自由创建Plain或转换媒体。"""
        if event.get_extra(OWNER_KEY) != "at_guard" or not (
                streaming or getattr(result, "is_llm_result", lambda: False)()):
            return
        if event.get_extra("_at_guard_delivery_plan"):
            return
        original_derive = getattr(result, "derive", None)
        if not callable(original_derive):
            # 未知宿主保留原生完整结果，不认领旧全文或吞正文。
            return
        token = object()
        try:
            def owned_derive(chain=None):
                message = original_derive(chain)
                message._at_guard_delivery_token = token
                return message
            result.derive = owned_derive
            result._at_guard_delivery_token = token
        except (AttributeError, TypeError):
            self.logger.warning("[智能过滤] 当前结果不能登记发送策略，保留原生回复")
            return
        event.set_extra("_at_guard_delivery_plan", {
            "token": token, "semantic_headers": {
                id(c): c for c in result.chain or [] if isinstance(c, (Reply, At))},
            "attempted": {}, "header_sent": False, "failed": False,
        })
        event.set_extra("_at_guard_actual_sent", False)
        event.set_extra("_at_guard_sent_has_content", False)
        event.set_extra("_at_guard_sent_text", "")

    def patch(self, event):
        if event.get_extra("_at_guard_send_patched", False):
            return True
        original = getattr(event, "send", None)
        if not callable(original):
            return False
        # 事件局部锁也挡住同一回复的并行发送，不产生全局群聊锁。
        lock = asyncio.Lock()

        async def wrapped(message, *args, **kwargs):
            task = asyncio.current_task()
            self.tasks.add(task)
            try:
                async with lock:
                    if not await self.pause(event, 0):
                        return
                    plan = event.get_extra("_at_guard_delivery_plan")
                    incoming = getattr(message, "chain", []) or []
                    belongs = bool(plan and getattr(message, "_at_guard_delivery_token", None)
                                   is plan["token"])
                    if not belongs:
                        sent = await original(message, *args, **kwargs)
                        if not plan:
                            await self.note_sent(event, incoming)
                        return sent
                    if plan["failed"]:
                        return
                    # 记录实际发送组件（保留强引用防止ID复用）；重复包只消费未尝试正文。
                    body = [c for c in incoming if not isinstance(c, (Reply, At))]
                    pending = [c for c in body if id(c) not in plan["attempted"]]
                    if not pending:
                        return
                    pending_ids = {id(c) for c in pending}
                    chain = [c for c in incoming if isinstance(c, (Reply, At)) or id(c) in pending_ids]
                    plan["attempted"].update({id(c): c for c in pending})
                    expression = getattr(self, "expression", None)
                    checker = event.get_extra("_at_guard_send_check")
                    reason = checker() if callable(checker) else ""
                    if reason:
                        event.set_extra("_at_guard_delivery_blocked", reason)
                        plan["failed"] = True
                        return
                    if not await self.typing_pause(event, self.typing_seconds(pending, event)):
                        plan["failed"] = True
                        return
                    reason = checker() if callable(checker) else ""
                    if reason:
                        event.set_extra("_at_guard_delivery_blocked", reason)
                        plan["failed"] = True
                        return
                    if expression:
                        chain = expression.before_send(event, chain, plan)
                    try:
                        sent = await original(self.derive(message, chain), *args, **kwargs)
                    except BaseException:
                        plan["failed"] = True
                        raise
                    if any(not isinstance(c, (Reply, At)) and type(c).__name__ != "Record"
                           for c in chain):
                        plan["header_sent"] = True
                    await self.note_sent(event, chain)
                    return sent
            finally:
                self.tasks.discard(task)

        try:
            event.send = wrapped
            event.set_extra("_at_guard_send_patched", True)
        except (AttributeError, TypeError):
            self.logger.warning("[智能过滤] 当前事件无法包装发送，表达保留宿主行为")
            return False

        original_streaming = getattr(event, "send_streaming", None)
        if callable(original_streaming):
            async def buffered_stream(stream, *args, **kwargs):
                # 开启接管/撤回保护时等待生成完成，避免边生成边发送越过撤回检查。
                task = asyncio.current_task()
                self.tasks.add(task)
                try:
                    if event.get_extra("_at_guard_stream_consumed", False):
                        return
                    event.set_extra("_at_guard_stream_consumed", True)
                    chain, template = [], None
                    async for chunk in stream:
                        if not await self.pause(event, 0):
                            return
                        template = chunk
                        chain.extend(getattr(chunk, "chain", []) or [])
                    if template is None or not chain:
                        return
                    full = self.derive(template, chain)
                    if event.get_extra(OWNER_KEY) == "at_guard":
                        expression = getattr(self, "expression", None)
                        if expression:
                            expression.clean_chain(full)
                        if not full.chain:
                            return
                        plan = event.get_extra("_at_guard_delivery_plan")
                        if plan is None:
                            self.arm(event, full, streaming=True)
                            plan = event.get_extra("_at_guard_delivery_plan")
                        if plan:
                            full._at_guard_delivery_token = plan["token"]
                    segments = getattr(self, "segments", None)
                    packets = segments.stream_packets(event, full) if segments else [full]
                    for packet in packets:
                        await wrapped(packet)
                finally:
                    self.tasks.discard(task)
            event.send_streaming = buffered_stream
        return True

    async def note_sent(self, event, chain):
        if not chain:
            return
        event.set_extra("_at_guard_actual_sent", True)
        event.set_extra("_at_guard_sent_has_content", True)
        previous = event.get_extra("_at_guard_sent_text", "")
        event.set_extra("_at_guard_sent_text", previous + "".join(
            c.text for c in chain if isinstance(c, Plain)))

    async def close(self):
        self.closed = True
        tasks = [t for t in self.tasks if t is not asyncio.current_task() and not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.records.clear()
        self.active_records.clear()
