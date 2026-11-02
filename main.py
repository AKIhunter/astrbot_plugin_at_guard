import asyncio
import html
import importlib
import json
import re
import time
from collections import defaultdict, deque
from dataclasses import replace
from pathlib import Path
from sys import maxsize

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart
from astrbot.core.platform.message_type import MessageType

try:  # 作为插件包加载时走相对导入
    from .guard_engine import GateEngine
except ImportError:  # 单文件调试兜底
    from guard_engine import GateEngine


NO_REPLY_TOKEN = "[[ASTRBOT_NO_REPLY]]"
MEANINGFUL_TEXT_RE = re.compile(r"[\w\u4e00-\u9fff]")
PLUGIN_NAME = "astrbot_plugin_at_guard"
BOT_LINE_PREFIX = "[我]: "


class Main(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.recent_messages: dict[str, deque[tuple[float, str]]] = defaultdict(
            lambda: deque(maxlen=30)
        )
        self.replied_fingerprints: dict[tuple[str, str], float] = {}
        self.last_proactive_reply: dict[str, float] = {}
        self.proactive_inflight_until: dict[str, float] = {}
        try:
            self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.participation_stats_path = self.data_dir / "participation_stats.json"
            self.participation_stats = self._load_participation_stats()
        except Exception:
            self.data_dir = None
            self.participation_stats_path = None
            self.participation_stats = self._empty_participation_stats()
        self.last_bot_line: dict[str, str] = {}
        self.last_bot_line_at: dict[str, float] = {}
        self.context_revision: dict[str, int] = defaultdict(int)
        self.engine = GateEngine(self.config, self.data_dir, self.logger)

    def _gate_mode(self) -> str:
        engine = getattr(self, "engine", None)
        return engine.mode() if engine is not None else "off"

    async def initialize(self) -> None:
        """Register the console bridge, then install bot names as wake prefixes."""
        try:
            from .guard_webui import register_webui

            register_webui(self, self.context)
        except Exception as exc:
            self.logger.warning(f"[智能过滤] 控制台注册失败: {exc!r}")

        if self._gate_mode() == "off":
            self.logger.info("[智能过滤] gate_mode=off，插件已解耦，不注册称呼唤醒词")
            return

        try:
            global_config = self.context.get_config()
            configured = global_config.get("wake_prefix", [])
            if isinstance(configured, str):
                wake_prefixes = [configured]
            elif isinstance(configured, list):
                wake_prefixes = configured
            else:
                wake_prefixes = []

            added: list[str] = []
            for name in self._bot_names():
                # Put punctuation-aware variants before the bare name so
                # "龙，出来" reaches the model as "出来", not "，出来".
                for prefix in (
                    f"{name}，",
                    f"{name},",
                    f"{name}：",
                    f"{name}:",
                    name,
                ):
                    if prefix not in wake_prefixes:
                        wake_prefixes.append(prefix)
                        added.append(prefix)
            global_config["wake_prefix"] = wake_prefixes
            if added:
                self.logger.info(
                    f"[智能过滤] 已注册机器人称呼唤醒词：{', '.join(added)}"
                )
        except Exception as exc:
            self.logger.warning(f"[智能过滤] 注册机器人称呼失败: {exc!r}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=maxsize)
    async def apply_hard_group_rules(self, event: AstrMessageEvent):
        """Record context, handle empty self mentions, and recognize bot names."""
        if self._gate_mode() == "off":
            return

        group_key = self._group_key(event)
        self._record_incoming(event, group_key)

        targets = self._mention_targets(event)
        self_id = str(event.get_self_id())
        mentions_self = self_id in targets
        mentions_other = self._mentions_other_user(targets, self_id)

        # Rule 1 has precedence over nickname matching.
        if mentions_other and not mentions_self:
            return

        if mentions_self and not mentions_other and not self._has_concrete_content(event):
            if self._empty_mention_mode() == "persona":
                event.set_extra("_at_guard_empty_mention_persona", True)
                yield event.request_llm(prompt=self._empty_mention_persona_prompt())
                event.stop_event()
                return
            yield event.plain_result(
                str(self.config.get("empty_mention_reply", "嗯？有事就说。"))
            )
            event.stop_event()
            return

        if not targets and self._starts_with_bot_name(event.message_str):
            event.is_wake = True
            event.is_at_or_wake_command = True

    @filter.on_llm_request(priority=10_000)
    async def guard_llm_request(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        if not self._applies_to_event(event):
            return

        mode = self._gate_mode()
        if mode == "off":
            # 开关式解耦：不判定、不拦截、不记录，等同插件未挂载。
            return

        is_group = event.get_message_type() == MessageType.GROUP_MESSAGE
        group_key = self._group_key(event)
        targets = self._mention_targets(event)
        self_id = str(event.get_self_id())
        mentions_self = self_id in targets
        mentions_other = self._mentions_other_user(targets, self_id)

        # Rules 1 and 2: only mentioning others is a hard stop; mentioning the
        # bot and others together is allowed.
        if mentions_other and not mentions_self:
            self.logger.debug("Blocked a group LLM request mentioning only other users.")
            event.stop_event()
            return

        now = time.monotonic()
        fingerprint = self._fingerprint(event.message_str)
        duplicate_window = max(
            0, int(self.config.get("duplicate_window_seconds", 120))
        )
        self._prune_fingerprints(now, duplicate_window)
        if (
            fingerprint
            and (group_key, fingerprint) in self.replied_fingerprints
            and now - self.replied_fingerprints[(group_key, fingerprint)]
            < duplicate_window
        ):
            self.logger.debug("Blocked a recently answered duplicate group message.")
            event.stop_event()
            return

        # Explicit @, reply-to-bot, wake prefix, and bot-name calls bypass the
        # proactive participation gate.
        if is_group and event.is_at_or_wake_command:
            event.set_extra("_at_guard_fingerprint", fingerprint)
            return

        cooldown = max(0.0, float(self.config.get("proactive_cooldown_seconds", 8)))
        if now - self.last_proactive_reply.get(group_key, 0.0) < cooldown:
            event.stop_event()
            return

        if now < self.proactive_inflight_until.get(group_key, 0.0):
            event.stop_event()
            return

        self.proactive_inflight_until[group_key] = now + 30.0
        event.set_extra("_at_guard_proactive", True)
        event.set_extra("_at_guard_group_key", group_key)
        event.set_extra("_at_guard_fingerprint", fingerprint)
        if event.get_extra("_at_guard_recorded_at") is None:
            self._record_incoming(event, group_key)

        wait_seconds = max(
            0.0, min(5.0, float(self.config.get("fragment_wait_seconds", 1.2)))
        )
        if wait_seconds:
            # 先等碎片，再取流水，判定时才能看到同一句话的后半截。
            await asyncio.sleep(wait_seconds)

        context_lines = self._recent_context(
            group_key, max_messages=self.engine.context_messages(),
            exclude_timestamp=event.get_extra("_at_guard_recorded_at"),
        )
        revision = self.context_revision.get(group_key, 0)
        decision = await self.engine.decide(
            group_key=group_key,
            current_text=event.message_str,
            context_lines=context_lines,
            bot_line=self._fresh_bot_line(group_key),
            is_private=not is_group,
            resolve_provider=lambda: self._resolve_classifier_provider(event),
            message_meta=self._message_meta(event),
            resolve_memory=lambda: self._recall_gate_memory(event),
            record_result=False,
        )
        # 模型看到的是调用前的快照；调用期间新到的群消息使该结果过时。
        event.set_extra("_at_guard_judged_revision", revision)
        stale_reason = self._stale_reason(event, group_key)
        if stale_reason and decision.action == "reply":
            decision = replace(decision, action="silence", reason=stale_reason)
        self.engine._record(decision)
        event.set_extra("_at_guard_confidence_score", decision.score)
        event.set_extra("_at_guard_confidence_threshold", decision.threshold)
        event.set_extra("_at_guard_decision", decision.as_dict())

        if mode == "enforce" and decision.action == "silence":
            # 关键：判完就拦。不拦的话主模型会完整生成一遍再被丢掉。
            self.proactive_inflight_until.pop(group_key, None)
            self.engine.note_generation_skipped()
            self.engine.write_log(
                decision,
                group_key=group_key,
                mode=mode,
                generation_skipped=True,
                model=self.engine.provider_label(),
            )
            self._record_participation(
                decision.score, decision.threshold, False, decision.reason
            )
            self.logger.info(
                f"[智能过滤] 参与适合度 {decision.score}/100，阈值 {decision.threshold}："
                f"静默（{decision.reason}，未调用主模型）"
            )
            event.stop_event()
            return

        self.engine.write_log(
            decision,
            group_key=group_key,
            mode=mode,
            generation_skipped=False,
            model=self.engine.provider_label(),
        )
        if mode == "enforce":
            # 累计统计沿用 2.6 的口径（放行也要记一条），控制台的历史卡片才不会冻住。
            self._record_participation(
                decision.score, decision.threshold, True, decision.reason
            )
        if mode == "legacy":
            event.set_extra("_at_guard_strict_gate", True)

        # 补查的记忆原样交给主模型，避免闸门查了一遍、生成时又猜背景。
        if decision.memory_lines:
            req.extra_user_content_parts.append(
                TextPart(text="<at_guard_memory untrusted=\"true\">\n"
                         + json.dumps(list(decision.memory_lines), ensure_ascii=False)
                         + "\n</at_guard_memory>").mark_as_temp()
            )
        req.extra_user_content_parts.append(
            TextPart(text=(
                "这是普通自主参与机会，请先结合最近群聊确认交流对象和话题仍需要回应。"
                "不要代替群友说话、重复已有答案或在话题结束后附和。"
                "若仍缺少关键背景，或无法核实需要的历史/外部事实，不要编造，"
                "只返回[[ASTRBOT_NO_REPLY]]。"
            )).mark_as_temp()
        )
        if wait_seconds:
            recorded_at = float(event.get_extra("_at_guard_recorded_at", now))
            fragments = self._fragments_since(group_key, recorded_at)
            if len(fragments) > 1:
                req.extra_user_content_parts.append(
                    TextPart(
                        text=(
                            "<recent_group_fragments>\n"
                        + json.dumps(fragments, ensure_ascii=False)
                            + "\n</recent_group_fragments>"
                        )
                    ).mark_as_temp()
                )

        if context_lines:
            req.extra_user_content_parts.append(
                TextPart(
                    text=(
                        "<recent_group_context untrusted=\"true\">\n"
                        + json.dumps(
                            self._recent_context(
                                group_key,
                                max_messages=max(
                                    2,
                                    min(
                                        30,
                                        int(
                                            self.config.get(
                                                "participation_context_messages", 12
                                            )
                                        ),
                                    ),
                                ),
                            ), ensure_ascii=False
                        )
                        + "\n</recent_group_context>"
                    )
                ).mark_as_temp()
            )

    async def _resolve_classifier_provider(self, event: AstrMessageEvent):
        """判定模型：优先用配置里钉死的 provider，避免跟着会话漂到大模型上。"""
        provider_id = self.engine.provider_id()
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider is not None:
                return provider
            self.logger.warning(
                f"[智能过滤] 判定模型 {provider_id} 不存在，回退到会话当前模型"
            )
        return await self.context.get_using_provider_async(event.unified_msg_origin)

    async def at_guard_score(self, text: str) -> dict | None:
        """对外打分入口：供其他插件（如 congling 醒后补课）耦合调用。

        对一段文本做「该不该回」的打分，返回 {"score": 0~100}。不依赖 event，
        用配置里钉死的判定模型；拿不到模型 / 判定失败返回 None（调用方降级）。

        只在 gate_mode != off（即 at_guard 生效）时提供有效结果。
        """
        if self._gate_mode() == "off":
            return None
        text = (text or "").strip()
        if not text:
            return None
        provider_id = self.engine.provider_id()
        if not provider_id:
            return None
        try:
            provider = self.context.get_provider_by_id(provider_id)
        except Exception:
            provider = None
        if provider is None:
            return None
        # 复用判定模型，dry_run 不落统计（避免污染 at_guard 自己的分布）
        try:
            async def resolve():
                return provider

            decision = await self.engine.decide(
                group_key="__external__",
                current_text=text,
                context_lines=[],
                bot_line="",
                is_private=True,
                resolve_provider=resolve,
                dry_run=True,
            )
        except Exception as exc:
            self.logger.debug(f"[智能过滤] 对外打分失败: {exc!r}")
            return None
        if decision.source == "error":
            return None
        # 旧调用方只看score，所以必须同步表达静默，不能漏出原始高分。
        return {"score": decision.score if decision.action == "reply" else 0}

    @filter.on_llm_response(priority=10_000)
    async def suppress_no_reply_and_track(
        self,
        event: AstrMessageEvent,
        response: LLMResponse,
    ) -> None:
        if not self._applies_to_event(event):
            return

        group_key = str(
            event.get_extra("_at_guard_group_key", self._group_key(event))
        )
        is_proactive = bool(event.get_extra("_at_guard_proactive", False))
        strict_gate = bool(event.get_extra("_at_guard_strict_gate", False))
        threshold = self._clamp_confidence(
            event.get_extra(
                "_at_guard_confidence_threshold",
                self._participation_threshold(),
            )
        )
        score = self._clamp_confidence(
            event.get_extra("_at_guard_confidence_score", 0)
        )
        # 无论成功失败都要解锁，否则一次生成异常会把群静默 30 秒。
        if is_proactive:
            self.proactive_inflight_until.pop(group_key, None)

        if response is None:
            return

        if is_proactive and self._gate_mode() == "enforce":
            stale_reason = self._stale_reason(event, group_key)
            if stale_reason:
                self._clear_llm_response(response)
                self._record_delivery_block(event, stale_reason)
                return

        text = (response.completion_text or "").strip()
        if NO_REPLY_TOKEN in text:
            self._clear_llm_response(response)
            if is_proactive and self._gate_mode() == "enforce":
                self._record_delivery_block(event, "no_reply")
            self._record_participation(0, threshold, False, "legacy_no_reply")
            self.logger.debug("Suppressed a proactive NO_REPLY decision.")
            return

        if strict_gate:
            saved = event.get_extra("_at_guard_decision", {}) or {}
            if score < threshold or saved.get("action") == "silence":
                self._clear_llm_response(response)
                self._record_participation(score, threshold, False, "below_threshold")
                self.logger.info(
                    f"[智能过滤] 参与适合度 {score}/100，阈值 {threshold}：静默"
                )
                return
            if not text:
                self._clear_llm_response(response)
                self._record_participation(score, threshold, False, "empty_reply")
                self.logger.info(
                    f"[智能过滤] 参与适合度 {score}/100，阈值 {threshold}：无正文，静默"
                )
                return
            self._record_participation(score, threshold, True, "allowed")
            self.logger.info(
                f"[智能过滤] 参与适合度 {score}/100，阈值 {threshold}：放行"
            )

        if not text:
            return

        # 生成成功不代表发送成功。发言、冷却与去重移到after_message_sent记录。

    @filter.on_decorating_result(priority=-maxsize)
    async def recheck_before_send(self, event: AstrMessageEvent):
        if not self._applies_to_event(event) or self._gate_mode() != "enforce":
            return
        if not event.get_extra("_at_guard_proactive", False):
            return
        group_key = self._group_key(event)
        reason = event.get_extra("_at_guard_delivery_blocked") or self._stale_reason(event, group_key)
        if reason:
            result = event.get_result()
            if result is not None:
                result.chain = []
            self._record_delivery_block(event, str(reason))
            self.proactive_inflight_until.pop(group_key, None)
            event.stop_event()
            return
        result = event.get_result()
        chain = getattr(result, "chain", None) or []
        event.set_extra("_at_guard_sent_text", " ".join(
            component.text for component in chain if isinstance(component, Plain)
        ))
        event.set_extra("_at_guard_sent_has_content", bool(chain))

    @filter.after_message_sent()
    async def track_sent_message(self, event: AstrMessageEvent):
        if not self._applies_to_event(event) or self._gate_mode() == "off":
            return
        result = event.get_result()
        chain = getattr(result, "chain", None) or []
        text = event.get_extra("_at_guard_sent_text") or " ".join(
            component.text for component in chain if isinstance(component, Plain)
        )
        if not chain and not event.get_extra("_at_guard_sent_has_content", False):
            return
        group_key = self._group_key(event)
        now = time.monotonic()
        if event.get_extra("_at_guard_proactive", False):
            self.last_proactive_reply[group_key] = now
            self.proactive_inflight_until.pop(group_key, None)
        if event.get_message_type() == MessageType.GROUP_MESSAGE:
            spoken = " ".join(text.split())[:300] or "[机器人发送了媒体，内容未解析]"
            self.last_bot_line[group_key] = spoken
            self.last_bot_line_at[group_key] = now
            self.recent_messages[group_key].append((now, f"{BOT_LINE_PREFIX}{spoken}"))
            self.context_revision[group_key] += 1
        fingerprint = event.get_extra("_at_guard_fingerprint", "")
        if fingerprint:
            self.replied_fingerprints[(group_key, str(fingerprint))] = now

    def _record_delivery_block(self, event: AstrMessageEvent, reason: str):
        if event.get_extra("_at_guard_delivery_blocked"):
            return
        event.set_extra("_at_guard_delivery_blocked", reason)
        # 单独记发送阶段，不伪装成一次新的模型判定或重复累计放行/静默。
        self.engine.log.add({
            "ts": int(time.time()), "gid": self._group_key(event),
            "mode": self._gate_mode(), "src": "delivery",
            "action": "silence", "reason": reason,
            "score": event.get_extra("_at_guard_confidence_score", 0),
            "th": event.get_extra("_at_guard_confidence_threshold", self.engine.threshold()),
            "gen_skipped": False,
        })

    def _stale_reason(self, event: AstrMessageEvent, group_key: str) -> str:
        started = event.get_extra("_at_guard_recorded_at")
        if started is not None and time.monotonic() - float(started) > self.engine.reply_max_age():
            return "message_expired"
        revision = event.get_extra("_at_guard_judged_revision")
        if revision is not None and revision != self.context_revision.get(group_key, 0):
            return "context_changed"
        return ""

    def _fresh_bot_line(self, group_key: str) -> str:
        stamp = self.last_bot_line_at.get(group_key)
        if stamp is None or time.monotonic() - stamp > self.engine.context_max_age():
            return ""
        return self.last_bot_line.get(group_key, "")

    def _message_meta(self, event: AstrMessageEvent) -> dict:
        def read(name, default=""):
            method = getattr(event, name, None)
            return str(method() or default) if callable(method) else default
        replies = []
        media = []
        getter = getattr(event, "get_messages", None)
        for component in getter() if callable(getter) else []:
            if isinstance(component, Reply):
                replies.append({
                    "message_id": str(component.id),
                    "sender_id": str(component.sender_id or ""),
                    "sender_name": str(component.sender_nickname or "")[:50],
                    "text": str(component.message_str or component.text or "")[:300],
                    "sent_at": component.time or None,
                })
            elif not isinstance(component, (At, Plain)):
                media.append(type(component).__name__)
        stamp = event.get_extra("_at_guard_recorded_at")
        return {
            "sender_id": read("get_sender_id"),
            "sender_name": read("get_sender_name")[:50],
            "bot_id": read("get_self_id"),
            "bot_names": self._bot_names(),
            "reply_to": replies[:2],
            "unread_media": media[:5],
            "age_seconds": round(max(0, time.monotonic() - float(stamp)), 1) if stamp else 0,
        }

    def _record_incoming(self, event: AstrMessageEvent, group_key: str):
        now = time.monotonic()
        event.set_extra("_at_guard_recorded_at", now)
        data = self._message_meta(event)
        data = {"text": (event.message_str or "").strip()[:800], **data}
        if not data["text"] and data["unread_media"]:
            data["text"] = "[媒体消息，内容未解析]"
        self.recent_messages[group_key].append((now, json.dumps(data, ensure_ascii=False)))
        self.context_revision[group_key] += 1

    async def _recall_gate_memory(self, event: AstrMessageEvent) -> list[str]:
        metadata = self.context.get_registered_star("astrbot_plugin_livingmemory")
        if metadata is None or not metadata.activated or metadata.star_cls is None:
            return []
        instance = metadata.star_cls
        initializer = getattr(instance, "initializer", None)
        engine = getattr(initializer, "memory_engine", None)
        if engine is None:
            return []
        # 使用记忆插件自己的身份解析器，避免另造人格/群标识映射。
        package = type(instance).__module__.rsplit(".", 1)[0]
        utils = importlib.import_module(f"{package}.core.utils")
        persona_id = await utils.get_persona_id(self.context, event)
        session_id = str(event.unified_msg_origin or "")
        if not persona_id or not session_id:
            return []  # 身份不明时绝不能退到全库检索。
        results = await engine.search_memories(
            query=(event.message_str or "")[:800], k=3,
            session_id=session_id, persona_id=persona_id,
        )
        lines = []
        for result in results:
            meta = result.metadata or {}
            # 再检查返回值的范围，不能依靠提示词隔离跨群或跨人格内容。
            if meta.get("session_id") != session_id or meta.get("persona_id") != persona_id:
                continue
            content = str(result.content or "").strip()
            if content:
                lines.append(content[:400])
        return lines[:3]

    def _starts_with_bot_name(self, text: str) -> bool:
        normalized = text.lstrip()
        return any(normalized.startswith(name) for name in self._bot_names())

    def _bot_names(self) -> list[str]:
        names = self.config.get("bot_names", ["恰比姐", "恰比"])
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, list):
            return []
        return [
            str(name).strip()
            for name in names
            if isinstance(name, str) and str(name).strip()
        ]

    def _empty_mention_mode(self) -> str:
        mode = str(self.config.get("empty_mention_mode", "fixed") or "fixed")
        return mode if mode in {"fixed", "persona"} else "fixed"

    def _empty_mention_persona_prompt(self) -> str:
        default = (
            "群友刚才只@了你，没有附带其他内容。请严格沿用当前会话已经选择的"
            "AstrBot人格，用符合该人格身份、关系、语气和说话习惯的一句简短自然话回应。"
            "不要解释任务，不要提到AI、系统、人设或提示词，也不要替用户虚构意图。"
        )
        prompt = str(self.config.get("empty_mention_persona_prompt", default) or "").strip()
        return prompt or default

    def _participation_threshold(self) -> int:
        return self._clamp_confidence(
            self.config.get("participation_confidence_threshold", 65)
        )

    def _applies_to_event(self, event: AstrMessageEvent) -> bool:
        message_type = event.get_message_type()
        if message_type == MessageType.GROUP_MESSAGE:
            return True
        if message_type == MessageType.FRIEND_MESSAGE:
            return bool(self.config.get("apply_to_private", False))
        return False

    @staticmethod
    def _clamp_confidence(value) -> int:
        try:
            return max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return 65

    @staticmethod
    def _clear_llm_response(response: LLMResponse) -> None:
        response.completion_text = ""
        result_chain = getattr(response, "result_chain", None)
        chain = getattr(result_chain, "chain", None)
        if isinstance(chain, list):
            chain.clear()

    @staticmethod
    def _empty_participation_stats() -> dict:
        return {
            "total": 0,
            "score_sum": 0,
            "average": 0.0,
            "allowed": 0,
            "blocked": 0,
            # 注意：不再保留 blocked_scores / blocked_median。那批分数是被阈值
            # 右删失的样本（>= 阈值的根本进不来），中位数必然贴着阈值下方并随
            # 阈值一起漂，无法用于调阈值。完整分布改由 guard_engine 的
            # score_distribution() 基于 decisions.jsonl 全量统计。
            "buckets": {
                "0-20": 0,
                "21-40": 0,
                "41-60": 0,
                "61-75": 0,
                "76-90": 0,
                "91-100": 0,
            },
            "last": {},
        }

    def _load_participation_stats(self) -> dict:
        path = self.participation_stats_path
        if path is None or not path.exists():
            return self._empty_participation_stats()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return self._empty_participation_stats()
        default = self._empty_participation_stats()
        if not isinstance(raw, dict):
            return default
        default.update({key: raw[key] for key in default if key in raw})
        if not isinstance(default.get("buckets"), dict):
            default["buckets"] = self._empty_participation_stats()["buckets"]
        return default

    @staticmethod
    def _confidence_bucket(score: int) -> str:
        if score <= 20:
            return "0-20"
        if score <= 40:
            return "21-40"
        if score <= 60:
            return "41-60"
        if score <= 75:
            return "61-75"
        if score <= 90:
            return "76-90"
        return "91-100"

    def _record_participation(
        self,
        score: int,
        threshold: int,
        allowed: bool,
        reason: str,
    ) -> None:
        stats = getattr(self, "participation_stats", None)
        if not isinstance(stats, dict):
            stats = self._empty_participation_stats()
            self.participation_stats = stats
        stats["total"] = int(stats.get("total", 0)) + 1
        stats["score_sum"] = int(stats.get("score_sum", 0)) + score
        stats["average"] = round(stats["score_sum"] / stats["total"], 2)
        key = "allowed" if allowed else "blocked"
        stats[key] = int(stats.get(key, 0)) + 1
        buckets = stats.setdefault("buckets", {})
        bucket = self._confidence_bucket(score)
        buckets[bucket] = int(buckets.get(bucket, 0)) + 1
        stats["last"] = {
            "at": int(time.time()),
            "score": score,
            "threshold": threshold,
            "allowed": allowed,
            "reason": reason,
        }
        path = getattr(self, "participation_stats_path", None)
        if path is None:
            return
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(stats, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception as exc:
            self.logger.debug(f"Failed to persist participation stats: {exc!r}")

    @staticmethod
    def _clamp_confidence(value) -> int:
        try:
            return max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return 65

    @filter.command("智能过滤统计")
    async def participation_stats_command(self, event: AstrMessageEvent):
        stats = getattr(self, "participation_stats", self._empty_participation_stats())
        total = int(stats.get("total", 0))
        average = float(stats.get("average", 0.0))
        allowed = int(stats.get("allowed", 0))
        blocked = int(stats.get("blocked", 0))
        threshold = self._participation_threshold()
        dist = self.engine.score_distribution(self.engine.provider_label())
        quantiles = dist.get("quantiles", {}) or {}
        q_text = (
            f"P50 {quantiles.get('p50')} / P75 {quantiles.get('p75')} / P90 {quantiles.get('p90')}"
            if dist.get("samples")
            else "暂无分布"
        )
        today = self.engine.snapshot()
        mode_label = {
            "enforce": "硬闸门",
            "shadow": "观察模式",
            "legacy": "旧行为",
            "off": "已解耦",
        }.get(self._gate_mode(), self._gate_mode())
        yield event.plain_result(
            f"智能过滤（{mode_label}）：阈值 {threshold}/100；"
            f"累计判断 {total} 次，平均 {average:.1f}；"
            f"放行 {allowed}，静默 {blocked}。\n"
            f"分数分布：{q_text}。\n"
            f"今日：判断 {today.get('judged', 0)} 次（模型 {today.get('model_calls', 0)}，"
            f"规则拦下 {today.get('rule_blocked', 0)}），"
            f"放行 {today.get('allowed', 0)}，静默 {today.get('silenced', 0)}，"
            f"省下生成 {today.get('gen_skipped', 0)} 次。"
        )

    @staticmethod
    def _mention_targets(event: AstrMessageEvent) -> set[str]:
        return {
            str(component.qq)
            for component in event.get_messages()
            if isinstance(component, At)
        }

    @staticmethod
    def _mentions_other_user(targets: set[str], self_id: str) -> bool:
        return any(target not in {self_id, "all"} for target in targets)

    @staticmethod
    def _has_concrete_content(event: AstrMessageEvent) -> bool:
        for component in event.get_messages():
            if isinstance(component, At):
                continue
            if isinstance(component, Plain):
                if MEANINGFUL_TEXT_RE.search(component.text or ""):
                    return True
                continue
            return True
        return False

    @staticmethod
    def _group_key(event: AstrMessageEvent) -> str:
        return str(event.get_group_id() or event.unified_msg_origin)

    @staticmethod
    def _fingerprint(text: str) -> str:
        return " ".join((text or "").casefold().split())

    def _fragments_since(self, group_key: str, since: float) -> list[str]:
        return [
            text
            for timestamp, text in self.recent_messages.get(group_key, ())
            if timestamp >= since and text.strip() and not text.startswith(BOT_LINE_PREFIX)
        ]

    def _recent_context(self, group_key: str, max_messages: int,
                        exclude_timestamp: float | None = None) -> list[str]:
        """Return bounded, escaped chat lines so transcript text cannot alter the gate."""
        if max_messages <= 0:
            return []
        rows = list(self.recent_messages.get(group_key, ()))
        now = time.monotonic()
        rows = [(stamp, raw) for stamp, raw in rows
                if now - stamp <= self.engine.context_max_age() and stamp != exclude_timestamp]
        newest_first: list[str] = []
        budget = 2400
        for stamp, raw in reversed(rows[-max_messages:]):
            line = f"[{max(0, int(now - stamp))}秒前] " + html.escape(
                " ".join((raw or "").split())
            )[:600]
            if not line:
                continue
            if len(line) > budget:
                line = line[:budget]
            newest_first.append(line)
            budget -= len(line)
            if budget <= 0:
                break
        newest_first.reverse()
        return newest_first

    def _prune_fingerprints(self, now: float, window: int) -> None:
        if window <= 0:
            self.replied_fingerprints.clear()
            return
        expired = [
            key
            for key, timestamp in self.replied_fingerprints.items()
            if now - timestamp >= window
        ]
        for key in expired:
            self.replied_fingerprints.pop(key, None)
