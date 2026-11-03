"""主回复实际发送边界与撤回保护，不接触真实平台或模型。"""
import asyncio
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.core.platform.message_type import MessageType
from guard_reply_delivery import OWNER_KEY, ReplyDelivery
from guard_native_reply import REMOVED_FIELDS
from guard_webui import EDITABLE, _coerce, _load_schema_subset
from test_gate import GateEvent, gate_config, plugin

TEXT = "第一句话完整解释这个问题。第二句话说明具体操作步骤。第三句话提供另外一个建议。第四句话保留最后的重要提醒。"


class Chain:
    def __init__(self, chain=None, llm=True):
        self.chain, self.llm = list(chain or []), llm
        self.use_markdown_, self.use_t2i_, self.type = True, False, "test"
    def is_llm_result(self):
        return self.llm
    def derive(self, chain):
        result = Chain(chain, self.llm)
        result.use_markdown_, result.use_t2i_, result.type = self.use_markdown_, self.use_t2i_, self.type
        return result


class Event(GateEvent):
    def __init__(self, group="1001", mid="m1", notice=None):
        super().__init__("现在回我", group=group)
        self.message_obj = SimpleNamespace(message_id=mid, message=[],
            raw_message=notice or {"post_type": "message"})
        self.result, self.sent, self.after_send = Chain([Plain(TEXT)]), [], None
        self.ready, self.stream_called, self.is_at_or_wake_command = asyncio.Event(), False, True
    def get_message_type(self):
        return MessageType.GROUP_MESSAGE if self._group else MessageType.FRIEND_MESSAGE
    def get_result(self):
        return self.result
    def get_sender_id(self):
        return "123"
    def set_extra(self, key, value):
        super().set_extra(key, value)
        if key == "_at_guard_recall_record":
            self.ready.set()
    async def send(self, message, *args, **kwargs):
        self.sent.append(message)
        if self.after_send:
            await self.after_send()
    async def send_streaming(self, stream, *_):
        self.stream_called = True
        async for chunk in stream:
            await self.send(chunk)


def recall(event, mid="m1", group=None):
    return Event(group=event._group if group is None else group, mid="random-notice-uuid",
                 notice={"post_type": "notice", "notice_type": "group_recall" if event._group
                         else "friend_recall", "message_id": mid})


class DeliveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guard = plugin(gate_config(gate_mode="off", reply_quote_enabled=False,
                                       reply_mention_enabled=False))
        self.delivery, self.event = self.guard._delivery(), Event()
    async def arm(self, **config):
        self.guard.config.update(expression_takeover_enabled=True, **config)
        await self.guard.prepare_reply_delivery(self.event)
        await self.guard.recheck_before_send(self.event)

    async def test_off_returns_expression_and_does_not_patch_or_wait(self):
        original = self.event.send
        await self.delivery.prepare(self.event)
        self.assertEqual(self.event.send, original)
        self.assertIsNone(self.event.get_extra(OWNER_KEY))

    async def test_default_on_all_platforms_and_private_outside_gate(self):
        self.guard.config.pop("expression_takeover_enabled")
        for scope, group in (("qq:group:1001", "1001"), ("discord:channel:88", "88"), ("telegram:private:4", "")):
            event = Event(group=group)
            event.unified_msg_origin = scope
            await self.delivery.prepare(event)
            self.assertEqual(event.get_extra(OWNER_KEY), "at_guard")

    async def test_native_packets_preserve_text_and_consume_residue_once(self):
        await self.arm()
        self.event.result.chain = [Plain(TEXT[:24]), Plain(TEXT[24:])]
        original = list(self.event.result.chain)
        for c in original:
            await self.event.send(self.event.result.derive([c]))
        await self.event.send(self.event.result)
        self.assertEqual(len(self.event.sent), 2)
        self.assertEqual("".join(c.text for r in self.event.sent for c in r.chain if isinstance(c, Plain)), TEXT)

    async def test_private_uses_native_whole_reply(self):
        self.event = Event(group="")
        await self.arm()
        await self.event.send(self.event.result)
        self.assertEqual(len(self.event.sent), 1)

    async def test_native_media_and_metadata_preserved(self):
        self.event.result.chain = [Reply(id="x"), At(qq="2"), Plain(TEXT), Image.fromURL("https://example.invalid/a")]
        await self.arm()
        await self.event.send(self.event.result)
        self.assertEqual(len(self.event.sent), 1)
        self.assertEqual(self.event.sent[0].chain, self.event.result.chain)
        self.assertEqual(self.event.sent[0].type, "test")

    async def test_semantic_quote_does_not_force_private_segmentation(self):
        self.event.result.chain.insert(0, Reply(id="source"))
        await self.arm()
        await self.event.send(self.event.result)
        self.assertEqual(len(self.event.sent), 1)
        self.assertIsInstance(self.event.sent[0].chain[0], Reply)

    async def test_unrelated_direct_send_is_not_split_or_delayed(self):
        await self.arm(reply_typing_enabled=True)
        direct = Chain([Plain("提示")], llm=False)
        with patch.object(self.delivery, "typing_pause", new_callable=AsyncMock) as wait:
            await self.event.send(direct)
        wait.assert_not_called()
        self.assertIs(self.event.sent[0], direct)

    async def test_typing_output_chars_25_gives_10_seconds(self):
        self.guard.config.update(expression_takeover_enabled=True, reply_typing_enabled=True)
        self.assertEqual(self.delivery.typing_seconds([Plain("字" * 25)]), 10)
        self.assertEqual(self.delivery.typing_seconds([Plain("字" * 100)]), 40)
        self.assertEqual(self.delivery.typing_seconds([Plain("字 " * 25)]), 10)
        self.guard.config["reply_typing_chars_per_second"] = 5
        self.assertEqual(self.delivery.typing_seconds([Plain("字" * 25)]), 5)

    async def test_typing_off_and_owner_off_disable_delay(self):
        for config in ({"reply_typing_enabled": False}, {"reply_typing_enabled": True, "expression_takeover_enabled": False}):
            self.guard.config.update(config)
            self.assertEqual(self.delivery.typing_seconds([Plain("字" * 25)]), 0)

    async def test_typing_one_delay_per_actual_bubble_without_duplicate_plan(self):
        self.event.result.chain = [Plain("字" * 25)]
        await self.arm(reply_typing_enabled=True)
        with patch.object(self.delivery, "typing_pause", new_callable=AsyncMock, return_value=True) as wait:
            await self.event.send(self.event.result)
            await self.event.send(self.event.result)
        wait.assert_awaited_once_with(self.event, 10)

    async def test_recall_scope_four_combinations(self):
        for group_on, private_on in ((False, False), (True, False), (False, True), (True, True)):
            self.guard.config.update(reply_recall_group=group_on, reply_recall_private=private_on)
            for group, expected in (("1001", group_on), ("", private_on)):
                event = Event(group=group, mid=f"{group_on}{private_on}{group}")
                await self.delivery.prepare(event)
                self.delivery.notice(recall(event, mid=event.message_obj.message_id))
                self.assertEqual(self.delivery.recalled(event), expected)

    async def test_no_initial_recall_window_even_if_old_key_exists(self):
        self.guard.config.update(reply_recall_group=True, reply_recall_window_seconds=30)
        with patch.object(self.delivery, "pause", new_callable=AsyncMock, return_value=True) as wait:
            await self.delivery.prepare(self.event)
        wait.assert_awaited_once_with(self.event, 0)

    async def test_early_recall_and_other_scope_isolation(self):
        self.guard.config["reply_recall_group"] = True
        self.delivery.notice(recall(self.event))
        await self.delivery.prepare(self.event)
        self.assertTrue(self.event.stopped)
        other = Event(group="2002")
        await self.delivery.prepare(other)
        self.assertFalse(self.delivery.recalled(other))
        self.assertFalse(self.delivery.recalled(Event(mid="random-notice-uuid")))

    async def test_recall_during_generation_clears_even_with_gate_off(self):
        self.guard.config["reply_recall_group"] = True
        await self.delivery.prepare(self.event)
        self.delivery.notice(recall(self.event))
        response = SimpleNamespace(completion_text="不要发", result_chain=Chain([Plain("不要发")]))
        await self.guard.suppress_no_reply_and_track(self.event, response)
        self.assertEqual(response.completion_text, "")
        self.assertEqual(response.result_chain.chain, [])

    async def test_recall_after_first_bubble_cancels_rest_without_fallback(self):
        await self.arm(reply_recall_group=True)
        self.event.after_send = AsyncMock(side_effect=lambda: self.delivery.notice(recall(self.event)))
        await self.event.send(self.event.result)
        await self.event.send(self.event.result)
        self.assertEqual(len(self.event.sent), 1)

    async def test_recall_during_typing_ends_wait_immediately(self):
        self.event.result.chain = [Plain("字" * 25)]
        await self.arm(reply_recall_group=True, reply_typing_enabled=True)
        task = asyncio.create_task(self.event.send(self.event.result))
        await asyncio.sleep(0)
        self.delivery.notice(recall(self.event))
        await asyncio.wait_for(task, .5)
        self.assertFalse(self.event.sent)

    async def test_earlier_fragment_recall_wakes_current_typing_wait(self):
        self.event.result.chain = [Plain("字" * 25)]
        await self.arm(reply_recall_group=True, reply_typing_enabled=True)
        earlier = Event(mid="old")
        await self.delivery.prepare(earlier)
        self.event.set_extra("_at_guard_fragment_sources", (earlier,))
        task = asyncio.create_task(self.event.send(self.event.result))
        await asyncio.sleep(0)
        self.delivery.notice(recall(earlier, mid="old"))
        await asyncio.wait_for(task, .5)
        self.assertFalse(self.event.sent)

    async def test_failed_partial_send_cannot_retry_full_message(self):
        await self.arm()
        self.event.after_send = AsyncMock(side_effect=OSError("platform"))
        with self.assertRaises(OSError):
            await self.event.send(self.event.result)
        await self.event.send(self.event.result)
        self.assertEqual(len(self.event.sent), 1)

    async def test_unload_cancels_typing_without_sending(self):
        await self.arm(reply_typing_enabled=True)
        task = asyncio.create_task(self.event.send(self.event.result))
        await asyncio.sleep(0)
        await self.delivery.close()
        self.assertTrue(task.cancelled())
        self.assertFalse(self.event.sent)

    async def test_context_change_still_cancels_between_bubbles(self):
        await self.arm()
        self.event.set_extra("_at_guard_send_check", lambda: "context_changed" if self.event.sent else "")
        await self.event.send(self.event.result)
        self.assertEqual(len(self.event.sent), 1)

    async def test_typing_excluded_from_ttl_but_context_revision_still_matters(self):
        self.event.set_extra("_at_guard_recorded_at", time.monotonic() - 80)
        self.event.set_extra("_at_guard_typing_seconds", 65)
        self.guard.config["gate_reply_max_age_seconds"] = 30
        self.assertEqual(self.guard._stale_reason(self.event, "1001"), "")
        self.event.set_extra("_at_guard_judged_revision", -1)
        self.assertEqual(self.guard._stale_reason(self.event, "1001"), "context_changed")

    async def test_recall_only_protects_direct_send_without_owner(self):
        self.guard.config["reply_recall_group"] = True
        await self.delivery.prepare(self.event)
        self.assertIsNone(self.event.get_extra(OWNER_KEY))
        self.delivery.notice(recall(self.event))
        await self.event.send(Chain([Plain("提示")], llm=False))
        self.assertFalse(self.event.sent)

    async def test_stream_buffered_then_expressed_once(self):
        await self.arm()
        self.event.set_extra("_at_guard_expression_done", False)
        async def stream():
            yield Chain([Plain(TEXT[:24])])
            self.assertFalse(self.event.sent)
            yield Chain([Plain(TEXT[24:])])
        await self.event.send_streaming(stream(), True)
        self.assertEqual(len(self.event.sent), 1)
        self.assertEqual("".join(c.text for r in self.event.sent for c in r.chain if isinstance(c, Plain)), TEXT)

    async def test_recall_in_stream_discards_all_chunks(self):
        await self.arm(reply_recall_group=True)
        async def stream():
            yield Chain([Plain("前半句")])
            self.delivery.notice(recall(self.event))
            yield Chain([Plain("后半句")])
        await self.event.send_streaming(stream(), True)
        self.assertFalse(self.event.sent)

    async def test_capacity_retains_active_recall_reference(self):
        self.guard.config["reply_recall_group"] = True
        self.delivery.CAPACITY = 2
        await self.delivery.prepare(self.event)
        for mid in ("2", "3"):
            self.delivery._record(self.delivery.key(Event(mid=mid)))
        self.delivery.notice(recall(self.event))
        self.assertTrue(self.delivery.recalled(self.event))

    async def test_canceled_unsent_reply_does_not_create_phantom_cooldown(self):
        await self.arm(reply_recall_group=True)
        self.guard.config["gate_mode"] = "enforce"
        self.event.set_extra("_at_guard_proactive", True)
        self.delivery.notice(recall(self.event))
        await self.event.send(self.event.result)
        await self.guard.track_sent_message(self.event)
        self.assertFalse(self.guard.last_bot_line)


class SettingsTest(unittest.TestCase):
    def test_old_features_removed_new_default_and_limits(self):
        schema = _load_schema_subset()
        self.assertTrue(schema["expression_takeover_enabled"]["default"])
        self.assertFalse(schema["reply_typing_enabled"]["default"])
        self.assertEqual(schema["reply_typing_chars_per_second"]["default"], 2.5)
        for key in ("reply_delivery_enabled", "reply_hesitation_enabled", "reply_recall_window_seconds"):
            self.assertNotIn(key, EDITABLE)
            self.assertNotIn(key, schema)
        for key, value in (("reply_typing_chars_per_second", 0), ("understand_forward_max_depth", 9)):
            with self.assertRaises(ValueError):
                _coerce(key, value)
        for key in REMOVED_FIELDS:
            self.assertNotIn(key, EDITABLE)
            self.assertNotIn(key, schema)


if __name__ == "__main__":
    unittest.main()
