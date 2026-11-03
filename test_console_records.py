"""运行记录、保存失败、身份搜索与能力耗时回归；无线上写入。"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from guard_engine import Decision, DecisionLog, GateEngine, decision_identity
import guard_webui
from test_gate import gate_config, plugin
import test_multimodal
from test_multimodal import Provider
from test_context_policy import output


class RecordsTest(unittest.TestCase):
    def test_rotation_and_reload_preserve_all_archives(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decisions.jsonl"
            log = DecisionLog(path, max_bytes=70)
            for index in range(12):
                log.add({"ts": index, "gid": "1001", "raw": "中文" * 10})
            self.assertEqual([r["ts"] for r in DecisionLog(path).recent(100)], list(reversed(range(12))))
            self.assertGreater(len(list(path.parent.glob("*.archive-*"))), 1)

    def test_legacy_rotation_and_corrupt_lines_are_recovered(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decisions.jsonl"
            path.with_suffix(".jsonl.1").write_text('{"ts":1}\n坏行\n{"ts":2}\n')
            path.write_text('{"ts":3}\n')
            self.assertEqual([r["ts"] for r in DecisionLog(path).recent()], [3, 2, 1])

    def test_large_file_reads_tail_and_limits_memory_ring(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decisions.jsonl"
            with path.open("w") as file:
                for index in range(6000):
                    file.write(json.dumps({"ts": index, "raw": "x" * 1000}) + "\n")
            rows = DecisionLog(path, ring_size=5).recent(100)
            self.assertEqual([r["ts"] for r in rows], [5999, 5998, 5997, 5996, 5995])

    def test_search_reaches_older_than_default_page_and_private_umo(self):
        log = DecisionLog(None)
        log.add({"gid": "aiocqhttp:FriendMessage:998877", "raw": "旧私聊"})
        for index in range(150):
            log.add({"gid": "1001", "ts": index})
        self.assertEqual(len(log.recent(100)), 100)
        self.assertEqual(len(log.recent(100, "998877")), 1)
        self.assertEqual(len(log.recent(100, "1001")), 100)

    def test_group_sender_never_becomes_private_identity(self):
        group = decision_identity({"gid": "1001", "chat_data": {"current_message": {"sender_id": "998877"}}})
        self.assertEqual(group["group_id"], "1001")
        self.assertEqual(group["private_qq"], "")

    def test_new_private_identity_is_written(self):
        engine = GateEngine(gate_config(), None, SimpleNamespace(debug=lambda *_: None))
        d = Decision(chat_data={"current_message": {"session_type": "private",
                     "private_qq": "998877", "session_id": "qq:FriendMessage:998877"}})
        engine.write_log(d, group_key="qq:FriendMessage:998877", mode="enforce", generation_skipped=False)
        self.assertEqual(engine.log.recent()[0]["private_qq"], "998877")
        self.assertEqual(engine.log.recent()[0]["timings"], {"version": 1})

    def test_shutdown_flushes_statistics(self):
        with TemporaryDirectory() as directory:
            guard = plugin(gate_config())
            guard.engine = GateEngine(guard.config, Path(directory), guard.logger)
            guard.engine.stats["judged"] = 7
            asyncio.run(guard.terminate())
            reloaded = GateEngine(guard.config, Path(directory), guard.logger)
            self.assertEqual(reloaded.stats["judged"], 7)


class SaveTest(unittest.TestCase):
    def save(self, updates, fail=False):
        class Config(dict):
            def save_config(self):
                if fail:
                    raise OSError("test disk error")
        guard = plugin(gate_config())
        guard.config = Config(guard.config)
        guard.engine.config = guard.config
        guard.engine.log.add({"gid": "1001", "ts": 1})
        guard.engine.stats["judged"] = 17
        guard.participation_stats = {"total": 30, "allowed": 10, "blocked": 20}
        before = dict(guard.config)
        async def body(**_):
            return updates
        async def providers(*_):
            return []
        with patch.object(guard_webui.request, "json", body, create=True), \
             patch.object(guard_webui, "_providers", providers):
            result = asyncio.run(guard_webui.handle_config(guard))
        return guard, result, before

    def test_rhythm_save_preserves_logs_and_both_statistics(self):
        guard, result, _ = self.save({"fragment_wait_seconds": 4})
        self.assertEqual(result["data"]["today"]["judged"], 17)
        self.assertEqual(result["data"]["lifetime"]["total"], 30)
        self.assertEqual(guard.engine.log.recent()[0]["ts"], 1)
        self.assertNotIn("口径", result["data"]["save_notice"])

    def test_policy_change_warns_and_preserves_records(self):
        guard, result, _ = self.save({"participation_confidence_threshold": 72})
        self.assertIn("口径", result["data"]["save_notice"])
        self.assertEqual(guard.engine.stats["judged"], 17)
        self.assertEqual(len(guard.engine.log.ring), 1)

    def test_disk_failure_restores_config_and_records(self):
        guard, result, before = self.save({"fragment_wait_private": True, "gate_min_length": 5}, fail=True)
        self.assertEqual(dict(guard.config), before)
        self.assertIn("配置未落盘", result)
        self.assertEqual(guard.engine.stats["judged"], 17)

    def test_same_value_does_not_report_policy_change(self):
        _, result, _ = self.save({"participation_confidence_threshold": 65})
        self.assertNotIn("口径", result["data"]["save_notice"])

    def test_unknown_field_cannot_clear_statistics(self):
        guard, result, before = self.save({"reset_stats": True})
        self.assertIn("不允许", result)
        self.assertEqual(dict(guard.config), before)

    def test_search_endpoint(self):
        guard = plugin(gate_config())
        guard.engine.log.add({"gid": "qq:FriendMessage:998877"})
        with patch.object(guard_webui.request, "query", {"query": "998877"}, create=True):
            result = asyncio.run(guard_webui.handle_decisions(guard))
        self.assertEqual(len(result["data"]["decisions"]), 1)

    def test_search_endpoint_legacy_args(self):
        guard = plugin(gate_config())
        guard.engine.log.add({"gid": "1001"})
        with patch.object(guard_webui.request, "query", None, create=True), \
             patch.object(guard_webui.request, "args", {"query": "1001"}, create=True):
            result = asyncio.run(guard_webui.handle_decisions(guard))
        self.assertEqual(len(result["data"]["decisions"]), 1)

    def test_search_endpoint_empty_query_returns_recent(self):
        guard = plugin(gate_config())
        guard.engine.log.add({"gid": "1001"})
        with patch.object(guard_webui.request, "query", {}, create=True):
            result = asyncio.run(guard_webui.handle_decisions(guard))
        self.assertEqual(result["data"]["query"], "")
        self.assertEqual(len(result["data"]["decisions"]), 1)


class TimingTest(unittest.TestCase):
    def test_model_stages_measure_actual_calls(self):
        _, d, _, _, _ = test_multimodal.MultimodalTest().run_case(
            vision=Provider(RuntimeError("failed")), gate_main_fallback_enabled=True)
        self.assertEqual(d.timings["text"]["calls"], 1)
        self.assertEqual(d.timings["vision"]["status"], "error")
        self.assertEqual(d.timings["main_fallback"]["calls"], 1)
        self.assertNotIn("memory", d.timings)
        self.assertTrue(all(a["called"] for a in d.attempts))

    def test_memory_and_text_recheck_have_separate_totals(self):
        async def memory():
            await asyncio.sleep(0.005)
            return ["历史背景"]
        text = Provider(output("silent", 30, False, "context_missing", "memory"), output())
        _, d, _, _, _ = test_multimodal.MultimodalTest().run_case(text=text, memory=memory)
        self.assertEqual(d.timings["text"]["calls"], 2)
        self.assertGreater(d.timings["memory"]["ms"], 0)
        self.assertEqual(d.timings["memory"]["status"], "ok")

    def test_memory_failure_is_timed_and_stays_closed(self):
        async def memory():
            raise RuntimeError("failed")
        text = Provider(output("silent", 30, False, "context_missing", "memory"))
        _, d, _, _, _ = test_multimodal.MultimodalTest().run_case(text=text, memory=memory)
        self.assertEqual(d.action, "silence")
        self.assertEqual(d.timings["memory"]["status"], "error")
        self.assertEqual(d.timings["memory"]["calls"], 1)

    def test_no_vision_provider_is_not_a_zero_ms_model_call(self):
        async def provider():
            return Provider(output("silent", 30, False, "media_unresolved"))
        engine = GateEngine(gate_config(gate_main_fallback_enabled=False), None, SimpleNamespace(debug=lambda *_: None))
        async def images():
            return [{"url": "unused"}]
        d = asyncio.run(engine.decide(group_key="test", current_text="看图", context_lines=[],
            bot_line="", is_private=False, resolve_provider=provider, resolve_images=images))
        self.assertNotIn("vision", d.timings)


_timing_path = Path(os.environ.get(
    "LIVINGMEMORY_TIMING_SOURCE",
    str(Path(__file__).resolve().parents[1] / "LivingMemory/core/retrieval/request_timing.py")))
timing = None
# LivingMemory是可选配套插件；独立克隆AtGuard时不能让其缺席阻断全部测试导入。
if _timing_path.is_file():
    _spec = importlib.util.spec_from_file_location("lm_request_timing_test", _timing_path)
    timing = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(timing)


@unittest.skipUnless(timing is not None, "需提供LivingMemory请求计时源码")
class RequestTimingTest(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_requests_are_isolated_and_children_share_request(self):
        async def request(name):
            with timing.capture() as metrics:
                async def child():
                    with timing.measure(name):
                        await asyncio.sleep(0.003)
                await asyncio.gather(child(), child())
            return metrics
        first, second = await asyncio.gather(request("vector"), request("memory"))
        self.assertEqual(set(first), {"vector"})
        self.assertEqual(set(second), {"memory"})
        self.assertEqual(first["vector"]["calls"], 2)

    async def test_cancellation_and_failure_are_recorded_without_swallowing(self):
        with timing.capture() as metrics:
            with self.assertRaises(asyncio.CancelledError):
                with timing.measure("vector"):
                    raise asyncio.CancelledError()
            with self.assertRaises(ValueError):
                with timing.measure("memory"):
                    raise ValueError("test")
        self.assertEqual(metrics["vector"]["status"], "cancelled")
        self.assertEqual(metrics["memory"]["status"], "error")

    async def test_nested_capture_restores_previous_request(self):
        with timing.capture() as outer:
            with timing.capture() as inner:
                with timing.measure("inner"):
                    pass
            with timing.measure("outer"):
                pass
        self.assertEqual(set(outer), {"outer"})
        self.assertEqual(set(inner), {"inner"})
        with timing.measure("uncollected"):
            pass
        self.assertEqual(set(outer), {"outer"})
