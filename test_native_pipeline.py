"""执行实际宿主ResultDecorateStage+RespondStage，仅替换模型、发送和时钟。"""
import ast
import asyncio
import copy
import logging
import math
import os
from pathlib import Path
import random
import re
import time
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from astrbot.api.message_components import At, Image, Plain, Record, Reply
from astrbot.core.platform.message_type import MessageType
from test_gate import gate_config, plugin
from test_native_reply import NATIVE
from guard_native_reply import second_period_regex, managed_regex
from test_reply_delivery import Chain, Event, recall

SOURCE = Path(os.environ.get("ASTRBOT_NATIVE_SOURCE", "/nonexistent-astrbot-source"))


def stages(guard):
    classes = {c.__name__: c for c in (Plain, At, Reply, Image, Record)}
    names = ("Plain At Reply Image Record Face Video Poke Node Nodes File Json Share Music "
             "Forward Location Contact Shake Dice RPS Unknown").split()
    for name in names:
        if name not in classes:
            classes[name] = type(name, (), {"__init__": lambda self, **kw: self.__dict__.update(kw)})
        if not hasattr(classes[name], "type"):
            classes[name].type = name
    comp = SimpleNamespace(**classes)
    types = SimpleNamespace(**{name: classes[name].type for name in names})
    types.GeneralResult = "GeneralResult"
    content_types = SimpleNamespace(STREAMING_RESULT="stream", STREAMING_FINISH="finish")
    namespace = dict(classes, Stage=object, ComponentType=types, Comp=comp,
        logger=logging.getLogger("native-test"), MessageChain=Chain,
        ResultContentType=content_types, random=random, re=re, time=time,
        math=math, asyncio=asyncio, traceback=traceback, MessageType=MessageType,
        registered_stages=[], ContentSafetyCheckStage=object,
        star_map={"at_guard": SimpleNamespace(name="at_guard")},
        EventType=SimpleNamespace(OnDecoratingResultEvent="decorate", OnAfterMessageSentEvent="after"),
        star_handlers_registry=SimpleNamespace(get_handlers_by_event_type=lambda *a, **kw: [
            SimpleNamespace(handler_module_path="at_guard", handler_name="reply_modes",
                            handler=guard.recheck_before_send)]),
        SessionServiceManager=SimpleNamespace(should_process_tts_request=AsyncMock(return_value=True)),
        html_renderer=SimpleNamespace(render_t2i=AsyncMock(return_value="https://example.invalid/render")),
        path_Mapping=lambda value, _: value)
    async def hook(*_):
        return False
    namespace["call_event_hook"] = hook
    for name, relative in (("ResultDecorateStage", "core/pipeline/result_decorate/stage.py"),
                           ("RespondStage", "core/pipeline/respond/stage.py")):
        tree = ast.parse((SOURCE / relative).read_text())
        nodes = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.Assign))
                 and (not isinstance(n, ast.Assign) or any(
                     isinstance(t, ast.Name) and t.id == "DEFAULT_LOG_BASE" for t in n.targets))]
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                node.decorator_list = []
        nodes.insert(0, ast.ImportFrom(module="__future__",
                                      names=[ast.alias(name="annotations")], level=0))
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                     str(SOURCE / relative), "exec"), namespace)
    return namespace


@unittest.skipUnless((SOURCE / "core/pipeline/respond/stage.py").is_file(), "需提供实际宿主源码路径")
class NativePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_managed_tables_and_fenced_code_keep_required_line_breaks(self):
        for text in ("|项目|值|\n|---|---|\n|状态|正常|", "~~~python\nprint('甲。乙。')\n~~~"):
            for enable in (False, True):
                with self.subTest(text=text, enable=enable):
                    event, _, _ = await self.run_pipeline(
                        text=text, native_quote=False, native_mention=False, quote=False, mention=False,
                        segments={"enable": enable, "only_llm_result": False,
                                  "regex": managed_regex(second=True, maximum=1), "split_mode": "regex"})
                    self.assertEqual(["".join(c.text for c in chain.chain if isinstance(c, Plain))
                                      for chain in event.sent], [text])

    async def test_second_period_runs_in_actual_host_and_resets_after_other_endings(self):
        for text, expected in (
            ("甲。乙。丙。丁。", ["甲。乙。", "丙。丁。"]),
            ("甲。乙！丙。丁。戊？己。庚。", ["甲。乙！", "丙。丁。", "戊？", "己。庚。"]),
            ("甲。\n乙。\n尾", ["甲。\n乙。", "尾"]),
        ):
            with self.subTest(text=text):
                event, guard, sleep = await self.run_pipeline(
                    text=text, native_quote=False, native_mention=False, quote=False, mention=False, segments={
                    "enable": True, "split_mode": "regex",
                    "regex": second_period_regex(NATIVE["split_words"]), "interval": "0,0",
                })
                texts = ["".join(c.text for c in chain.chain if isinstance(c, Plain))
                         for chain in event.sent]
                self.assertEqual(texts, expected)
                self.assertTrue(all(call.args == (0.0,) for call in sleep.await_args_list))

    async def run_pipeline(self, text="第一句。第二句！", segments=None, platform="test",
                           native_quote=True, native_mention=True, quote=True, mention=True,
                           media=False, tts=False, t2i=False, cancel="", streaming=False):
        guard = plugin(gate_config(expression_takeover_enabled=True, reply_quote_enabled=quote,
                                   reply_mention_enabled=mention, reply_typing_enabled=True))
        event = Event()
        event.get_platform_name = lambda: platform
        event.plugins_name = []
        event.is_stopped = lambda: event.stopped
        event.clear_result = lambda: None
        event._outline_chain = lambda _: "offline fixture"
        event.get_platform_id = lambda: "test"
        event.result.result_content_type = "llm"
        event.result.is_model_result = lambda: True
        event.result.get_plain_text = lambda: "".join(c.text for c in event.result.chain if isinstance(c, Plain))
        event.result.chain = [Plain(text)] + ([Image.fromURL("https://example.invalid/media")] if media else [])
        event.result.use_t2i_ = t2i
        await guard._delivery().prepare(event)
        guard._delivery().typing_pause = AsyncMock(return_value=True)
        if cancel == "recall":
            guard.config["reply_recall_group"] = True
            # 提前登记原消息；发送首段后模拟平台撤回通知。
            event.set_extra("_at_guard_recall_record", guard._delivery()._record(
                guard._delivery().key(event)))
            event.after_send = AsyncMock(side_effect=lambda: guard._delivery().notice(recall(event)))
        elif cancel:
            event.set_extra("_at_guard_send_check", lambda: cancel if event.sent else "")
        if streaming:
            event.result.result_content_type = "stream"
            async def chunks():
                yield Chain([Plain(text[:len(text)//2])])
                yield Chain([Plain(text[len(text)//2:])])
            event.result.async_stream = chunks()
        namespace = stages(guard)
        native = copy.deepcopy(NATIVE)
        native.update(enable=True, interval="0,0")
        native.update(segments or {})
        guard.context.get_config = lambda *_: {"platform_settings": {"segmented_reply": native}}
        tts_provider = SimpleNamespace(get_audio=AsyncMock(return_value="/offline/audio.wav")) if tts else None
        ctx = SimpleNamespace(astrbot_config=dict(
            platform_settings=dict(reply_prefix="", reply_with_quote=native_quote,
                reply_with_mention=native_mention, segmented_reply=native, forward_threshold=10000),
            provider_tts_settings=dict(enable=tts, use_file_service=False, dual_output=False),
            provider_settings={}, t2i_word_threshold=50, t2i_strategy="remote",
            t2i_active_template="", t2i=t2i, content_safety={"also_use_in_response": False},
            callback_api_base="", t2i_use_file_service=False),
            plugin_manager=SimpleNamespace(context=SimpleNamespace(
                get_using_tts_provider_async=AsyncMock(return_value=tts_provider))))
        decorate, respond = namespace["ResultDecorateStage"](), namespace["RespondStage"]()
        await decorate.initialize(ctx)
        await respond.initialize(ctx)
        with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
            async for _ in decorate.process(event):
                pass
            await respond.process(event)
            if streaming:
                event.result.result_content_type = "finish"
                await respond.process(event)
                event.result.result_content_type = "llm"
                await respond.process(event)
        return event, guard, sleep

    async def test_actual_stages_split_once_with_all_head_switch_combinations(self):
        for native_quote in (False, True):
            for native_mention in (False, True):
                for quote in (False, True):
                    for mention in (False, True):
                        event, guard, sleep = await self.run_pipeline(
                            native_quote=native_quote, native_mention=native_mention,
                            quote=quote, mention=mention)
                        self.assertEqual(len(event.sent), 2)
                        first, second = [r.chain for r in event.sent]
                        self.assertEqual(sum(isinstance(c, Reply) for c in first), int(quote))
                        self.assertEqual(sum(isinstance(c, At) for c in first), int(mention))
                        self.assertFalse(any(isinstance(c, (At, Reply)) for c in second))
                        self.assertEqual("".join(c.text.strip() for r in event.sent for c in r.chain
                                                if isinstance(c, Plain)), "第一句。第二句！")
                        self.assertEqual(sleep.await_count, 2)
                        self.assertEqual(guard._delivery().typing_pause.await_count, 2)

    async def test_native_disable_threshold_words_and_platform_limits(self):
        for segments, platform, count, expected in (
            ({"enable": False}, "test", 1, "第一句。第二句！"),
            ({"words_count_threshold": 2}, "test", 1, "第一句。第二句！"),
            ({"split_mode": "words"}, "test", 2, "第一句第二句"),
            ({}, "qq_official_webhook", 1, "第一句。第二句！"),
            ({}, "dingtalk", 1, "第一句。第二句！")):
            event, _, _ = await self.run_pipeline(segments=segments, platform=platform)
            self.assertEqual(len(event.sent), count)
            self.assertEqual("".join(c.text.strip() for r in event.sent for c in r.chain
                                    if isinstance(c, Plain)), expected)

    async def test_media_tts_and_t2i_preserve_native_transform_without_old_text_replay(self):
        event, _, _ = await self.run_pipeline(media=True)
        self.assertEqual(len(event.sent), 3)
        self.assertIsInstance(event.sent[-1].chain[-1], Image)
        event, _, _ = await self.run_pipeline(tts=True)
        self.assertTrue(all(isinstance(c, Record) for r in event.sent for c in r.chain))
        event, _, _ = await self.run_pipeline(text="字" * 80, t2i=True)
        self.assertEqual(len(event.sent), 1)
        self.assertIsInstance(event.sent[0].chain[-1], Image)
        self.assertFalse(any(isinstance(c, Plain) for r in event.sent for c in r.chain))

    async def test_actual_native_loop_ignores_late_recall_but_stops_on_context_change(self):
        for reason in ("recall", "context_changed"):
            event, _, _ = await self.run_pipeline(cancel=reason)
            self.assertEqual(len(event.sent), 2 if reason == "recall" else 1)
            self.assertEqual(event.get_extra("_at_guard_delivery_blocked"),
                             None if reason == "recall" else reason)

    async def test_actual_native_stream_finish_does_not_replay_text(self):
        event, guard, _ = await self.run_pipeline(streaming=True)
        self.assertEqual(len(event.sent), 1)
        self.assertEqual("".join(c.text for c in event.sent[0].chain if isinstance(c, Plain)),
                         "第一句。第二句！")
        self.assertEqual(guard._delivery().typing_pause.await_count, 1)

    async def test_managed_model_paragraphs_split_with_stream_and_normal_results(self):
        for streaming in (False, True):
            event, _, _ = await self.run_pipeline(text="甲。乙。\n\n丙。丁。",
                streaming=streaming, native_quote=False, native_mention=False, quote=False,
                mention=False, segments={"regex": managed_regex(), "only_llm_result": True})
            self.assertEqual(["".join(c.text for c in r.chain if isinstance(c, Plain))
                              for r in event.sent], ["甲。乙。", "丙。丁。"])

    async def test_managed_refinements_are_independent_and_maximum_keeps_all_text(self):
        for words, second, maximum, expected in (
            (False, False, 0, ["甲。乙！丙。丁。戊！"]),
            (False, True, 0, ["甲。乙！丙。", "丁。戊！"]),
            (True, False, 0, ["甲。", "乙！", "丙。", "丁。", "戊！"]),
            (True, True, 0, ["甲。乙！", "丙。丁。", "戊！"]),
            (True, False, 2, ["甲。 乙！ 丙。", "丁。 戊！"]),
        ):
            event, _, _ = await self.run_pipeline(text="甲。乙！丙。丁。戊！",
                native_quote=False, native_mention=False, quote=False, mention=False,
                segments={"regex": managed_regex(words, second, maximum)})
            self.assertEqual(["".join(c.text for c in r.chain if isinstance(c, Plain))
                              for r in event.sent], expected)

    async def test_managed_disable_preserves_model_paragraph_switch(self):
        for only, expected in ((False, "甲。 乙。"), (True, "甲。\n\n乙。")):
            for streaming in (False, True):
                event, _, _ = await self.run_pipeline(text="甲。\n\n乙。", streaming=streaming,
                    native_quote=False, native_mention=False, quote=False, mention=False,
                    segments={"enable": False, "regex": managed_regex(), "only_llm_result": only})
                self.assertEqual(len(event.sent), 1)
                self.assertEqual("".join(c.text for c in event.sent[0].chain if isinstance(c, Plain)), expected)
