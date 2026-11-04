"""三层参与判断回归：模型替身验证调用顺序，绝不向群聊发送消息。"""
import asyncio
import json
import time
import unittest
from types import SimpleNamespace

from astrbot.api.message_components import Image, Reply
from guard_engine import GateEngine, GateProvider
from guard_media import image_target, StrictVisionProvider
from test_gate import GateEvent, gate_config, plugin
from test_context_policy import output


NEED_IMAGE = output("silent", 30, False, "media_unresolved")
IMAGE = dict(url="https://example.invalid/image.png", source="current",
             sender_id="alice", message_id="1", age_seconds="0")


class Provider:
    def __init__(self, *values, model="qwen3-vl-plus", modalities=None):
        self.values = iter(values)
        self.requests = []
        self.model = model
        self.provider_config = {"id": model}
        if modalities is not None:
            self.provider_config["modalities"] = modalities

    def get_model(self):
        return self.model

    async def text_chat(self, **kwargs):
        self.requests.append(kwargs)
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(completion_text=value)


class MultimodalTest(unittest.TestCase):
    def run_case(self, text=None, vision=None, main=None, images=None, memory=None,
                 dry_run=False, current="看看这张图", **config):
        text = text or Provider(NEED_IMAGE, model="Qwen/Qwen3-8B")
        vision = vision or Provider(output())
        main = main or Provider(output(), model="gemini-2.5-flash")
        images = [IMAGE] if images is None else images
        engine = GateEngine(gate_config(
            classifier_provider_id="text::Qwen/Qwen3-8B",
            vision_provider_id="vision::qwen3-vl-plus", **config),
            None, SimpleNamespace(debug=lambda *_: None))

        async def resolve_text():
            return text

        async def resolve_vision():
            return image_target(vision, "vision::qwen3-vl-plus")

        async def resolve_main():
            return image_target(main)

        async def resolve_images():
            return images

        d = asyncio.run(engine.decide(
            group_key="test", current_text=current, context_lines=[], bot_line="",
            is_private=False, resolve_provider=resolve_text,
            resolve_vision_provider=resolve_vision, resolve_main_provider=resolve_main,
            resolve_images=resolve_images, resolve_memory=memory, dry_run=dry_run,
            message_meta={"has_images": bool(images)},
        ))
        return engine, d, text, vision, main

    def test_text_sufficient_never_calls_vision(self):
        _, d, text, vision, main = self.run_case(text=Provider(output()))
        self.assertEqual(d.stage, "text")
        self.assertEqual((len(text.requests), len(vision.requests), len(main.requests)), (1, 0, 0))
        self.assertNotIn("image_urls", text.requests[0])

    def test_text_silent_never_escalates_without_media_reason(self):
        _, d, _, vision, main = self.run_case(
            text=Provider(output("silent", 12, True, "others_talking")))
        self.assertEqual(d.action, "silence")
        self.assertEqual(len(vision.requests) + len(main.requests), 0)

    def test_needed_images_go_to_vision_directly(self):
        _, d, text, vision, main = self.run_case()
        self.assertEqual((d.stage, d.action, d.model_calls), ("vision", "reply", 2))
        self.assertEqual(d.model, "qwen3-vl-plus::qwen3-vl-plus")
        self.assertNotIn("image_urls", text.requests[0])
        self.assertEqual(vision.requests[0]["image_urls"], [IMAGE["url"]])
        self.assertEqual(len(main.requests), 0)
        self.assertNotIn(IMAGE["url"], d.context)

    def test_pure_image_is_not_blocked_by_text_rules(self):
        _, d, text, _, _ = self.run_case(current="")
        self.assertEqual(d.stage, "vision")
        self.assertEqual(len(text.requests), 1)

    def test_explicit_need_vision_supported(self):
        value = json.loads(output("silent", 30, False, "context_missing"))
        value["need_vision"] = True
        _, d, _, _, _ = self.run_case(text=Provider(json.dumps(value)))
        self.assertEqual(d.stage, "vision")

    def test_vision_silent_keeps_veto(self):
        _, d, _, _, main = self.run_case(
            vision=Provider(output("silent", 99, True, "others_talking")))
        self.assertEqual((d.action, d.score), ("silence", 20))
        self.assertEqual(len(main.requests), 0)

    def test_vision_unresolved_is_valid_silence(self):
        _, d, _, _, main = self.run_case(vision=Provider(NEED_IMAGE))
        self.assertEqual(d.reason, "media_unresolved")
        self.assertEqual(len(main.requests), 0)

    def test_vision_exception_falls_back_once(self):
        _, d, _, _, main = self.run_case(vision=Provider(RuntimeError("test")))
        self.assertEqual((d.stage, d.action, d.model_calls), ("main_fallback", "reply", 3))
        self.assertEqual([x["status"] for x in d.attempts], ["ok", "error", "ok"])
        self.assertEqual(main.requests[0]["image_urls"], [IMAGE["url"]])

    def test_vision_timeout_falls_back_once(self):
        _, d, _, _, _ = self.run_case(vision=Provider(asyncio.TimeoutError()))
        self.assertEqual(d.stage, "main_fallback")
        self.assertEqual(d.attempts[1]["status"], "timeout")

    def test_invalid_vision_format_falls_back(self):
        _, d, _, _, _ = self.run_case(vision=Provider("90"))
        self.assertEqual(d.stage, "main_fallback")
        self.assertEqual(d.attempts[1]["status"], "parse_error")

    def test_failed_main_stays_silent(self):
        _, d, _, _, main = self.run_case(
            vision=Provider(RuntimeError()), main=Provider("bad", model="gpt-4o"))
        self.assertEqual((d.action, d.reason), ("silence", "parse_error"))
        self.assertEqual(len(main.requests), 1)

    def test_text_failure_does_not_burn_vision_budget(self):
        _, d, _, vision, main = self.run_case(text=Provider("bad"))
        self.assertEqual(d.reason, "parse_error")
        self.assertEqual(len(vision.requests) + len(main.requests), 0)

    def test_missing_image_does_not_call_visual_models(self):
        _, d, _, vision, main = self.run_case(images=[])
        self.assertEqual(d.reason, "image_unavailable")
        self.assertEqual(len(vision.requests) + len(main.requests), 0)

    def test_disabled_fallback(self):
        _, d, _, _, main = self.run_case(
            vision=Provider(RuntimeError()), gate_main_fallback_enabled=False)
        self.assertEqual(d.action, "silence")
        self.assertEqual(len(main.requests), 0)

    def test_text_only_main_cannot_be_fallback(self):
        _, d, _, _, main = self.run_case(
            vision=Provider(RuntimeError()), main=Provider(output(), model="deepseek-chat"))
        self.assertEqual(d.reason, "main_no_vision")
        self.assertEqual(len(main.requests), 0)

    def test_total_budget_applies_to_vision(self):
        _, d, _, vision, _ = self.run_case(gate_hourly_budget=1)
        self.assertEqual(d.reason, "budget")
        self.assertEqual(len(vision.requests), 0)

    def test_visual_budget_applies_to_main(self):
        _, d, _, _, main = self.run_case(
            vision=Provider(RuntimeError()), gate_vision_hourly_budget=1)
        self.assertEqual(d.reason, "vision_budget")
        self.assertEqual(len(main.requests), 0)

    def test_dryrun_does_not_update_counts(self):
        engine, d, _, _, _ = self.run_case(dry_run=True)
        self.assertEqual(d.model_calls, 2)
        self.assertEqual(engine.stats["judged"], 0)
        self.assertEqual(engine.budget_usage(), {})

    def test_memory_after_vision_is_bounded_and_keeps_images(self):
        async def memory():
            return ["本群昨天约定一起检查图中的错误"]

        _, d, _, vision, main = self.run_case(
            vision=Provider(output("silent", 30, False, "context_missing", "memory"), output()),
            memory=memory)
        self.assertEqual(d.model_calls, 3)
        self.assertEqual(len(vision.requests), 2)
        self.assertEqual(vision.requests[1]["image_urls"], [IMAGE["url"]])
        self.assertTrue(d.memory_grounded)
        self.assertEqual(len(main.requests), 0)

    def test_log_uses_actual_stage_and_model(self):
        engine, d, _, _, _ = self.run_case()
        engine.write_log(d, group_key="test", mode="enforce", generation_skipped=False,
                         model="text")
        row = engine.log.recent(1)[0]
        self.assertEqual(row["mdl"], d.model)
        self.assertEqual(row["stage"], "vision")
        self.assertNotIn(IMAGE["url"], json.dumps(row))

    def test_same_failed_model_is_not_retried(self):
        p = Provider(RuntimeError())
        _, d, _, _, main = self.run_case(vision=p, main=p)
        self.assertEqual(d.reason, "fallback_same_model")
        self.assertEqual(len(main.requests), 1)

    def test_total_deadline_stops_slow_text_without_escalation(self):
        class Slow(Provider):
            async def text_chat(self, **kwargs):
                await asyncio.sleep(2)
                return SimpleNamespace(completion_text=NEED_IMAGE)
        _, d, _, vision, main = self.run_case(text=Slow(), gate_total_timeout_seconds=1)
        self.assertEqual(d.action, "silence")
        self.assertLess(d.latency_ms, 1500)
        self.assertEqual(len(vision.requests) + len(main.requests), 0)


class ImageSourceTest(unittest.TestCase):
    def setUp(self):
        self.plugin = plugin(gate_config())

    def test_current_and_reply_are_mapped(self):
        event = GateEvent(mentions=[
            Image.fromURL("https://example.invalid/current"),
            Reply(id="2", sender_id="alice", chain=[
                Image.fromURL("https://example.invalid/reply")]),
        ])
        sources = self.plugin._gate_images(event, "test")
        self.assertEqual([x["source"] for x in sources], ["current", "reply"])
        self.assertEqual(sources[1]["sender_id"], "alice")
        meta = self.plugin._message_meta(event)
        self.assertTrue(meta["has_images"])
        self.assertEqual(meta["reply_to"][0]["unread_media"], ["Image"])

    def test_old_reply_image_is_not_used(self):
        event = GateEvent(mentions=[Reply(
            id="2", time=int(time.time()) - 1000, chain=[
                Image.fromURL("https://example.invalid/old")])])
        self.assertEqual(self.plugin._gate_images(event, "test"), [])

    def test_same_group_recent_images_only(self):
        event = GateEvent(mentions=[Image.fromURL("https://example.invalid/recent")])
        self.plugin._record_incoming(event, "group-a")
        current = GateEvent()
        self.assertEqual(len(self.plugin._gate_images(current, "group-a")), 1)
        self.assertEqual(self.plugin._gate_images(current, "group-b"), [])

    def test_images_are_deduplicated_and_limited(self):
        event = GateEvent(mentions=[Image.fromURL(f"https://example.invalid/{i}") for i in range(10)])
        self.assertEqual(len(self.plugin._gate_images(event, "test")), 3)

    def test_explicit_text_modality_overrides_model_name(self):
        p = Provider(model="gpt-4o", modalities=["text"])
        with self.assertRaises(Exception):
            image_target(p)

    def test_mixed_router_pins_visual_upstream(self):
        text = Provider(model="Qwen/Qwen3-8B")
        vision = Provider()
        router = SimpleNamespace(provider_config={"id": "router"},
                                 upstreams=[SimpleNamespace(client=text),
                                            SimpleNamespace(client=vision)])
        target = image_target(router)
        self.assertEqual(target.model, "qwen3-vl-plus")
        with self.assertRaises(Exception):
            image_target(router, "router::nonexistent-vl")

    def test_strict_native_adapter_does_not_remove_images_on_error(self):
        calls = []

        async def prepare(*args, **kwargs):
            calls.append(("prepare", args[1]))
            return {"messages": [{"role": "user", "content": args[1]}]}, []

        async def query(payload, tools, **kwargs):
            calls.append(("query", kwargs["request_max_retries"]))
            raise RuntimeError("The model is not a VLM")

        native = SimpleNamespace(_prepare_chat_payload=prepare, _query=query)
        with self.assertRaises(RuntimeError):
            asyncio.run(StrictVisionProvider(native).text_chat(
                prompt="看图", image_urls=["/test.png"], system_prompt="规则", model="test"))
        self.assertEqual(calls, [("prepare", ["/test.png"]), ("query", 1)])

    def test_zero_context_disables_recent_images(self):
        p = plugin(gate_config(gate_context_messages=0))
        p._record_incoming(GateEvent(mentions=[
            Image.fromURL("https://example.invalid/recent")]), "test")
        self.assertEqual(p._gate_images(GateEvent(), "test"), [])

    def test_event_pipeline_passes_images_to_classifier_and_reply(self):
        p = plugin(gate_config(
            classifier_provider_id="text::Qwen/Qwen3-8B",
            vision_provider_id="vision::qwen3-vl-plus"))
        text = Provider(NEED_IMAGE, model="Qwen/Qwen3-8B")
        vision = Provider(output())
        p.context = SimpleNamespace(get_provider_by_id=lambda name:
                                    {"text": text, "vision": vision}[name])
        event = GateEvent(mentions=[Reply(id="2", sender_id="alice", chain=[
            Image.fromURL("https://example.invalid/reply")])])
        req = SimpleNamespace(image_urls=[], extra_user_content_parts=[])
        asyncio.run(p.guard_llm_request(event, req))
        self.assertFalse(event.stopped)
        self.assertEqual(req.image_urls, ["https://example.invalid/reply"])
        self.assertNotIn("image_urls", text.requests[0])
        self.assertEqual(vision.requests[0]["image_urls"], req.image_urls)
        self.assertEqual(event.extra["_at_guard_decision"]["stage"], "vision")

    def test_event_pipeline_does_not_send_on_visual_silence(self):
        p = plugin(gate_config(
            classifier_provider_id="text::Qwen/Qwen3-8B",
            vision_provider_id="vision::qwen3-vl-plus"))
        providers = {"text": Provider(NEED_IMAGE),
                     "vision": Provider(output("silent", 5, True, "no_value"))}
        p.context = SimpleNamespace(get_provider_by_id=providers.get)
        event = GateEvent(mentions=[Image.fromURL("https://example.invalid/image")])
        req = SimpleNamespace(image_urls=[], extra_user_content_parts=[])
        asyncio.run(p.guard_llm_request(event, req))
        self.assertTrue(event.stopped)
        self.assertEqual(req.image_urls, [])


if __name__ == "__main__":
    unittest.main()
