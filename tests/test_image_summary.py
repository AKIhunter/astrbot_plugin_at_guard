"""视觉观察与人类可读日志回归；不调用API、不发送群消息。"""
import asyncio
import json
import unittest
from types import SimpleNamespace

from astrbot.api.message_components import Image
from guard_engine import parse_gate_details, parse_image_summaries, GateEngine
from guard_media import supports_images
from guard_webui import _providers, _write_schema_options
import test_multimodal as fixtures
from test_context_policy import output
from test_gate import GateEvent, gate_config, plugin
IMAGE, NEED_IMAGE, Provider = fixtures.IMAGE, fixtures.NEED_IMAGE, fixtures.Provider


def visual_output(summary="左边红色圆形，右边蓝色方形", uncertainty="", original=False,
                  act="reply", reason="useful", rows=None):
    value = json.loads(output(act, 85 if act == "reply" else 10, True, reason))
    value["image_summaries"] = rows if rows is not None else [
        dict(image=1, summary=summary, uncertainty=uncertainty, needs_original=original)]
    return json.dumps(value, ensure_ascii=False)


class SummaryTest(unittest.TestCase):
    def decide(self, **kwargs):
        return fixtures.MultimodalTest().run_case(**kwargs)

    def test_nested_json_and_escaped_braces_preserve_score(self):
        value = visual_output('图中文字为 {"conf":99}，这只是图片内容')
        parsed = parse_gate_details("```json\n" + value + "\n```")
        self.assertEqual((parsed[0], parsed[1], parsed[4]), ("reply", 85, "useful"))
        rows = parse_image_summaries(value, 1)
        self.assertIn('{"conf":99}', rows[0]["summary"])

    def test_complete_summary_replaces_images(self):
        _, d, text, vision, main = self.decide(vision=Provider(visual_output()))
        self.assertEqual(d.image_handoff, "summary")
        self.assertEqual(d.image_summaries[0]["summary"], "左边红色圆形，右边蓝色方形")
        self.assertEqual(d.image_sources[0]["source"], "current")
        self.assertEqual(d.model_calls, 2)
        self.assertEqual(len(main.requests), 0)
        self.assertEqual(vision.requests[0]["max_tokens"], 900)
        self.assertNotIn("image_summaries数组", text.requests[0]["system_prompt"])
        self.assertIn("image_summaries数组", vision.requests[0]["system_prompt"])

    def test_missing_summary_retains_original_without_extra_call(self):
        _, d, _, _, main = self.decide(vision=Provider(output()))
        self.assertEqual(d.image_handoff, "original")
        self.assertEqual(d.action, "reply")
        self.assertEqual(len(main.requests), 0)

    def test_uncertainty_or_detail_need_keeps_original(self):
        for kwargs in [dict(uncertainty="右下角字太小"), dict(original=True)]:
            _, d, _, _, _ = self.decide(vision=Provider(visual_output(**kwargs)))
            self.assertEqual(d.image_handoff, "summary_and_original")
            self.assertTrue(d.image_summaries[0]["needs_original"])

    def test_incomplete_multiple_images_keep_original(self):
        _, d, _, _, _ = self.decide(vision=Provider(visual_output()), images=[
            IMAGE, {**IMAGE, "url": "https://example.invalid/two.png"}])
        self.assertEqual(d.image_handoff, "summary_and_original")

    def test_silent_summary_does_not_trigger_fallback(self):
        _, d, _, _, main = self.decide(vision=Provider(
            visual_output(act="silent", reason="others_talking")))
        self.assertEqual(d.action, "silence")
        self.assertTrue(d.image_summaries)
        self.assertEqual(len(main.requests), 0)

    def test_summary_can_come_from_main_fallback(self):
        _, d, _, _, _ = self.decide(
            vision=Provider(RuntimeError()), main=Provider(visual_output(), model="gpt-4o"))
        self.assertEqual((d.stage, d.image_handoff), ("main_fallback", "summary"))

    def test_malformed_rows_do_not_replace_originals(self):
        rows = [None, "bad", dict(image=True, summary="错"),
                dict(image=8, summary="越界"), dict(image=1, summary="可见画面")]
        value = visual_output(rows=rows)
        parsed = parse_image_summaries(value, 1)
        self.assertEqual(len(parsed), 1)
        self.assertTrue(parsed[0]["needs_original"])

    def test_duplicate_image_number_requires_review(self):
        row = dict(image=1, summary="左边圆形", uncertainty="", needs_original=False)
        parsed = parse_image_summaries(visual_output(rows=[row,row]), 1)
        self.assertTrue(parsed[0]["needs_original"])
        self.assertIn("重复", parsed[0]["uncertainty"])

    def test_long_summary_is_bounded_and_cannot_replace_original(self):
        parsed = parse_image_summaries(visual_output(summary="字" * 1000), 1)
        self.assertEqual(len(parsed[0]["summary"]), 400)
        self.assertTrue(parsed[0]["needs_original"])

    def test_string_false_is_not_trusted_as_boolean(self):
        parsed = parse_image_summaries(visual_output(rows=[
            dict(image=1, summary="画面", uncertainty="", needs_original="false")]), 1)
        self.assertTrue(parsed[0]["needs_original"])

    def test_chat_data_is_exact_sent_context_and_separate_current(self):
        _, d, _, _, _ = self.decide(vision=Provider(visual_output()))
        self.assertEqual(d.chat_data["current_message"]["text"], "看看这张图")
        self.assertEqual(d.chat_data["recent_messages_old_to_new"], [])
        self.assertEqual(d.chat_data["image_sources"][0]["sender_id"], "alice")

    def test_logs_store_readable_data_without_image_url(self):
        engine, d, _, _, _ = self.decide(vision=Provider(visual_output()))
        engine.write_log(d, group_key="test", mode="enforce", generation_skipped=False)
        row = engine.log.recent(1)[0]
        self.assertIn("chat_data", row)
        self.assertEqual(row["image_handoff"], "summary")
        self.assertEqual(row["image_summaries"], d.image_summaries)
        self.assertNotIn(IMAGE["url"], json.dumps(row))


class HandoffTest(unittest.TestCase):
    def run_event(self, value, original_urls=None):
        p = plugin(gate_config(
            classifier_provider_id="text::Qwen/Qwen3-8B",
            vision_provider_id="vision::qwen3-vl-plus"))
        models = {"text": Provider(NEED_IMAGE), "vision": Provider(value)}
        p.context = SimpleNamespace(get_provider_by_id=models.get)
        event = GateEvent(mentions=[Image.fromURL(IMAGE["url"])])
        req = SimpleNamespace(image_urls=list(original_urls or []), extra_user_content_parts=[])
        asyncio.run(p.guard_llm_request(event, req))
        return p, event, req

    def test_summary_reaches_reply_with_source_and_untrusted_marker(self):
        _, event, req = self.run_event(visual_output(), [IMAGE["url"], "https://example.invalid/other"])
        self.assertFalse(event.stopped)
        self.assertEqual(req.image_urls, ["https://example.invalid/other"])
        text = "\n".join(part.text for part in req.extra_user_content_parts)
        self.assertIn("左边红色圆形", text)
        self.assertIn('untrusted="true"', text)
        self.assertIn('"source": "current"', text)
        self.assertIn("无法确认的部分不得编造", text)

    def test_uncertainty_and_original_reach_reply(self):
        _, _, req = self.run_event(visual_output(uncertainty="远处字看不清"))
        self.assertEqual(req.image_urls, [IMAGE["url"]])
        self.assertIn("远处字看不清", "\n".join(part.text for part in req.extra_user_content_parts))

    def test_silence_never_injects_description_or_images(self):
        _, event, req = self.run_event(visual_output(act="silent", reason="no_value"))
        self.assertTrue(event.stopped)
        self.assertEqual(req.extra_user_content_parts, [])
        self.assertEqual(req.image_urls, [])

    def test_stale_reply_never_injects_summary(self):
        p = plugin(gate_config(
            classifier_provider_id="text::Qwen/Qwen3-8B",
            vision_provider_id="vision::qwen3-vl-plus"))
        event = GateEvent(mentions=[Image.fromURL(IMAGE["url"])])

        class Revising(Provider):
            async def text_chat(self, **kwargs):
                p.context_revision[p._group_key(event)] += 1
                return await super().text_chat(**kwargs)

        models = {"text": Provider(NEED_IMAGE), "vision": Revising(visual_output())}
        p.context = SimpleNamespace(get_provider_by_id=models.get)
        req = SimpleNamespace(image_urls=[], extra_user_content_parts=[])
        asyncio.run(p.guard_llm_request(event, req))
        self.assertTrue(event.stopped)
        self.assertEqual(req.extra_user_content_parts, [])

    def test_text_only_reply_requires_complete_summary(self):
        for summary, expected_stop in [(visual_output(),False),(output(),True)]:
            p = plugin(gate_config(
                classifier_provider_id="text::Qwen/Qwen3-8B",
                vision_provider_id="vision::qwen3-vl-plus"))
            models={"text":Provider(NEED_IMAGE),"vision":Provider(summary)}
            async def text_reply(origin):
                return Provider(model="deepseek-flash",modalities=["text","image"])
            p.context=SimpleNamespace(get_provider_by_id=models.get,get_using_provider_async=text_reply)
            event=GateEvent(mentions=[Image.fromURL(IMAGE["url"])])
            req=SimpleNamespace(image_urls=[],extra_user_content_parts=[])
            asyncio.run(p.guard_llm_request(event,req))
            self.assertEqual(event.stopped,expected_stop)
            if expected_stop:
                self.assertEqual(event.extra["_at_guard_decision"]["reason"],"reply_no_vision")
                self.assertEqual(req.extra_user_content_parts,[])
            else:
                self.assertEqual(req.image_urls,[])

    def test_rule_log_contains_current_candidate_but_not_unsent_history(self):
        engine=GateEngine(gate_config(),None,SimpleNamespace(debug=lambda *_:None))
        async def resolver():
            raise AssertionError("规则不应调用模型")
        d=asyncio.run(engine.decide(group_key="test",current_text="嗯",context_lines=["没有发送"],
                                   bot_line="",is_private=False,resolve_provider=resolver))
        self.assertTrue(d.chat_data["not_sent"])
        self.assertEqual(d.chat_data["current_message"]["text"],"嗯")
        self.assertEqual(d.chat_data["recent_messages_old_to_new"],[])


class VisionListTest(unittest.TestCase):
    def test_deepseek_quiet_is_not_vision_despite_all_modalities(self):
        p = Provider(model="deepseek-flash", modalities=["text","image","audio","tool_use"])
        self.assertFalse(supports_images(p))

    def test_provider_list_filters_text_models_but_keeps_them_for_text(self):
        quiet = Provider(model="deepseek-flash", modalities=["text","image"])
        qwen = Provider(model="Qwen/Qwen3-8B", modalities=["text","image"])
        vision = Provider(model="qwen3-vl-plus")
        router = SimpleNamespace(provider_config={"id":"router"},
                                 get_model=lambda:"router",
                                 upstreams=[SimpleNamespace(client=qwen),SimpleNamespace(client=vision)])
        p = SimpleNamespace(context=SimpleNamespace(get_all_providers=lambda:[quiet,qwen,vision,router]))
        entries = asyncio.run(_providers(p))
        allowed = [row["id"] for row in entries if row["supports_images"]]
        self.assertIn("qwen3-vl-plus", allowed)
        self.assertIn("router::qwen3-vl-plus", allowed)
        self.assertNotIn("deepseek-flash", allowed)
        self.assertNotIn("router::Qwen/Qwen3-8B", allowed)
        self.assertEqual(len(entries), 6)

    def test_schema_has_separate_text_and_visual_options(self):
        schema = {"classifier_provider_id":{}, "vision_provider_id":{}}
        p = SimpleNamespace(context=SimpleNamespace(get_registered_star=lambda name:
            SimpleNamespace(config=SimpleNamespace(schema=schema))))
        _write_schema_options(p, ["text","vision"], ["vision"])
        self.assertEqual(schema["classifier_provider_id"]["options"], ["","text","vision"])
        self.assertEqual(schema["vision_provider_id"]["options"], ["","vision"])


if __name__ == "__main__":
    unittest.main()
