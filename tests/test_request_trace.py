"""请求隔离、真实宿主监听派发、存储降级和自身发送取消回归。"""
import ast
import asyncio
import enum
import inspect
import json
import logging
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import traceback
import typing
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from astrbot.api.message_components import Plain, Image, Reply
from guard_request_trace import ID_KEY, TRACE_KEY, RequestTrace, registry_coverage, emit
from guard_reply_delivery import ReplyDelivery
from guard_image_settings import ImageSettings
import guard_webui
from test_gate import plugin, gate_config, FakeProvider
from test_reply_delivery import Event, Chain
import test_image_generation as image_fixture

SOURCE = Path(os.environ.get("ASTRBOT_NATIVE_SOURCE", "/nonexistent"))


class TraceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = TemporaryDirectory()
        self.guard = plugin(gate_config(expression_takeover_enabled=True))
        self.guard.data_dir = Path(self.folder.name)
        self.service = self.guard._trace()

    async def asyncTearDown(self):
        await self.guard.terminate()
        self.folder.cleanup()

    async def test_ids_stable_separate_concurrent_group_and_private_requests(self):
        events = [Event(group=group, mid="same") for group in ("1", "1", "2", "")]
        ids = [self.service.begin(event) for event in events]
        self.assertEqual(len(set(ids)), 4)
        self.assertTrue(all(len(key) == 32 for key in ids))
        await asyncio.gather(*[
            self.guard.trace_tool_start(event, SimpleNamespace(name=f"tool-{i}"), {})
            for i, event in enumerate(events)])
        for index, event in enumerate(events):
            self.assertEqual(self.service.begin(event), ids[index])
            row = await self.service.get(ids[index])
            self.assertEqual(row["session_id"], event.unified_msg_origin)
            self.assertEqual(row["summary"]["tool_calls"], 1)
            self.assertEqual(row["features"]["tools"]["tool"], f"tool-{index}")

    async def test_fragment_aliases_survive_reload_and_children_use_canonical(self):
        first, owner = Event(mid="first"), Event(mid="last")
        old, rid = self.service.begin(first), self.service.begin(owner)
        emit(first, "input_parse", "success")
        self.service.merge(owner, (first, owner))
        self.assertEqual(first.get_extra(ID_KEY), rid)
        self.assertEqual((await self.service.get(old))["merged_into"], rid)
        emit(first, "speech_to_text", "success", calls=1)
        self.service.finish(owner, "completed")
        await self.service.close()
        other = RequestTrace(self.guard, self.guard.data_dir)
        row = await other.get(rid)
        self.assertEqual(row["source_ids"], [old])
        self.assertEqual(row["features"]["speech_to_text"]["status"], "success")
        self.assertEqual((await other.get(old))["state"], "merged")
        await other.close()

    async def test_snapshot_is_bounded_retains_start_and_last_outcome(self):
        event = Event()
        rid = self.service.begin(event)
        for n in range(250):
            emit(event, "tools", "running", tool="x" * 5000, span_id=n)
        self.service.finish(event, "cancelled", "context_changed")
        row = await self.service.get(rid)
        self.assertEqual(len(row["events"]), self.service.MAX_EVENTS)
        self.assertEqual(row["events"][0]["feature"], "receive")
        self.assertEqual(row["events"][-1]["reason"], "context_changed")
        self.assertTrue(row["truncated"])
        self.assertLessEqual(len(json.dumps(row, ensure_ascii=False).encode()), self.service.MAX_BYTES)

    async def test_trace_does_not_save_prompts_parameters_or_responses(self):
        event = Event()
        rid = self.service.begin(event)
        emit(event, "tools", "success", prompt="secret-prompt", tool_args={"key": "secret-key"},
             result="secret-result", reasoning="secret-think", model="https://key.invalid/?token=private",
             ms=float("nan"))
        row = json.dumps(await self.service.get(rid))
        for word in ("secret-", "key.invalid", "token", "NaN"):
            self.assertNotIn(word, row)

    async def test_livinghello_quote_vision_replaces_congling_uncollected_without_url(self):
        native = SimpleNamespace(enabled_for=lambda event: True)
        provider = SimpleNamespace(
            get_model=lambda: "qwen3-vl",
            provider_config={"modalities": ["image"]},
            text_chat=AsyncMock(return_value=SimpleNamespace(
                completion_text="引用图片摘要")),
        )
        self.guard.config.update(
            understand_image_enabled=True,
            understand_image_provider_id="own-vision",
        )
        self.guard.context = SimpleNamespace(
            get_registered_star=lambda _: SimpleNamespace(
                activated=True, star_cls=native),
            get_provider_by_id=lambda _: provider,
        )
        event = Event(group="native")
        url = "https://example.invalid/never-store.png"
        event.message_obj.message = [
            Reply(id="quoted", sender_nickname="原作者",
                  chain=[Image(url=url)]),
        ]
        event.set_extra("_livinghello_image_owner", "livinghello")
        rid = self.service.begin(event)
        emit(event, "input_parse", "uncollected", reason="congling_owns_input")

        await self.guard._understanding().attach(
            event, SimpleNamespace(extra_user_content_parts=[]))

        row = await self.service.get(rid)
        self.assertEqual(row["features"]["input_parse"], {
            "status": "success",
            "source": "livinghello",
            "ms": row["features"]["input_parse"]["ms"],
        })
        self.assertEqual(
            row["features"]["input_vision"]["status"], "success")
        self.assertEqual(
            row["features"]["input_vision"]["source"], "livinghello")
        self.assertNotIn(url, json.dumps(row, ensure_ascii=False))

    async def test_disk_failure_does_not_block_message_processing(self):
        self.service.path = Path(self.folder.name)  # 目录不能作为SQLite文件。
        event = Event()
        rid = self.service.begin(event)
        await self.service.writer
        self.assertEqual(self.service.health["storage"], "unavailable")
        await self.guard._delivery().prepare(event)
        await event.send(Chain([Plain("隔离发送")]))
        self.assertEqual(len(event.sent), 1)
        self.assertEqual((await self.service.get(rid))["summary"]["sent_packets"], 1)

    async def test_pending_capacity_prioritises_finished_snapshots(self):
        self.service.MAX_PENDING = 2
        # 模拟writer正在工作，禁止该测试中自动消费队列。
        waiter = asyncio.Event()
        self.service.writer = asyncio.create_task(waiter.wait())
        a, b, c = Event(mid="a"), Event(mid="b"), Event(mid="c")
        for event in (a, b, c):
            self.service.begin(event)
        self.assertEqual(len(self.service.pending), 2)
        self.service.finish(c, "cancelled", "message_recalled")
        self.assertIn(c.get_extra(ID_KEY), self.service.pending)
        self.assertEqual(len(self.service.pending), 2)
        waiter.set()
        await self.service.writer
        self.service.writer = None
        await self.service._drain()
        self.assertGreater(self.service.health["dropped"], 0)

    async def test_active_capacity_fails_open_and_final_records_are_evicted_first(self):
        self.service.MAX_REQUESTS = 2
        a, b, c = Event(mid="a"), Event(mid="b"), Event(mid="c")
        self.service.begin(a)
        self.service.begin(b)
        rid = self.service.begin(c)
        self.assertEqual(len(self.service.rows), 2)
        self.assertTrue(c.get_extra("_livinghello_trace_limited"))
        self.assertIsNone(await self.service.get(rid))
        self.service.finish(a, "silent")
        self.service.begin(c)
        self.assertNotIn(a.get_extra(ID_KEY), self.service.rows)
        self.assertIn(rid, self.service.rows)

    async def test_sqlite_retention_and_incomplete_restart_state(self):
        self.service.MAX_REQUESTS = 3
        for n in range(8):
            event = Event(mid=str(n))
            self.service.begin(event)
            self.service.finish(event, "completed")
        await self.service.writer
        with sqlite3.connect(self.service.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 3)
        event = Event(mid="unfinished")
        rid = self.service.begin(event)
        await self.service.writer
        other = RequestTrace(self.guard, self.guard.data_dir)
        self.assertEqual((await other.get(rid))["state"], "incomplete")
        await other.close()

    async def test_bypass_and_cancel_share_id_do_not_count_as_decisions(self):
        event = Event()
        event._mentions = [SimpleNamespace(qq="bot")]
        self.guard._mention_targets = lambda e: {"bot"}
        await self.guard.guard_llm_request(event, SimpleNamespace(extra_user_content_parts=[], image_urls=[]))
        self.guard._record_delivery_block(event, "context_changed")
        rows = self.guard.engine.log.recent()
        self.assertEqual({row["src"] for row in rows}, {"bypass", "delivery"})
        self.assertEqual(len({row["request_id"] for row in rows}), 1)
        self.assertEqual(self.guard.engine.stats["judged"], 0)
        self.assertEqual(self.guard.participation_stats["total"], 0)

    async def test_model_silence_records_identity_and_actual_metrics(self):
        self.guard.context.provider = FakeProvider(
            '{"act":"silent","conf":10,"understood":true,"reason":"others_talking","ground":"none"}')
        event = Event()
        event.is_at_or_wake_command = False
        await self.guard.guard_llm_request(event, SimpleNamespace(extra_user_content_parts=[], image_urls=[]))
        log = self.guard.engine.log.recent()[0]
        self.assertEqual(log["request_id"], event.get_extra(ID_KEY))
        self.assertEqual(log["message_id"], "m1")
        row = await self.service.get(log["request_id"])
        self.assertEqual(row["features"]["text"]["calls"], 1)
        self.assertEqual(row["features"]["vision"]["calls"], 0)
        self.assertEqual(row["state"], "silent")
        self.assertEqual(row["features"]["embedding"]["status"], "uncollected")

    async def test_api_validation_missing_and_batch_search(self):
        event = Event()
        rid = self.service.begin(event)
        self.guard._record_bypass(event, "at_call")
        with patch.object(guard_webui.request, "query", {"request_id": "bad"}, create=True), \
             patch.object(guard_webui, "_failure", lambda m, s: {"status": s}):
            self.assertEqual((await guard_webui.handle_request_trace(self.guard))["status"], 400)
        with patch.object(guard_webui.request, "query", {"request_id": "0" * 32}, create=True), \
             patch.object(guard_webui, "_failure", lambda m, s: {"status": s}):
            self.assertEqual((await guard_webui.handle_request_trace(self.guard))["status"], 404)
        with patch.object(guard_webui.request, "query", {"query": rid}, create=True):
            rows = (await guard_webui.handle_decisions(self.guard))["data"]["decisions"]
        self.assertEqual(rows[0]["trace_summary"]["state"], "active")
        self.assertIsNone((await self.service.enrich([{"gid": "1001"}]))[0]["trace_summary"])

    async def test_own_preface_and_multiple_packets_do_not_cancel_image(self):
        event = Event()
        event.extra.update(_at_guard_fragment_judged=True, _at_guard_judged_revision=12,
                           _at_guard_recorded_at=time.monotonic())
        self.guard.context_revision[self.guard._group_key(event)] = 12
        self.service.begin(event)
        images = ImageSettings(self.guard)
        for text in ("行，开始画。", "继续说明。"):
            event.result = Chain([Plain(text)])
            await self.guard.track_sent_message(event)
            self.assertFalse(images.blocked(event))
        self.assertEqual(event.get_extra("_at_guard_judged_revision"), 14)
        row = await self.service.get(event.get_extra(ID_KEY))
        self.assertTrue(row["features"]["context_revision"]["synced"])
        await images.close()

    async def test_external_change_other_request_recall_and_expiry_still_cancel(self):
        event, other = Event(), Event(mid="external")
        event.extra.update(_at_guard_fragment_judged=True, _at_guard_judged_revision=12,
                           _at_guard_recorded_at=time.monotonic())
        other.extra["_at_guard_judged_revision"] = 12
        self.guard.context_revision[self.guard._group_key(event)] = 13
        await self.guard.track_sent_message(event)
        self.assertEqual(event.get_extra("_at_guard_judged_revision"), 12)
        self.assertEqual(other.get_extra("_at_guard_judged_revision"), 12)
        self.assertEqual(self.guard._stale_reason(event, self.guard._group_key(event)), "context_changed")
        event.extra["_at_guard_source_recalled"] = True
        self.assertEqual(self.guard._stale_reason(event, self.guard._group_key(event)), "message_recalled")
        event.extra["_at_guard_source_recalled"] = False
        event.extra["_at_guard_recorded_at"] = time.monotonic() - 10000
        self.assertEqual(self.guard._stale_reason(event, self.guard._group_key(event)), "message_expired")

    async def test_preface_success_agent_done_and_failed_final_send_are_distinct(self):
        event = Event()
        self.service.begin(event)
        await self.guard.prepare_reply_delivery(event)
        await event.send(Chain([Plain("前置回复")]))
        await self.guard.trace_agent_begin(event, None)
        await self.guard.trace_agent_done(event, None, SimpleNamespace(completion_text="正文"))
        row = await self.service.get(event.get_extra(ID_KEY))
        self.assertEqual(row["summary"]["sent_packets"], 1)
        self.assertEqual(row["state"], "awaiting_delivery")
        event.result = Chain([Plain("最后正文")])
        await self.guard.recheck_before_send(event)
        plan = event.get_extra("_at_guard_delivery_plan")
        # 实际send异常不会伪装为成功；已有成功包保留。
        ReplyDelivery.trace_failure(event, OSError("secret exception"))
        plan["failed"] = True
        await self.guard.track_sent_message(event)
        row = await self.service.get(event.get_extra(ID_KEY))
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["summary"]["sent_packets"], 1)
        self.assertNotIn("secret exception", json.dumps(row))

    async def test_tool_end_missing_start_is_not_reported_as_zero_calls(self):
        event = Event()
        self.guard.evidence = SimpleNamespace(remember=lambda *args: None)
        await self.guard.remember_tool_evidence(event, SimpleNamespace(name="query"), {}, None)
        row = await self.service.get(event.get_extra(ID_KEY))
        self.assertEqual(row["features"]["tools"]["status"], "uncollected")

    async def test_agent_done_marks_missing_tool_end_uncollected(self):
        event = Event()
        await self.guard.trace_tool_start(event, SimpleNamespace(name="query"), {})
        await self.guard.trace_agent_done(event, None, SimpleNamespace(completion_text="正文"))
        row = await self.service.get(event.get_extra(ID_KEY))
        self.assertEqual(row["features"]["tools"]["status"], "uncollected")
        self.assertEqual(row["features"]["tools"]["reason"], "tool_end_missing")
        self.assertEqual(row["summary"]["tool_calls"], 1)
        self.assertFalse(event.get_extra("_livinghello_tool_spans"))


def actual_host_namespace(guard):
    """执行当前宿主注册表、派发函数及MainAgentHooks方法体。"""
    stars = {type(guard).__module__: SimpleNamespace(activated=True, star_cls=guard,
                                                   name="astrbot_plugin_at_guard",
                                                   display_name="LivingHello")}
    ns = {"enum": enum, "Generic": typing.Generic, "T": typing.TypeVar("T"),
          "overload": typing.overload, "star_map": stars, "inspect": inspect, "traceback": traceback,
          "logger": logging.getLogger("actual-host-trace"), "MessageEventResult": Chain,
          "CommandResult": type("CommandResult", (), {}),
          "AstrAgentContext": object,
          "BaseAgentRunHooks": type("Base", (), {"__class_getitem__": classmethod(lambda cls, _: cls)})}
    selected = {
        "core/star/star_handler.py": {"StarHandlerRegistry", "EventType"},
        "core/pipeline/context_utils.py": {"call_event_hook"},
        "core/astr_agent_hooks.py": {"MainAgentHooks"},
    }
    for path, names in selected.items():
        tree = ast.parse((SOURCE / path).read_text())
        nodes = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.AsyncFunctionDef)) and n.name in names]
        nodes.insert(0, ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0))
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), path, "exec"), ns)
        if "StarHandlerRegistry" in names:
            ns["star_handlers_registry"] = ns["StarHandlerRegistry"]()
    return ns, stars


@unittest.skipUnless((SOURCE / "core/astr_agent_hooks.py").exists(), "需提供当前真实宿主事件源码")
class ActualHostListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_dispatch_tool_and_agent_callbacks_and_reload_identity(self):
        guard = plugin(gate_config(expression_takeover_enabled=True))
        guard.evidence = SimpleNamespace(remember=lambda *args: None)
        ns, stars = actual_host_namespace(guard)
        registry, kinds = ns["star_handlers_registry"], ns["EventType"]
        from guard_request_trace import LISTENERS
        for key, (kind, method) in LISTENERS.items():
            registry.append(SimpleNamespace(event_type=getattr(kinds, kind), handler_name=method,
                handler_full_name=method, handler_module_path=type(guard).__module__,
                handler=getattr(guard, method), enabled=True, extras_configs={}))
        coverage = registry_coverage(guard, registry, kinds, stars)
        self.assertTrue(all(row["status"] == "registered" for row in coverage.values()))
        event = Event()
        event.plugins_name = ["*"]
        event.is_stopped = lambda: event.stopped
        event.clear_result = lambda: setattr(event, "result", None)
        event.trace = SimpleNamespace(record=lambda *_args, **_kwargs: None)
        context = SimpleNamespace(context=SimpleNamespace(event=event))
        tool, args = SimpleNamespace(name="search_memory"), {"secret": "never-log"}
        hooks = ns["MainAgentHooks"]()
        await hooks.on_agent_begin(context)
        await hooks.on_tool_start(context, tool, args)
        await hooks.on_tool_end(context, tool, args, SimpleNamespace(isError=False, content="secret-result"))
        response = SimpleNamespace(completion_text="完成", reasoning_content=None)
        await hooks.on_agent_done(context, response)
        service = guard._trace()
        row = await service.get(event.get_extra(ID_KEY))
        self.assertEqual(row["summary"]["tool_calls"], 1)
        self.assertEqual(row["features"]["tools"]["status"], "success")
        self.assertEqual(row["features"]["main_model"]["status"], "success")
        self.assertEqual(row["state"], "awaiting_delivery")
        self.assertTrue({"main_model", "tool_start", "tool_end", "agent_done"} <= set(service.last_seen))
        replacement = plugin(gate_config())
        self.assertTrue(all(r["status"] == "unverified" for r in
                            registry_coverage(replacement, registry, kinds, stars).values()))
        first = next(iter(registry))
        registry.append(first)
        self.assertEqual(registry_coverage(guard, registry, kinds, stars)["receive"]["status"], "unregistered")
        await guard.terminate()


class ImageTraceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = image_fixture.ImageGenerationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.folder.cleanup)
        self.service = self.fixture.service
        self.trace = RequestTrace(self.fixture.plugin, self.fixture.plugin.data_dir)

    async def asyncTearDown(self):
        await self.service.close()
        await self.trace.close()

    async def test_image_context_preflight_cancellation_calls_tool_but_not_api(self):
        from test_image_generation import Event as ImageEvent
        event = ImageEvent()
        self.trace.begin(event)
        event.extra["_at_guard_delivery_blocked"] = "context_changed"
        result = await self.service.generate(event, "画猫")
        self.assertIn("已取消", result)
        row = await self.trace.get(event.get_extra(ID_KEY))
        self.assertEqual(row["summary"]["tool_calls"], 1)
        self.assertEqual(row["summary"]["image_api_calls"], 0)
        self.assertEqual(row["features"]["image_tool"]["reason"], "context_changed")
        self.fixture.service._request.assert_not_awaited()
        self.assertEqual(self.service.used(), 0)

    async def test_image_quota_exhausted_preserves_zero_api_calls(self):
        from test_image_generation import Event as ImageEvent
        self.service.values["daily_limit"] = 0
        event = ImageEvent()
        self.trace.begin(event)
        result = await self.service.generate(event, "画猫")
        self.assertIn("次数已用完", result)
        row = await self.trace.get(event.get_extra(ID_KEY))
        self.assertEqual(row["features"]["image_quota"]["reason"], "quota_exhausted")
        self.assertEqual(row["summary"]["image_api_calls"], 0)
        self.assertEqual(row["summary"]["tool_calls"], 1)
        self.service._request.assert_not_awaited()

    async def test_host_and_internal_image_tool_counts_are_not_duplicated(self):
        from test_image_generation import Event as ImageEvent
        event = ImageEvent()
        self.trace.begin(event)
        self.trace.increment(event, "tool_calls")
        event.extra["_livinghello_tool_spans"] = {(1, 2): 1}
        await self.service.generate(event, "画猫")
        row = await self.trace.get(event.get_extra(ID_KEY))
        self.assertEqual(row["summary"]["tool_calls"], 1)
        self.assertEqual(row["features"]["image_delivery"]["status"], "success")
        self.assertEqual(self.service.used(), 1)
