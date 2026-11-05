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


CONGLING_MEMORY = """<!--congling:memory-->
【长期记忆 · 仅供参考；与当前对话冲突时以当前对话为准】
你和 TA 现在是「陌生」：还不太熟，说话客气一点
【记忆结束】"""
CONGLING_PERCEIVED = """<!--congling:perceived:1012794694-->
【消息解析补充 · 以下内容由系统自动展开，不是用户原话】
以下是这个会话里最近的几条消息（转发已展开），只作上下文参考，不是当前用户这一句的内容：
聪明的帕鲁: 廉颇老矣，尚能饭否
闲人xh925: 抓紧打影之刃，我都快通关了。
闲人xh925: 你真的廉颇老矣了，还能打不？
聪明的帕鲁: 我连着两个晚自习，已经倦了
鎖鈊嗳╰ァㄊ: [图片：图中是Q版初音未来的表情包，她流着两条蓝色宽泪、半闭着眼，呈现无语流泪的情绪。图中无文字。]从MIKU出生单推至今~
聪明的帕鲁: 今天健身，准备明天的贡品，直接请假上坟去
【补充结束】"""


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

    def test_category_filters_full_ring_before_page_limit_and_can_combine_search(self):
        log = DecisionLog(None)
        log.add({"gid": "qq:FriendMessage:998877", "raw": "关键旧私聊"})
        log.add({"gid": "1001", "raw": "关键呼叫",
                 "chat_data": {"current_message": {"explicit_call": True}}})
        for index in range(150):
            log.add({"gid": "1001", "raw": "公开话题", "ts": index})
        self.assertEqual(len(log.recent(100, category="private")), 1)
        self.assertEqual(len(log.recent(100, "关键", category="group")), 1)
        self.assertEqual(len(log.recent(100, category="public")), 100)
        self.assertEqual(log.recent(100, "不存在", category="private"), [])

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

    def test_overview_only_returns_ten_recent_decisions(self):
        guard = plugin(gate_config())
        for index in range(12):
            guard.engine.log.add({"gid": "1001", "ts": index})
        async def providers(*_):
            return []
        with patch.object(guard_webui, "_providers", providers):
            result = asyncio.run(guard_webui._state(guard))
        self.assertEqual([row["ts"] for row in result["decisions"]], list(range(11, 1, -1)))

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


class DisplayContextTest(unittest.TestCase):
    @staticmethod
    def chat(*, recent=None, memory=None):
        return {
            "recent_messages_old_to_new": recent or [],
            "current_message": {
                "text": "然后你深海少女手办买了吗",
                "sender_name": "聪明的帕鲁",
            },
            "scoped_memory": memory if memory is not None else [
                CONGLING_MEMORY, CONGLING_PERCEIVED],
        }

    def test_user_sample_counts_six_perceived_messages_not_long_term_memory(self):
        chat = guard_webui._display_chat_data(self.chat())
        self.assertEqual(chat["reference_count"], 6)
        self.assertEqual(
            [item["sender_name"] for item in chat["reference_messages_old_to_new"]],
            ["聪明的帕鲁", "闲人xh925", "闲人xh925", "聪明的帕鲁",
             "鎖鈊嗳╰ァㄊ", "聪明的帕鲁"],
        )
        self.assertEqual(chat["background_memory"], [CONGLING_MEMORY])
        self.assertNotIn("congling:perceived", "\n".join(chat["background_memory"]))

    def test_local_messages_win_dedup_and_omission_stays_visible_without_counting(self):
        perceived = """<!--congling:perceived:1-->
以下是这个会话里最近的几条消息：
Alice: HELLO &amp;   world
…（更早的略）
Bob：第二条: 正文冒号保留
【补充结束】"""
        chat = guard_webui._display_chat_data(self.chat(
            recent=[{"sender_name": "alice", "text": "hello & world"}],
            memory=[perceived],
        ))
        self.assertEqual(chat["reference_count"], 2)
        self.assertEqual(len(chat["reference_messages_old_to_new"]), 3)
        self.assertEqual(chat["reference_messages_old_to_new"][0]["source"], "livinghello")
        self.assertTrue(chat["reference_messages_old_to_new"][1]["is_omission"])
        self.assertEqual(chat["reference_messages_old_to_new"][2]["text"], "第二条: 正文冒号保留")

    def test_broken_or_unknown_perceived_block_fails_open(self):
        broken = "<!--congling:perceived:1-->\nAlice: 没有稳定标题"
        chat = guard_webui._display_chat_data(self.chat(memory=[broken]))
        self.assertEqual(chat["reference_count"], 0)
        self.assertEqual(chat["background_memory"], [broken])

    def test_reference_display_is_bounded(self):
        recent = [
            {"sender_name": f"user-{index}", "text": "x" * 700}
            for index in range(35)
        ]
        chat = guard_webui._display_chat_data(self.chat(recent=recent, memory=[]))
        self.assertEqual(chat["reference_count"], 30)
        self.assertEqual(len(chat["reference_messages_old_to_new"]), 30)
        self.assertTrue(all(
            len(item["text"]) == 600
            for item in chat["reference_messages_old_to_new"]
        ))

    def test_state_and_search_apply_the_same_legacy_record_enhancement(self):
        guard = plugin(gate_config())
        guard.engine.log.add({"gid": "1001", "ts": 1, "chat_data": self.chat()})

        async def providers(*_):
            return []

        with patch.object(guard_webui, "_providers", providers):
            state = asyncio.run(guard_webui._state(guard))
        with patch.object(guard_webui.request, "query", {"query": "1001"}, create=True):
            search = asyncio.run(guard_webui.handle_decisions(guard))["data"]
        state_chat = state["decisions"][0]["chat_data"]
        search_chat = search["decisions"][0]["chat_data"]
        self.assertEqual(state_chat["reference_count"], 6)
        self.assertEqual(search_chat["reference_count"], 6)
        self.assertEqual(
            state_chat["reference_messages_old_to_new"],
            search_chat["reference_messages_old_to_new"],
        )


class ConsolePageContractTest(unittest.TestCase):
    def test_recent_decision_table_uses_conversation_id_and_historical_threshold(self):
        page = (Path(__file__).resolve().parents[1] / "pages/guard/index.html").read_text()
        self.assertIn(
            "<tr><th>对话ID</th><th>request_id</th><th>文字判断结果</th>"
            "<th>总耗时</th><th>参与适合度</th><th>判断结果</th><th>查看详情</th></tr>",
            page,
        )
        self.assertEqual(page.count('colspan="7"'), 2)
        self.assertIn("/:(?:GroupMessage|group):(\\d+)$/i", page)
        self.assertIn('const threshold=hasThreshold?Number(row.th):null;', page)
        self.assertIn("const passed=score>=threshold;", page)
        self.assertIn(".decision-score.pass", page)
        self.assertIn(".decision-score.fail", page)
        self.assertIn(".decision-score.missing", page)
        summary = page[
            page.index("function textDecisionSummary"):
            page.index("function decisionDuration")
        ]
        self.assertNotIn("建议参与", summary)
        self.assertNotIn("建议静默", summary)

    def test_console_exposes_only_two_modes_and_explains_observation_flow(self):
        page = (Path(__file__).resolve().parents[1] / "pages/guard/index.html").read_text()
        self.assertIn('["enforce", "shadow"].forEach((mode)', page)
        mode_block = page[
            page.index("const MODE_HINT"):
            page.index("const STATUS_LABEL")
        ]
        self.assertNotIn("legacy:", mode_block)
        self.assertNotIn("off:", mode_block)
        self.assertIn('observed:"仅记录"', page)
        self.assertIn('id="flowModeSummary"', page)
        self.assertIn("<span>线上处理 <b>", page)
        self.assertIn("继续回复（观察模式不拦截）", page)
        for removed in ("legacy", "off"):
            with self.assertRaises(ValueError):
                guard_webui._coerce("gate_mode", removed)


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
    # 测试移入 tests/ 后仍从插件同级目录寻找可选配套源码。
    str(Path(__file__).resolve().parents[2] / "LivingMemory/core/retrieval/request_timing.py")))
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
