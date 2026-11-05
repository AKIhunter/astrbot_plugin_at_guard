"""碎片安静期、单次判断、归档原文及撤回配套的离线时序回归。"""
import asyncio
import json
import time
import unittest
from types import SimpleNamespace

from astrbot.api.message_components import At, Image, Plain, Reply
from guard_fragments import FragmentWait
from guard_webui import EDITABLE, _coerce, _load_schema_subset
from test_context_policy import SequenceProvider, output
from test_gate import gate_config, plugin
from test_reply_delivery import Event, recall as delivery_recall


class Clock:
    now = 0

    def __call__(self):
        return self.now


class FragmentEvent(Event):
    def __init__(self, text="现在的问题", sender="alice", group="1001", mid="1",
                 components=None, explicit=False, scope=None):
        super().__init__(group, mid)
        self.message_str = text
        self.sender = sender
        self.components = components if components is not None else [Plain(text)]
        self.is_at_or_wake_command = explicit
        self.unified_msg_origin = scope or f"qq:{'group' if group else 'friend'}:{group or sender}"

    def get_sender_id(self):
        return self.sender

    def get_messages(self):
        return self.components

    def plain_result(self, text):
        return ("plain", text)

    def request_llm(self, *, prompt):
        return ("llm", prompt)


def request():
    return SimpleNamespace(prompt="宿主已经构造的原始问题", image_urls=[], extra_user_content_parts=[])


def recall(event, mid="1"):
    notice = delivery_recall(event, mid)
    notice.unified_msg_origin = event.unified_msg_origin
    return notice


class QueueTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.queue = FragmentWait(self.clock)

    def observe(self, mid, key=("qq:group:1001", "alice"), explicit=False, text="问"):
        return self.queue.observe(object(), key, {"text": text, "message_id": mid}, 3,
                                  explicit, mid)

    async def test_three_seconds_restart_after_each_same_user_message(self):
        a = self.observe("a", explicit=True)
        self.assertEqual(a.batch.deadline, 3)
        self.clock.now = 2
        b = self.observe("b")
        self.assertEqual(b.batch.deadline, 5)
        self.clock.now = 4
        c = self.observe("c")
        self.assertEqual(c.batch.deadline, 7)
        self.assertTrue(c.batch.explicit)
        self.assertIsNone(await self.queue.wait(a))
        self.assertIsNone(await self.queue.wait(b))
        self.clock.now = 7
        self.assertEqual(await self.queue.wait(c), (a, b, c))
        self.assertIsNone(await self.queue.wait(c))
        self.assertFalse(self.queue.batches)

    async def test_other_user_and_other_scope_never_reset_deadline(self):
        a = self.observe("a")
        self.clock.now = 2
        b = self.observe("b", ("qq:group:1001", "bob"))
        c = self.observe("c", ("telegram:group:1001", "alice"))
        d = self.observe("d", ("qq:friend:alice", "alice"))
        self.assertEqual(a.batch.deadline, 3)
        self.clock.now = 5
        for ticket in (a, b, c, d):
            self.assertEqual(await self.queue.wait(ticket), (ticket,))

    async def test_new_message_at_deadline_starts_next_batch(self):
        a = self.observe("a", explicit=True)
        self.clock.now = 3
        b = self.observe("b")
        self.assertIsNot(a.batch, b.batch)
        self.assertFalse(b.batch.explicit)
        self.assertEqual(await self.queue.wait(a), (a,))
        self.assertIs(self.queue.batches[b.batch.key], b.batch)
        self.clock.now = 6
        self.assertEqual(await self.queue.wait(b), (b,))

    async def test_duplicate_platform_id_does_not_extend_quiet_period(self):
        a = self.observe("a")
        self.clock.now = 2
        duplicate = self.observe("a")
        self.assertEqual(duplicate.reason, "fragment_duplicate")
        self.assertEqual(a.batch.deadline, 3)
        self.assertIsNone(await self.queue.wait(duplicate))
        self.clock.now = 3
        self.assertEqual(await self.queue.wait(a), (a,))
        self.assertEqual(self.observe("a").reason, "fragment_duplicate")

    async def test_message_ids_are_isolated_by_full_scope(self):
        a = self.observe("a")
        b = self.observe("a", ("qq:group:1002", "alice"))
        self.assertIsNot(a.batch, b.batch)
        self.assertFalse(b.reason)

    async def test_waiter_is_released_immediately_when_superseded(self):
        a = self.observe("a")
        task = asyncio.create_task(self.queue.wait(a))
        await asyncio.sleep(0)
        self.clock.now = 1
        b = self.observe("b")
        self.assertIsNone(await asyncio.wait_for(task, 0.2))
        self.clock.now = 4
        self.assertEqual(await self.queue.wait(b), (a, b))

    async def test_cancellation_does_not_replay_partial_batch(self):
        a = self.observe("a")
        task = asyncio.create_task(self.queue.wait(a))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.clock.now = 3
        self.assertIsNone(await self.queue.wait(a))
        self.assertFalse(self.queue.batches)

    async def test_reload_cancels_waiters_and_clears_transient_state(self):
        a = self.observe("a")
        task = asyncio.create_task(self.queue.wait(a))
        await asyncio.sleep(0)
        await self.queue.close()
        self.assertTrue(task.cancelled())
        self.assertFalse(self.queue.batches)
        self.assertFalse(self.queue.seen)
        self.assertEqual(self.observe("b").reason, "fragment_closed")

    async def test_overflow_fails_closed_then_recovers_after_quiet_period(self):
        self.queue.MAX_MESSAGES = 2
        self.observe("a")
        self.observe("b")
        c = self.observe("c")
        self.assertTrue(c.batch.blocked)
        self.assertIsNone(await self.queue.wait(c))
        self.clock.now = 3
        d = self.observe("d")
        self.clock.now = 6
        self.assertEqual(await self.queue.wait(d), (d,))

    async def test_character_limit_does_not_judge_truncated_input(self):
        self.queue.MAX_CHARACTERS = 2
        a = self.observe("a", text="完整问题不能被截断")
        self.assertIsNone(await self.queue.wait(a))
        self.assertFalse(a.batch.items)

    async def test_capacity_rejects_new_session_without_evicting_waiter(self):
        self.queue.CAPACITY = 1
        a = self.observe("a")
        b = self.observe("b", ("other", "bob"))
        self.assertEqual(b.reason, "fragment_capacity")
        self.clock.now = 3
        self.assertEqual(await self.queue.wait(a), (a,))

    async def test_real_quiet_timer_never_finishes_early(self):
        queue = FragmentWait()
        started = time.monotonic()
        a = queue.observe(object(), ("scope", "alice"), {"text": "问"}, 0.02)
        result = await queue.wait(a)
        self.assertEqual(result, (a,))
        self.assertGreaterEqual(time.monotonic() - started, 0.02)


class PipelineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.provider = SequenceProvider(output())
        self.guard = plugin(gate_config(fragment_wait_seconds=3, bot_names=["恰比"]),
                            self.provider)
        self.guard.fragment_wait = FragmentWait(self.clock)
        self.archived = []

    async def incoming(self, event, stamp=0):
        self.clock.now = stamp
        await self.guard.observe_message_fragments(event)
        await self.guard.prepare_reply_delivery(event)
        if event.get_group_id():
            results = [item async for item in self.guard.apply_hard_group_rules(event)]
            self.assertEqual(results, [])
        # 模拟从绫：在淘汰旧触发前逐条记录原始消息，而非合并后的复制文本。
        self.archived.append((event.message_str, list(event.get_messages())))
        return event

    async def finish(self, event):
        ticket = event.get_extra("_at_guard_fragment_ticket")
        if ticket and ticket.batch:
            self.clock.now = ticket.batch.deadline
        return [item async for item in self.guard.coalesce_message_fragments(event)]

    async def judge(self, event):
        req = request()
        await self.guard.guard_llm_request(event, req)
        return req

    async def test_empty_at_then_question_merges_into_one_direct_reply(self):
        a = await self.incoming(FragmentEvent("", components=[At(qq="bot")]))
        b = await self.incoming(FragmentEvent("我想问", mid="2"), 2)
        c = await self.incoming(FragmentEvent("怎么加点", mid="3"), 4)
        await self.finish(a)
        await self.finish(b)
        self.assertTrue(a.stopped and b.stopped)
        self.assertEqual(await self.finish(c), [])
        self.assertFalse(c.stopped)
        self.assertTrue(c.is_at_or_wake_command)
        req = await self.judge(c)
        self.assertEqual(self.provider.prompts, [])
        self.assertEqual(len(c.extra["_at_guard_fragments"]), 3)
        self.assertTrue(c.extra["_at_guard_fragment_judged"])
        self.assertNotIn("_at_guard_empty_mention_persona", c.extra)
        self.assertIn("我想问", req.extra_user_content_parts[0].text)
        self.assertEqual(req.prompt, "宿主已经构造的原始问题")
        self.assertEqual([row[0] for row in self.archived], ["", "我想问", "怎么加点"])
        self.assertEqual(a.message_str, "")
        self.assertIs(a.get_messages()[0], self.archived[0][1][0])
        self.assertEqual(c.message_str, "怎么加点")

    async def test_native_at_is_not_silenced_as_other_talk_or_missing_context(self):
        for reason in ("others_talking", "context_missing"):
            with self.subTest(reason=reason):
                provider = SequenceProvider(output("silent", 20, False, reason))
                self.guard.context.provider = provider
                event = await self.incoming(FragmentEvent(
                    "现在回我", mid=reason, components=[At(qq="bot"), Plain("现在回我")]))
                await self.finish(event)
                await self.judge(event)
                self.assertFalse(event.stopped)
                self.assertEqual(provider.prompts, [])
                self.assertNotIn("_at_guard_decision", event.extra)
                self.assertEqual(self.guard.engine.stats["model_calls"], 0)

    async def test_native_at_works_without_host_wake_flag(self):
        event = await self.incoming(FragmentEvent(
            "现在回我", components=[At(qq="bot"), Plain("现在回我")]))
        await self.finish(event)
        event.is_at_or_wake_command = False
        await self.judge(event)
        self.assertFalse(event.stopped)
        self.assertEqual(self.provider.prompts, [])

    async def test_only_at_other_user_still_blocks_but_bot_and_other_can_reply(self):
        other = FragmentEvent("帮我看看", mid="other", components=[At(qq="bob"), Plain("帮我看看")])
        # 硬规则可能已在归档之前阻断；请求边界也必须阻断，不能靠宿主唤醒标记放行。
        other.is_at_or_wake_command = True
        await self.judge(other)
        self.assertTrue(other.stopped)
        both = await self.incoming(FragmentEvent(
            "帮他看看", mid="both", components=[At(qq="bot"), At(qq="bob"), Plain("帮他看看")]))
        await self.finish(both)
        await self.judge(both)
        self.assertFalse(both.stopped)
        self.assertEqual(self.provider.prompts, [])

    async def test_plain_at_text_still_uses_participation_model(self):
        self.guard.context.provider = SequenceProvider(output("silent", 20, False, "others_talking"))
        event = await self.incoming(FragmentEvent("@恰比 现在回我"))
        await self.finish(event)
        await self.judge(event)
        self.assertTrue(event.stopped)
        self.assertEqual(event.extra["_at_guard_decision"]["reason"], "others_talking")
        self.assertEqual(len(self.guard.context.provider.prompts), 1)

    async def test_native_at_keeps_active_proactive_request_and_duplicate_protection(self):
        self.guard.last_proactive_reply["1001"] = time.monotonic()
        self.guard.config["proactive_cooldown_seconds"] = 60
        self.guard.config["duplicate_window_seconds"] = 120
        self.guard.proactive_inflight_until["1001"] = time.monotonic() + 30
        inflight = self.guard.proactive_inflight_until["1001"]
        event = await self.incoming(FragmentEvent(
            "现在回我", components=[At(qq="bot"), Plain("现在回我")]))
        await self.finish(event)
        await self.judge(event)
        response = SimpleNamespace(completion_text="收到")
        await self.guard.suppress_no_reply_and_track(event, response)
        self.assertEqual(response.completion_text, "收到")
        self.assertFalse(event.stopped)
        self.assertFalse(event.get_extra("_at_guard_proactive", False))
        self.assertEqual(self.guard.proactive_inflight_until["1001"], inflight)
        await self.guard.recheck_before_send(event)
        await event.send(event.result)
        await self.guard.track_sent_message(event)
        repeat = await self.incoming(FragmentEvent(
            "现在回我", mid="repeat", components=[At(qq="bot"), Plain("现在回我")]))
        await self.finish(repeat)
        await self.judge(repeat)
        self.assertTrue(repeat.stopped)
        self.assertEqual(self.provider.prompts, [])

    async def test_native_at_keeps_expiry_and_context_checks_before_sending(self):
        for reason in ("message_expired", "context_changed"):
            with self.subTest(reason=reason):
                event = await self.incoming(FragmentEvent(
                    "现在回我", mid=reason, components=[At(qq="bot"), Plain("现在回我")]))
                await self.finish(event)
                await self.judge(event)
                self.assertFalse(event.stopped)
                if reason == "message_expired":
                    event.set_extra("_at_guard_recorded_at",
                                    time.monotonic() - self.guard.engine.reply_max_age() - 1)
                else:
                    self.guard.context_revision["1001"] += 1
                await self.guard.recheck_before_send(event)
                self.assertTrue(event.stopped)
                self.assertEqual(event.extra["_at_guard_delivery_blocked"], reason)
                self.assertEqual(event.result.chain, [])
                self.assertEqual(self.provider.prompts, [])

    async def test_native_at_ignores_recall_after_fragment_window(self):
        self.guard.config.update(reply_recall_group=True, reply_recall_private=True)
        a = await self.incoming(FragmentEvent(
            "原问题", components=[At(qq="bot"), Plain("原问题")]))
        b = await self.incoming(FragmentEvent("补充", mid="2"), 2)
        await self.finish(a)
        await self.finish(b)
        await self.judge(b)
        self.assertFalse(b.stopped)
        self.assertEqual(self.provider.prompts, [])
        await self.guard.prepare_reply_delivery(recall(a, mid="1"))
        response = SimpleNamespace(completion_text="不能发送的回答")
        await self.guard.suppress_no_reply_and_track(b, response)
        self.assertEqual(response.completion_text, "不能发送的回答")
        await b.send(b.result)
        self.assertTrue(b.sent)

    async def test_native_at_keeps_response_context_and_no_reply_checks(self):
        for reason in ("context_changed", "no_reply"):
            with self.subTest(reason=reason):
                event = await self.incoming(FragmentEvent(
                    "现在回我", mid=reason, components=[At(qq="bot"), Plain("现在回我")]))
                await self.finish(event)
                await self.judge(event)
                if reason == "context_changed":
                    self.guard.context_revision["1001"] += 1
                response = SimpleNamespace(completion_text=(
                    "旧回复" if reason == "context_changed" else "[[ASTRBOT_NO_REPLY]]"))
                await self.guard.suppress_no_reply_and_track(event, response)
                self.assertEqual(response.completion_text, "")
                self.assertEqual(self.provider.prompts, [])

    async def test_name_call_waits_and_can_be_silenced_by_complete_intent(self):
        self.guard.context.provider = SequenceProvider(output("silent", 20, True, "conversation_ended"))
        a = await self.incoming(FragmentEvent("恰比，帮我看看"))
        b = await self.incoming(FragmentEvent("算了，不用回了", mid="2"), 2)
        await self.finish(a)
        await self.finish(b)
        await self.judge(b)
        self.assertTrue(b.stopped)
        self.assertEqual(b.extra["_at_guard_decision"]["reason"], "conversation_ended")
        self.assertEqual(len(self.guard.context.provider.prompts), 1)

    async def test_explicit_batch_bypasses_cooldown_and_preserves_other_inflight(self):
        self.guard.last_proactive_reply["1001"] = time.monotonic()
        self.guard.config["proactive_cooldown_seconds"] = 60
        self.guard.proactive_inflight_until["1001"] = time.monotonic() + 30
        inflight = self.guard.proactive_inflight_until["1001"]
        event = await self.incoming(FragmentEvent("恰比，在吗"))
        await self.finish(event)
        await self.judge(event)
        self.assertFalse(event.stopped)
        self.assertTrue(event.extra["_at_guard_fragment_judged"])
        self.assertFalse(event.get_extra("_at_guard_proactive", False))
        response = SimpleNamespace(completion_text="在呢。")
        await self.guard.suppress_no_reply_and_track(event, response)
        self.assertEqual(self.guard.proactive_inflight_until["1001"], inflight)

    async def test_silent_explicit_batch_does_not_unlock_other_proactive_request(self):
        self.guard.context.provider = SequenceProvider(output("silent", 20, True, "conversation_ended"))
        self.guard.proactive_inflight_until["1001"] = time.monotonic() + 30
        inflight = self.guard.proactive_inflight_until["1001"]
        event = await self.incoming(FragmentEvent("恰比，别回"))
        await self.finish(event)
        await self.judge(event)
        self.assertTrue(event.stopped)
        self.assertEqual(self.guard.proactive_inflight_until["1001"], inflight)

    async def test_ordinary_messages_are_also_coalesced_once(self):
        a = await self.incoming(FragmentEvent("我想问"))
        b = await self.incoming(FragmentEvent("这个任务怎么做", mid="2"), 2)
        await self.finish(a)
        await self.finish(b)
        await self.judge(b)
        self.assertTrue(a.stopped)
        self.assertFalse(b.stopped)
        self.assertTrue(b.extra["_at_guard_proactive"])
        self.assertEqual(len(self.provider.prompts), 1)

    async def test_other_user_not_in_batch_and_does_not_reset_original_deadline(self):
        a = await self.incoming(FragmentEvent("恰比，我想问"))
        await self.incoming(FragmentEvent("别人说话", sender="bob", mid="2"), 2)
        self.assertEqual(a.extra["_at_guard_fragment_ticket"].batch.deadline, 3)
        await self.finish(a)
        req = await self.judge(a)
        self.assertEqual(len(a.extra["_at_guard_fragments"]), 1)
        self.assertNotIn("别人说话", req.extra_user_content_parts[0].text)

    async def test_private_switch_is_independent_of_participation_filter(self):
        self.guard.config["fragment_wait_private"] = True
        a = await self.incoming(FragmentEvent("我想问", group=""))
        b = await self.incoming(FragmentEvent("这个任务怎么做", group="", mid="2"), 2)
        await self.finish(a)
        await self.finish(b)
        req = await self.judge(b)
        self.assertTrue(a.stopped)
        self.assertFalse(b.stopped)
        self.assertIn("我想问", req.extra_user_content_parts[0].text)
        self.assertEqual(self.provider.prompts, [])

    async def test_private_filter_and_fragment_wait_can_both_be_enabled(self):
        self.guard.config.update(fragment_wait_private=True, apply_to_private=True)
        event = await self.incoming(FragmentEvent("我想问任务怎么做", group=""))
        await self.finish(event)
        await self.judge(event)
        self.assertFalse(event.stopped)
        self.assertEqual(len(self.provider.prompts), 1)

    async def test_private_default_does_not_wait_even_when_filter_is_on(self):
        self.guard.config["apply_to_private"] = True
        event = await self.incoming(FragmentEvent(group=""))
        self.assertIsNone(event.get_extra("_at_guard_fragment_ticket"))
        await self.judge(event)
        self.assertEqual(len(self.provider.prompts), 1)

    async def test_private_wait_is_independent_from_shadow_participation(self):
        self.guard.config.update(gate_mode="shadow", fragment_wait_private=True)
        event = await self.incoming(FragmentEvent(group=""))
        await self.finish(event)
        req = await self.judge(event)
        self.assertTrue(req.extra_user_content_parts)
        self.assertFalse(self.provider.prompts)

    async def test_zero_wait_keeps_explicit_call_direct_behavior(self):
        self.guard.config["fragment_wait_seconds"] = 0
        event = await self.incoming(FragmentEvent(explicit=True))
        self.assertIsNone(event.get_extra("_at_guard_fragment_ticket"))
        await self.judge(event)
        self.assertFalse(self.provider.prompts)
        self.assertFalse(event.stopped)

    async def test_empty_at_only_replies_after_quiet_period(self):
        event = await self.incoming(FragmentEvent("", components=[At(qq="bot")]))
        results = await self.finish(event)
        self.assertEqual(results, [("plain", "嗯？有事就说。")])
        self.assertTrue(event.stopped)
        self.assertFalse(self.provider.prompts)

    async def test_empty_at_persona_stays_with_selected_persona_after_wait(self):
        self.guard.config["empty_mention_mode"] = "persona"
        event = await self.incoming(FragmentEvent("", components=[At(qq="bot")]))
        result = await self.finish(event)
        self.assertEqual(result[0][0], "llm")
        self.assertIn("当前会话", result[0][1])
        await self.judge(event)
        self.assertFalse(self.provider.prompts)

    async def test_earlier_image_and_reply_metadata_are_passed_to_model(self):
        image = Image.fromURL("https://example.invalid/fragment.png")
        quoted = Reply(id="old", sender_id="bob", message_str="被引用的问题")
        a = await self.incoming(FragmentEvent("恰比，看看", components=[Plain("恰比，看看"), image, quoted]))
        b = await self.incoming(FragmentEvent("这张图片", mid="2"), 2)
        await self.finish(a)
        await self.finish(b)
        req = await self.judge(b)
        self.assertIn(image.url, req.image_urls)
        self.assertIn("被引用的问题", req.extra_user_content_parts[0].text)
        self.assertEqual(self.guard._gate_images(b, "1001")[0]["message_id"], "1")
        self.assertIs(a.components[1], image)

    async def test_long_earlier_text_is_not_lost_to_current_text_limit(self):
        text = "恰比，" + "背景" * 600 + "末尾关键要求"
        a = await self.incoming(FragmentEvent(text))
        b = await self.incoming(FragmentEvent("请统一回答", mid="2"), 2)
        await self.finish(a)
        await self.finish(b)
        req = await self.judge(b)
        self.assertIn("末尾关键要求", self.provider.prompts[0])
        self.assertIn("末尾关键要求", req.extra_user_content_parts[0].text)

    async def test_new_context_after_judgment_cancels_explicit_generated_reply(self):
        event = await self.incoming(FragmentEvent("恰比，看看这个问题"))
        await self.finish(event)
        await self.judge(event)
        self.guard._record_incoming(FragmentEvent("后续新内容", mid="2"), "1001")
        response = SimpleNamespace(completion_text="过时的回答")
        await self.guard.suppress_no_reply_and_track(event, response)
        self.assertEqual(response.completion_text, "")
        self.assertEqual(event.extra["_at_guard_delivery_blocked"], "context_changed")

    async def test_earlier_source_recall_blocks_complete_batch_before_generation(self):
        self.guard.config.update(reply_recall_group=True, reply_recall_private=True)
        a = await self.incoming(FragmentEvent("恰比，原问题"))
        b = await self.incoming(FragmentEvent("补充", mid="2"), 2)
        await self.guard.prepare_reply_delivery(recall(a, mid="1"))
        await self.finish(a)
        await self.finish(b)
        self.assertTrue(b.stopped)
        self.assertFalse(self.provider.prompts)
        self.assertFalse(b.sent)

    async def test_earlier_recall_after_window_does_not_block_response(self):
        self.guard.config.update(reply_recall_group=True, reply_recall_private=True)
        a = await self.incoming(FragmentEvent("恰比，原问题"))
        b = await self.incoming(FragmentEvent("补充", mid="2"), 2)
        await self.finish(a)
        await self.finish(b)
        await self.judge(b)
        await self.guard.prepare_reply_delivery(recall(a, mid="1"))
        response = SimpleNamespace(completion_text="不能发送的回答")
        await self.guard.suppress_no_reply_and_track(b, response)
        self.assertEqual(response.completion_text, "不能发送的回答")
        await b.send(b.result)
        self.assertTrue(b.sent)

    async def test_notice_and_bot_own_message_do_not_reset_wait(self):
        event = await self.incoming(FragmentEvent("恰比，问题"))
        self.clock.now = 2
        notice = recall(event)
        notice.message_str = "不是用户消息"
        await self.guard.observe_message_fragments(notice)
        own = FragmentEvent(sender="bot", mid="2")
        await self.guard.observe_message_fragments(own)
        self.assertIsNone(notice.get_extra("_at_guard_fragment_ticket"))
        self.assertIsNone(own.get_extra("_at_guard_fragment_ticket"))
        self.assertEqual(event.extra["_at_guard_fragment_ticket"].batch.deadline, 3)

    async def test_recall_registration_does_not_delay_fragment_arrival_clock(self):
        self.guard.config.update(reply_recall_group=True)
        a = FragmentEvent("恰比，我想问")
        await self.guard.observe_message_fragments(a)
        await self.guard.prepare_reply_delivery(a)
        self.clock.now = 2
        b = FragmentEvent("补充问题", mid="2")
        await self.guard.observe_message_fragments(b)
        self.assertEqual(b.extra["_at_guard_fragment_ticket"].batch.deadline, 5)
        self.assertIs(a.extra["_at_guard_fragment_ticket"].batch, b.extra["_at_guard_fragment_ticket"].batch)

    async def test_arrival_cancels_old_reply_before_new_message_processing(self):
        self.guard.config.update(reply_recall_group=True)
        event = await self.incoming(FragmentEvent("恰比，旧问题"))
        await self.finish(event)
        await self.judge(event)
        new = FragmentEvent("更新的问题", mid="2")
        await self.guard.observe_message_fragments(new)
        await self.guard.prepare_reply_delivery(new)
        response = SimpleNamespace(completion_text="旧回答")
        await self.guard.suppress_no_reply_and_track(event, response)
        self.assertEqual(response.completion_text, "")
        self.assertEqual(event.extra["_at_guard_delivery_blocked"], "context_changed")

    async def test_reload_stops_pending_pipeline_without_reply(self):
        event = await self.incoming(FragmentEvent("恰比，问题"))
        waiting = asyncio.create_task(self.guard.coalesce_message_fragments(event).__anext__())
        await asyncio.sleep(0)
        await self.guard.terminate()
        self.assertTrue(waiting.cancelled())
        self.assertFalse(self.provider.prompts)
        self.assertFalse(event.sent)


class SettingsTest(unittest.TestCase):
    def test_default_three_seconds_and_independent_private_console_switch(self):
        schema = _load_schema_subset()
        self.assertEqual(schema["fragment_wait_seconds"]["default"], 3)
        self.assertIs(schema["fragment_wait_private"]["default"], False)
        self.assertIn("fragment_wait_private", EDITABLE)
        self.assertIs(_coerce("fragment_wait_private", "true"), True)
        self.assertIs(_coerce("fragment_wait_private", "false"), False)
        self.assertEqual(_coerce("fragment_wait_seconds", 3), 3)
        for value in (6, -1, float("nan")):
            with self.assertRaises(ValueError):
                _coerce("fragment_wait_seconds", value)
