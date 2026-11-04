"""图片观察缓存不复用参与结论；使用替身模型及时间，不发送真实消息。"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot.api.message_components import Image
from guard_image_cache import ImageObservationCache
from guard_engine import GateEngine
from test_gate import GateEvent, gate_config, plugin
from test_multimodal import Provider, IMAGE, NEED_IMAGE
from test_context_policy import output
from test_image_summary import visual_output


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.now = 10
        self.cache = ImageObservationCache(max_entries=2, clock=lambda: self.now)
        self.observation = [dict(image=1, summary="左边红色圆形", uncertainty="", needs_original=False)]
        self.cache.store("group-a", "vl|v5", [IMAGE], self.observation, "vl", 300)

    def test_hit_contains_facts_but_no_reply_score(self):
        rows = self.cache.lookup("group-a", "vl|v5", [IMAGE], 300)
        self.assertEqual(rows[0]["summary"], "左边红色圆形")
        self.assertTrue(rows[0]["cached"])
        self.assertNotIn("action", rows[0])
        self.assertNotIn("score", rows[0])

    def test_scope_model_and_image_are_isolated(self):
        self.assertEqual(self.cache.lookup("group-b", "vl|v5", [IMAGE], 300), [])
        self.assertEqual(self.cache.lookup("group-a", "other-vl|v5", [IMAGE], 300), [])
        self.assertEqual(self.cache.lookup("group-a", "vl|other-policy", [IMAGE], 300), [])
        self.assertEqual(self.cache.lookup("group-a", "vl|v5", [{**IMAGE, "url": "https://example.invalid/other"}], 300), [])

    def test_ttl_does_not_extend_on_read(self):
        self.now = 309
        self.assertEqual(len(self.cache.lookup("group-a", "vl|v5", [IMAGE], 300)), 1)
        self.now = 310
        self.assertEqual(self.cache.lookup("group-a", "vl|v5", [IMAGE], 300), [])

    def test_ttl_zero_and_shorter_setting_take_effect(self):
        self.assertEqual(self.cache.lookup("group-a", "vl|v5", [IMAGE], 0), [])
        self.now = 21
        self.assertEqual(self.cache.lookup("group-a", "vl|v5", [IMAGE], 10), [])

    def test_capacity_evicts_and_values_are_not_shared(self):
        result = self.cache.lookup("group-a", "vl|v5", [IMAGE], 300)
        result[0]["summary"] = "恶意替换"
        self.assertEqual(self.cache.lookup("group-a", "vl|v5", [IMAGE], 300)[0]["summary"], "左边红色圆形")
        for suffix in ("b", "c"):
            self.cache.store("group-a", "vl|v5", [{**IMAGE, "url": suffix}],
                             self.observation, "vl", 300)
        self.assertEqual(self.cache.snapshot()["entries"], 2)
        self.assertEqual(self.cache.snapshot()["evicted"], 1)

    def test_new_instance_does_not_load_stale_disk_data(self):
        self.assertEqual(ImageObservationCache().snapshot()["entries"], 0)

    def test_local_file_changed_invalidate(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "image.png"
            file.write_bytes(b"one")
            images = [{**IMAGE, "url": file.as_uri()}]
            self.cache.store("group-a", "vl|v5", images, self.observation, "vl", 300)
            file.write_bytes(b"two-new")
            self.assertEqual(self.cache.lookup("group-a", "vl|v5", images, 300), [])

    def test_session_lock_serializes_and_is_released_on_cancel(self):
        async def check():
            order = []
            async def fill(label):
                async with self.cache.session_lock("same"):
                    order.append(label+"start")
                    await asyncio.sleep(0)
                    order.append(label+"end")
            await asyncio.gather(fill("a"), fill("b"))
            self.assertEqual(order, ["astart", "aend", "bstart", "bend"])
            self.assertEqual(self.cache.locks, {})
            async with self.cache.session_lock("same"):
                with self.assertRaises(asyncio.TimeoutError):
                    async with self.cache.session_lock("same", timeout=0.01):
                        pass
            self.assertEqual(self.cache.locks, {})
        asyncio.run(check())


class FlowTest(unittest.TestCase):
    def make_plugin(self, text_values, vision_values, ttl=300):
        p = plugin(gate_config(
            classifier_provider_id="text::Qwen/Qwen3-8B",
            vision_provider_id="vision::qwen3-vl-plus",
            gate_image_cache_ttl_seconds=ttl, gate_min_length=1))
        models = {"text": Provider(*text_values), "vision": Provider(*vision_values)}
        p.context = SimpleNamespace(get_provider_by_id=models.get)
        p.image_cache = ImageObservationCache()
        return p, models

    def event(self, text="这图左边是什么颜色？", group="1001"):
        return GateEvent(text=text, group=group, mentions=[Image.fromURL(IMAGE["url"])])

    async def run_one(self, p, event):
        p.proactive_inflight_until.clear()
        req = SimpleNamespace(image_urls=[IMAGE["url"]], extra_user_content_parts=[])
        await p.guard_llm_request(event, req)
        return req

    def test_same_picture_new_message_uses_facts_without_second_vision(self):
        p, models = self.make_plugin([NEED_IMAGE, output()], [visual_output()])
        async def check():
            first = self.event()
            await self.run_one(p, first)
            second = self.event("所以这个圆是红色吗？")
            req = await self.run_one(p, second)
            self.assertEqual(len(models["vision"].requests), 1)
            self.assertEqual(second.extra["_at_guard_decision"]["image_cache_hits"], 1)
            self.assertEqual(second.extra["_at_guard_decision"]["stage"], "text")
            self.assertEqual(req.image_urls, [])
            self.assertIn("左边红色圆形", "\n".join(item.text for item in req.extra_user_content_parts))
            prompt = models["text"].requests[1]["prompt"]
            self.assertIn('"cached_image_observations"', prompt)
            self.assertIn("所以这个圆", prompt)
        asyncio.run(check())

    def test_cached_reply_cannot_override_current_silent(self):
        p, models = self.make_plugin([NEED_IMAGE, output("silent", 10, True, "conversation_ended")],
                                    [visual_output()])
        async def check():
            await self.run_one(p, self.event())
            second = self.event("别说了，结束吧")
            req = await self.run_one(p, second)
            self.assertTrue(second.stopped)
            self.assertEqual(req.extra_user_content_parts, [])
            self.assertEqual(len(models["vision"].requests), 1)
        asyncio.run(check())

    def test_silent_but_valid_visual_observation_can_be_reused(self):
        p, models = self.make_plugin([NEED_IMAGE, output()],
            [visual_output(act="silent", reason="others_talking")])
        async def check():
            first = self.event()
            await self.run_one(p, first)
            self.assertTrue(first.stopped)
            second = self.event("那张图的颜色呢？")
            await self.run_one(p, second)
            self.assertFalse(second.stopped)
            self.assertEqual(len(models["vision"].requests), 1)
        asyncio.run(check())

    def test_recent_picture_cache_reaches_text_only_current_message(self):
        p, models = self.make_plugin([NEED_IMAGE, output()], [visual_output()])
        async def check():
            await self.run_one(p, self.event())
            second = GateEvent(text="刚才那个红圆是什么梗？")
            await self.run_one(p, second)
            self.assertIn("左边红色圆形", models["text"].requests[1]["prompt"])
            self.assertEqual(len(models["vision"].requests), 1)
        asyncio.run(check())

    def test_different_group_cannot_reuse_cache(self):
        p, models = self.make_plugin([NEED_IMAGE, NEED_IMAGE], [visual_output(), visual_output()])
        async def check():
            await self.run_one(p, self.event(group="a"))
            second = self.event(group="b")
            await self.run_one(p, second)
            self.assertEqual(second.extra["_at_guard_decision"]["image_cache_hits"], 0)
            self.assertEqual(len(models["vision"].requests), 2)
        asyncio.run(check())

    def test_expired_cache_requires_new_vision(self):
        p, models = self.make_plugin([NEED_IMAGE, NEED_IMAGE], [visual_output(), visual_output()])
        now = [10]
        p.image_cache = ImageObservationCache(clock=lambda: now[0])
        async def check():
            await self.run_one(p, self.event())
            now[0] += 301
            await self.run_one(p, self.event())
            self.assertEqual(len(models["vision"].requests), 2)
        asyncio.run(check())

    def test_disabled_cache_requires_new_vision(self):
        p, models = self.make_plugin([NEED_IMAGE, NEED_IMAGE], [visual_output(), visual_output()], ttl=0)
        async def check():
            await self.run_one(p, self.event())
            await self.run_one(p, self.event())
            self.assertEqual(len(models["vision"].requests), 2)
            self.assertEqual(p.image_cache.snapshot()["entries"], 0)
        asyncio.run(check())

    def test_incomplete_cache_allows_new_vision_for_missing_detail(self):
        p, models = self.make_plugin([NEED_IMAGE, NEED_IMAGE],
            [visual_output(uncertainty="文字看不清"), visual_output()])
        async def check():
            await self.run_one(p, self.event())
            await self.run_one(p, self.event())
            self.assertEqual(len(models["vision"].requests), 2)
        asyncio.run(check())

    def test_vision_error_does_not_cache_false_facts(self):
        p, models = self.make_plugin([NEED_IMAGE, NEED_IMAGE], [RuntimeError(), visual_output()])
        p.config["gate_main_fallback_enabled"] = False
        async def check():
            await self.run_one(p, self.event())
            self.assertEqual(p.image_cache.snapshot()["entries"], 0)
            await self.run_one(p, self.event())
            self.assertEqual(len(models["vision"].requests), 2)
        asyncio.run(check())

    def test_standalone_image_cannot_be_declared_ended_without_seeing_it(self):
        p, models = self.make_plugin([output("silent", 0, True, "conversation_ended")], [visual_output()])
        async def check():
            event = self.event(text="")
            await self.run_one(p, event)
            self.assertEqual(event.extra["_at_guard_decision"]["stage"], "vision")
            self.assertEqual(len(models["vision"].requests), 1)
        asyncio.run(check())

    def test_textual_ending_with_picture_still_does_not_call_vision(self):
        p, models = self.make_plugin([output("silent", 0, True, "conversation_ended")], [])
        async def check():
            event = self.event(text="到此为止，不聊了")
            await self.run_one(p, event)
            self.assertTrue(event.stopped)
            self.assertEqual(models["vision"].requests, [])
        asyncio.run(check())

    def test_image_not_in_actual_bounded_context_is_not_included(self):
        p, _ = self.make_plugin([], [])
        p.config["gate_context_messages"] = 1
        picture = self.event()
        p._record_incoming(picture, "test")
        p._record_incoming(GateEvent("后来的新话题"), "test")
        self.assertEqual(p._gate_images(GateEvent("当前问题"), "test"), [])

    def test_same_group_id_on_different_platform_does_not_share_picture(self):
        p, _ = self.make_plugin([], [])
        first = self.event(group="1001")
        first.unified_msg_origin = "platform-a:group:1001"
        p._record_incoming(first, "1001")
        second = GateEvent("另一个平台的消息", group="1001")
        second.unified_msg_origin = "platform-b:group:1001"
        self.assertEqual(p._gate_images(second, "1001"), [])


if __name__ == "__main__":
    unittest.main()
