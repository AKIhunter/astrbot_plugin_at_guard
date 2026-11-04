"""输入边界、模型真实参数和并发隔离回归；所有模型及平台使用替身。"""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from astrbot.api.message_components import Plain, Image, Reply, Record
from guard_understanding import Understanding, audio_capable
from guard_expression_help import HELP
from guard_webui import _providers, _load_schema_subset, EDITABLE
from test_gate import gate_config, plugin
from test_reply_delivery import Event, Chain


def component(name, **data):
    value = type(name, (), {})()
    for key, item in data.items():
        setattr(value, key, item)
    return value


def segment(text):
    return {"type": "text", "data": {"text": text}}


def node(text, author="原作者"):
    return {"type": "node", "data": {"name": author, "content": [segment(text)]}}


class UnderstandingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guard = plugin(gate_config(expression_takeover_enabled=True,
            understand_image_enabled=False, understand_voice_enabled=False))
        self.reader, self.event = self.guard._understanding(), Event()
    async def parse(self, chain, **config):
        self.guard.config.update(config)
        self.event.message_obj.message = chain
        return await self.reader.parse(self.event)

    async def test_inline_forward_global_count_and_authors(self):
        observation = await self.parse([component("Nodes", nodes=[node(str(i), f"user{i}") for i in range(20)])],
                                       understand_forward_max_nodes=10)
        self.assertEqual(observation.nodes, 10)
        self.assertIn("user9: 9", observation.text)
        self.assertNotIn("user10: 10", observation.text)
        self.assertIn("条数上限", observation.text)

    async def test_nested_depth_includes_outer(self):
        nested = component("Nodes", nodes=[{"name": "outer", "content": [
            component("Nodes", nodes=[node("inner secret")])]}])
        observation = await self.parse([nested], understand_forward_max_depth=1)
        self.assertNotIn("inner secret", observation.text)
        self.assertIn("层数上限", observation.text)

    async def test_zero_depth_and_switch_off_do_not_read_forward(self):
        for config in ({"understand_forward_max_depth": 0}, {"understand_expand_forward": False}):
            event = Event()
            event.message_obj.message = [component("Nodes", nodes=[node("隐藏正文")])]
            self.guard.config.update(config)
            observation = await self.reader.parse(event)
            self.assertNotIn("隐藏正文", observation.text)

    async def test_nested_count_shares_one_budget(self):
        inner = component("Nodes", nodes=[node(str(i)) for i in range(10)])
        observation = await self.parse([component("Nodes", nodes=[node("first"),
            {"name": "nested", "content": [inner]}, node("last")])],
            understand_forward_max_nodes=4, understand_forward_max_depth=3)
        self.assertEqual(observation.nodes, 4)
        self.assertNotIn("last", observation.text)

    async def test_cycles_and_duplicate_forward_id_do_not_refetch(self):
        forward = component("Forward", id="id")
        self.event.bot = SimpleNamespace(call_action=AsyncMock(return_value={
            "messages": [{"sender": {"nickname": "A"}, "content": [forward]}]}))
        observation = await self.parse([forward, component("Forward", id="id")],
            understand_forward_max_depth=8)
        self.assertLess(len(observation.text), 200)
        self.event.bot.call_action.assert_awaited_once_with("get_forward_msg", message_id="id")

    async def test_malformed_payloads_are_bounded_and_keep_good_text(self):
        bad = component("Nodes", nodes=10)
        cycle = component("Nodes", nodes=[])
        cycle.nodes.append({"content": [cycle]})
        observation = await self.parse([bad, cycle, Plain("有效末尾")])
        self.assertIn("有效末尾", observation.text)
        self.assertLess(len(observation.text), 500)

    async def test_quote_depth_zero_really_skips_direct_quote(self):
        observation = await self.parse([Reply(id="q", message_str="私密引用")], understand_quote_depth=0)
        self.assertNotIn("私密引用", observation.text)
        self.assertFalse(observation.quoted)

    async def test_quote_chain_depth_one_does_not_read_nested_quote(self):
        observation = await self.parse([Reply(id="q", chain=[Plain("直接引用"),
            Reply(id="q2", message_str="更深引用")])], understand_quote_depth=1)
        self.assertIn("直接引用", observation.text)
        self.assertNotIn("更深引用", observation.text)

    async def test_cards_extract_nested_miniprogram_and_malformed_raw(self):
        observation = await self.parse([component("Json", data={
            "app": "com.tencent.miniapp", "meta": {"detail": {"title": "标题", "qqdocurl": "https://example.invalid"}}}),
            component("Json", data='{不规范内容')])
        self.assertIn("标题", observation.text)
        self.assertIn("https://example.invalid", observation.text)
        self.assertIn("不规范内容", observation.text)

    async def test_forward_summary_keeps_full_archive_and_labels_excerpt(self):
        observation = await self.parse([component("Nodes", nodes=[node("始" + "字" * 2000 + "末")])],
                                       understand_forward_summary_threshold=200)
        self.assertGreater(len(observation.text), 2000)
        self.assertLess(len(observation.prompt), 230)
        self.assertIn("中间未展示", observation.prompt)
        self.assertIn("始", observation.prompt)
        self.assertIn("末", observation.prompt)

    async def test_concurrent_events_use_their_own_bot(self):
        a, b = Event(group="a"), Event(group="b")
        for event, content in ((a, "A内容"), (b, "B内容")):
            event.message_obj.message = [component("Forward", id="same")]
            event.bot = SimpleNamespace(call_action=AsyncMock(return_value={"messages": [node(content)]}))
        first, second = await asyncio.gather(self.reader.parse(a), self.reader.parse(b))
        self.assertIn("A内容", first.text)
        self.assertNotIn("B内容", first.text)
        self.assertIn("B内容", second.text)

    async def test_same_event_concurrent_consumers_share_task(self):
        self.event.message_obj.message = [component("Forward", id="x")]
        self.event.bot = SimpleNamespace(call_action=AsyncMock(return_value={"messages": [node("正文")]}))
        a, b = await asyncio.gather(self.reader.parse(self.event), self.reader.parse(self.event))
        self.assertIs(a, b)
        self.event.bot.call_action.assert_awaited_once()

    async def test_native_stt_uses_get_text_and_own_picker(self):
        stt = SimpleNamespace(get_text=AsyncMock(return_value="语音原话"))
        self.guard.context = SimpleNamespace(get_provider_by_id=Mock(return_value=stt))
        observation = await self.parse([Record(url="audio.wav")],
            understand_voice_enabled=True, understand_voice_provider_id="native-stt")
        await self.reader.enrich(self.event, observation)
        stt.get_text.assert_awaited_once_with("audio.wav")
        self.guard.context.get_provider_by_id.assert_called_once_with("native-stt")
        self.assertIn("语音原话", observation.text)

    async def test_routed_stt_has_real_audio_urls_and_pinned_model(self):
        client = SimpleNamespace(get_model=lambda: "qwen-omni", provider_config={})
        route = SimpleNamespace(upstreams=[SimpleNamespace(client=client)],
            text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="声音原话")))
        self.guard.context = SimpleNamespace(get_provider_by_id=Mock(return_value=route))
        observation = await self.parse([Record(url="test.wav")], understand_voice_enabled=True,
            understand_voice_provider_id="router::qwen-omni")
        await self.reader.enrich(self.event, observation)
        kwargs = route.text_chat.await_args.kwargs
        self.assertEqual(kwargs["audio_urls"], ["test.wav"])
        self.assertEqual(kwargs["model"], "qwen-omni")
        self.assertIn("声音原话", observation.text)

    async def test_missing_pin_does_not_fallback_or_call_model(self):
        route = SimpleNamespace(upstreams=[], text_chat=AsyncMock())
        self.guard.context = SimpleNamespace(get_provider_by_id=Mock(return_value=route))
        observation = await self.parse([Record(url="test.wav")], understand_voice_enabled=True,
            understand_voice_provider_id="router::removed-model")
        await self.reader.enrich(self.event, observation)
        route.text_chat.assert_not_awaited()
        self.assertIn("语音转写失败", observation.prompt)

    async def test_text_only_model_is_not_used_for_transcription(self):
        provider = SimpleNamespace(get_model=lambda: "deepseek-chat",
            provider_config={"modalities": ["audio"]}, text_chat=AsyncMock())
        self.guard.context = SimpleNamespace(get_provider_by_id=Mock(return_value=provider))
        observation = await self.parse([Record(url="test.wav")], understand_voice_enabled=True,
            understand_voice_provider_id="text")
        await self.reader.enrich(self.event, observation)
        provider.text_chat.assert_not_awaited()
        self.assertTrue(observation.errors)

    async def test_vision_uses_own_model_does_not_mutate_shared_provider(self):
        provider = SimpleNamespace(get_model=lambda: "qwen3-vl", provider_config={"modalities": ["image"]},
            set_model=Mock(), text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="可见文字")))
        self.guard.context = SimpleNamespace(get_provider_by_id=Mock(return_value=provider))
        observation = await self.parse([Image(url="image.png")],
            understand_image_enabled=True, understand_image_provider_id="own-vision")
        await self.reader.enrich(self.event, observation)
        provider.text_chat.assert_awaited_once()
        self.assertEqual(provider.text_chat.await_args.kwargs["image_urls"], ["image.png"])
        provider.set_model.assert_not_called()
        self.assertIn("可见文字", observation.text)

    async def test_attach_has_untrusted_tags_and_timings_without_repeated_enrichment(self):
        self.event.message_obj.message = [Plain("用户输入")]
        req = SimpleNamespace(extra_user_content_parts=[])
        await self.reader.attach(self.event, req)
        await self.reader.attach(self.event, req)
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertIn('untrusted="true"', req.extra_user_content_parts[0].text)
        self.assertIn("input_parse", self.event.get_extra("_at_guard_input_timings"))


class ExpressionMediaTest(unittest.IsolatedAsyncioTestCase):
    async def test_media_plugins_are_never_selected_cancelled_or_counted(self):
        guard = plugin(gate_config(expression_takeover_enabled=True, expression_sticker_enabled=True,
                                   expression_voice_enabled=True))
        guard._delivery()
        stealer = SimpleNamespace(
            meme_selector=SimpleNamespace(is_path_allowed_for_event=Mock(return_value=False)),
            is_send_enabled_for_event=lambda _: True,
            _search_meme_candidates=AsyncMock(return_value=[("/private/image", "desc", "emotion", [])]))
        guard.context = SimpleNamespace(get_registered_star=lambda _: SimpleNamespace(activated=True, star_cls=stealer))
        media = Image.fromURL("https://example.invalid/sticker")
        result = Chain([Plain("安慰😊(・ω・)\n原字。"), media, Record(url="voice.wav")])
        await guard.expression.decorate(Event(), result)
        self.assertEqual(result.chain[0].text, "安慰\n原字。")
        self.assertIs(result.chain[1], media)
        stealer._search_meme_candidates.assert_not_awaited()
        stealer.meme_selector.is_path_allowed_for_event.assert_not_called()

    async def test_current_congling_event_skips_independent_understanding(self):
        guard = plugin(gate_config(expression_takeover_enabled=True))
        native = SimpleNamespace(enabled_for=lambda event: event.get_group_id() == "native")
        guard.context = SimpleNamespace(get_registered_star=lambda _: SimpleNamespace(
            activated=True, star_cls=native))
        reader = guard._understanding()
        with unittest.mock.patch.object(reader, "parse", new_callable=AsyncMock) as parse:
            req = SimpleNamespace(extra_user_content_parts=[])
            await reader.attach(Event(group="native"), req)
            parse.assert_not_awaited()
            self.assertEqual(req.extra_user_content_parts, [])
        req = SimpleNamespace(extra_user_content_parts=[])
        await reader.attach(Event(group="independent"), req)
        self.assertEqual(len(req.extra_user_content_parts), 1)


class HelpTest(unittest.TestCase):
    def test_all_new_fields_have_two_examples_and_native_schema(self):
        schema = _load_schema_subset()
        for key in EDITABLE:
            if key.startswith(("expression_", "understand_", "reply_typing_", "reply_recall_")):
                self.assertIn(key, schema)
                self.assertIn(key, HELP)
                self.assertEqual(len(HELP[key]), 3)
                self.assertTrue(all(HELP[key]))

    def test_explicit_text_model_is_not_audio(self):
        self.assertFalse(audio_capable(SimpleNamespace(get_model=lambda: "Qwen3-8B", provider_config={})))
