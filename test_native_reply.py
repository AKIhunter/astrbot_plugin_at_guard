"""共用原生配置、冲突/失败恢复与确定性回复头的行为回归。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.api.message_components import At, Image, Plain, Reply
from guard_native_reply import NativeReply, NativeReplyError, migrate, validate, second_period_regex
from test_gate import gate_config, plugin
from test_reply_delivery import Chain, Event

NATIVE = dict(enable=False, only_llm_result=False, interval_method="random",
              interval="1.5,3.5", log_base=2.6, words_count_threshold=150,
              split_mode="regex", regex=r".*?[。？！~…]+|.+$",
              split_words=["。", "？", "！", "~", "…"], content_cleanup_rule="")


class Config(dict):
    def save_config(self):
        self.saved = copy.deepcopy(dict(self))
        if getattr(self, "config_path", None):
            Path(self.config_path).write_text(json.dumps(self.saved), encoding="utf-8-sig")

    def _prepare_config_snapshot(self, replace_config=None):
        self.save_revision = getattr(self, "save_revision", 0) + 1
        return copy.deepcopy(dict(self)), self.save_revision

    def _write_config_snapshot(self, snapshot, revision, indent):
        self.saved = copy.deepcopy(snapshot)
        Path(self.config_path).write_text(json.dumps(snapshot), encoding="utf-8-sig")
        return True


class NativeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conf = Config(platform_settings=dict(segmented_reply=copy.deepcopy(NATIVE)),
                           unrelated={"preserve": True})
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.conf.config_path = str(Path(self.folder.name) / "native.json")
        self.conf.save_config()
        self.other = copy.deepcopy(self.conf)
        self.mgr = SimpleNamespace(confs={"default": self.conf, "other": self.other},
                                   get_conf_list=lambda: [
                                       {"id": "default", "name": "默认"}, {"id": "other", "name": "其他"}])
        self.bridge = NativeReply(SimpleNamespace(astrbot_config_mgr=self.mgr))
        self.reload = AsyncMock()
        async def update(config_id, data):
            conf = self.mgr.confs[config_id]
            conf.clear()
            conf.update(data)
            await self.reload(config_id)
        self.service = SimpleNamespace(update_profile=AsyncMock(side_effect=update),
                                       core_lifecycle=SimpleNamespace(reload_pipeline_scheduler=self.reload))
        self.request = SimpleNamespace(_request=SimpleNamespace(app=SimpleNamespace(
            state=SimpleNamespace(services=SimpleNamespace(config_profiles=self.service)))))

    def body(self, **values):
        return {"config_id": "default", "expected_revision": self.bridge.read()["revision"],
                "values": values}

    async def test_update_uses_native_pipeline_and_only_target_profile(self):
        saved = await self.bridge.save(self.request, self.body(enable=True, split_mode="words"))
        self.assertTrue(saved["values"]["enable"])
        self.reload.assert_awaited_once_with("default")
        self.assertEqual(self.other["platform_settings"]["segmented_reply"], NATIVE)
        self.assertEqual(self.conf["unrelated"], {"preserve": True})
        self.assertEqual(set(saved), {"config_id", "values", "revision", "profiles"})
        self.assertNotIn("unrelated", str(saved))

    async def test_second_period_is_native_regex_and_words_changes_recompile(self):
        saved = await self.bridge.save(self.request, self.body(second_period=True))
        self.assertTrue(saved["values"]["second_period"])
        raw = self.conf["platform_settings"]["segmented_reply"]
        self.assertNotIn("second_period", raw)
        self.assertEqual(raw["split_mode"], "regex")
        self.assertEqual(re.findall(raw["regex"], "甲。乙。丙！丁。戊。"), ["甲。乙。", "丙！", "丁。戊。"])
        saved = await self.bridge.save(self.request, self.body(split_words=["。", "END"]))
        self.assertTrue(saved["values"]["second_period"])
        self.assertEqual(re.findall(saved["values"]["regex"], "甲。乙END丙。丁。"),
                         ["甲。乙END", "丙。丁。"])
        saved = await self.bridge.save(self.request, self.body(second_period=False))
        self.assertFalse(saved["values"]["second_period"])
        self.assertEqual(re.findall(saved["values"]["regex"], "甲。乙。"), ["甲。", "乙。"])
        self.assertNotIn("second-period", saved["values"]["regex"])

    async def test_native_editor_changes_second_period_state_and_conflicts(self):
        await self.bridge.save(self.request, self.body(second_period=True))
        stale = self.body(enable=True)
        self.conf["platform_settings"]["segmented_reply"]["regex"] = NATIVE["regex"]
        self.assertFalse(self.bridge.read()["values"]["second_period"])
        with self.assertRaises(NativeReplyError) as error:
            await self.bridge.save(self.request, stale)
        self.assertEqual(error.exception.status, 409)

    async def test_second_period_save_failure_restores_native_regex_and_mode(self):
        normal = self.bridge.persist
        calls = 0
        async def update(svc, config_id, data):
            nonlocal calls
            calls += 1
            await normal(svc, config_id, data)
            if calls == 1:
                raise OSError("refresh failed")
        self.bridge.persist = update
        with self.assertRaises(NativeReplyError):
            await self.bridge.save(self.request, self.body(second_period=True))
        self.assertEqual(self.conf["platform_settings"]["segmented_reply"], NATIVE)

    def test_second_period_boundaries_reset_preserve_text_and_multichar_words(self):
        regex = second_period_regex(["。", "！", "？", "~", "…"])
        fixtures = (
            ("甲。乙。丙。丁。尾", ["甲。乙。", "丙。丁。", "尾"]),
            ("甲。尾", ["甲。尾"]),
            ("甲。", ["甲。"]),
            ("甲？乙。丙。丁！戊。己。", ["甲？", "乙。丙。", "丁！", "戊。己。"]),
            ("甲。乙！丙。丁。", ["甲。乙！", "丙。丁。"]),
            ("甲~乙。丙…丁。戊。", ["甲~", "乙。丙…", "丁。戊。"]),
            ("第一行。\n第二行。\n第三行", ["第一行。\n第二行。", "\n第三行"]),
            ("A.B.C。D.E。", ["A.B.C。D.E。"]),
            ("甲乙丙", ["甲乙丙"]),
            ("", []),
            ("。。。。", ["。。", "。。"]),
        )
        for text, expected in fixtures:
            with self.subTest(text=text):
                result = re.findall(regex, text, re.DOTALL | re.MULTILINE)
                self.assertEqual(result, expected)
                self.assertEqual("".join(result), text)
        for words, text, expected in (
            (["。", "END", "ENDLONG"], "甲。乙ENDLONG丙。丁。", ["甲。乙ENDLONG", "丙。丁。"]),
            (["。", "。END"], "甲。END乙。丙。", ["甲。END", "乙。丙。"]),
            (["！", "[END]"], "甲。乙！丙[END]丁。", ["甲。乙！", "丙[END]", "丁。"]),
        ):
            self.assertEqual(re.findall(second_period_regex(words), text), expected)

    async def test_native_page_change_is_visible_and_old_revision_conflicts(self):
        stale = self.body(enable=True)
        self.conf["platform_settings"]["segmented_reply"]["interval"] = "2,4"
        self.assertEqual(self.bridge.read()["values"]["interval"], "2,4")
        with self.assertRaises(NativeReplyError) as error:
            await self.bridge.save(self.request, stale)
        self.assertEqual(error.exception.status, 409)
        self.service.update_profile.assert_not_awaited()

    async def test_latest_unrelated_settings_survive(self):
        body = self.body(enable=True)
        self.conf["unrelated"] = {"changed_in_native_page": True}
        await self.bridge.save(self.request, body)
        self.assertEqual(self.conf["unrelated"], {"changed_in_native_page": True})

    async def test_failure_after_disk_write_restores_fields_and_reloads(self):
        normal = self.bridge.persist
        calls = 0
        async def fail_once(svc, config_id, data):
            nonlocal calls
            calls += 1
            await normal(svc, config_id, data)
            if calls == 1:
                self.conf["unrelated"]["during_failure"] = True
                raise OSError("saved but refresh failed")
        self.bridge.persist = fail_once
        with self.assertRaisesRegex(NativeReplyError, "已恢复"):
            await self.bridge.save(self.request, self.body(enable=True))
        self.assertFalse(self.bridge.read()["values"]["enable"])
        self.assertTrue(self.conf["unrelated"]["during_failure"])
        self.assertEqual(self.reload.await_count, 2)

    async def test_repeated_service_failure_uses_native_config_and_scheduler_recovery(self):
        self.reload.side_effect = OSError("scheduler failure")
        with self.assertRaises(NativeReplyError):
            await self.bridge.save(self.request, self.body(enable=True))
        self.assertFalse(self.conf["platform_settings"]["segmented_reply"]["enable"])
        self.assertEqual(self.reload.await_count, 3)
        self.assertFalse(self.conf.saved["platform_settings"]["segmented_reply"]["enable"])

    async def test_recovery_disk_and_scheduler_failures_report_actual_state(self):
        self.conf._write_config_snapshot = lambda *_: (_ for _ in ()).throw(OSError("disk failure"))
        with self.assertRaisesRegex(NativeReplyError, "恢复未能落盘"):
            await self.bridge.save(self.request, self.body(enable=True))
        self.reload.assert_not_awaited()
        del self.conf._write_config_snapshot
        self.reload.side_effect = RuntimeError("scheduler failure")
        with self.assertRaisesRegex(NativeReplyError, "已恢复但原生发送流程刷新失败"):
            await self.bridge.save(self.request, self.body(enable=True))
        self.assertFalse(self.conf.saved["platform_settings"]["segmented_reply"]["enable"])

    async def test_config_path_detects_failed_disk_write_and_restores(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "native.json"
            path.write_text(json.dumps(self.conf), encoding="utf-8-sig")
            self.conf.config_path = str(path)
            normal = self.conf._write_config_snapshot
            calls = 0
            def update(data, revision, indent):
                nonlocal calls
                calls += 1
                if calls > 1:
                    return normal(data, revision, indent)
                return True
            self.conf._write_config_snapshot = update
            with self.assertRaisesRegex(NativeReplyError, "已恢复"):
                await self.bridge.save(self.request, self.body(enable=True))
            self.assertFalse(self.bridge.read()["values"]["enable"])
            self.assertFalse(json.loads(path.read_text(encoding="utf-8-sig"))[
                "platform_settings"]["segmented_reply"]["enable"])
            self.assertEqual(calls, 2)

    async def test_validation_and_missing_adapter_never_save(self):
        for patch in ({"other": True}, {"enable": 1}, {"interval": "3,1"},
                      {"interval": "nan,2"}, {"log_base": 1}, {"regex": "(abc)"},
                      {"regex": "["}, {"split_words": []}, {"split_words": [2]},
                      {"words_count_threshold": 1.5}, {"split_mode": "custom"}):
            with self.subTest(patch=patch), self.assertRaises(NativeReplyError):
                validate(patch)
        with self.assertRaises(NativeReplyError) as error:
            await self.bridge.save(SimpleNamespace(), self.body(enable=True))
        self.assertEqual(error.exception.status, 503)
        self.service.update_profile.assert_not_awaited()

    def test_migration_preserves_wait_recall_understanding_and_native_values(self):
        config = dict(expression_quote_probability=.6, expression_sticker_enabled=True,
                      fragment_wait_seconds=3.5, reply_typing_enabled=True,
                      reply_recall_group=True, understand_voice_enabled=True)
        native = {"platform_settings": {"reply_with_quote": False, "reply_with_mention": True,
                                       "segmented_reply": copy.deepcopy(NATIVE)}}
        before = copy.deepcopy(native)
        self.assertTrue(migrate(config, native))
        self.assertFalse(config["reply_quote_enabled"])
        self.assertTrue(config["reply_mention_enabled"])
        self.assertEqual(config["fragment_wait_seconds"], 3.5)
        self.assertTrue(config["reply_typing_enabled"])
        self.assertTrue(config["reply_recall_group"])
        self.assertTrue(config["understand_voice_enabled"])
        self.assertEqual(native, before)
        self.assertFalse(migrate(config, native))


class ReplyModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_four_switches_override_auto_heads_and_only_first_body(self):
        for quote in (False, True):
            for mention in (False, True):
                for native_quote in (False, True):
                    for native_mention in (False, True):
                        guard = plugin(gate_config(expression_takeover_enabled=True,
                            reply_quote_enabled=quote, reply_mention_enabled=mention))
                        event = Event()
                        delivery = guard._delivery()
                        await delivery.prepare(event)
                        delivery.arm(event, event.result)
                        # 模拟宿主创建的新Plain，完全不依赖装饰前组件身份。
                        parts = [Plain("第一句"), Plain("第二句")]
                        for i, body in enumerate(parts):
                            heads = ([Reply(id="m1")] if native_quote else []) + (
                                [At(qq="123")] if native_mention else [])
                            await event.send(event.result.derive(heads + [body]))
                        self.assertEqual(len(event.sent), 2)
                        self.assertEqual(sum(isinstance(c, Reply) for c in event.sent[0].chain), int(quote))
                        self.assertEqual(sum(isinstance(c, At) for c in event.sent[0].chain), int(mention))
                        self.assertFalse(any(isinstance(c, (Reply, At)) for c in event.sent[1].chain))
                        self.assertEqual(event.sent[0].chain[-1].text, "第一句")

    async def test_independent_media_send_and_semantic_heads_are_preserved(self):
        guard = plugin(gate_config(expression_takeover_enabled=True, reply_quote_enabled=False,
                                   reply_mention_enabled=False, reply_typing_enabled=True))
        event = Event()
        semantic_quote, semantic_at = Reply(id="m1"), At(qq="999")
        event.result.chain = [semantic_quote, semantic_at, Plain("原文")]
        delivery = guard._delivery()
        await delivery.prepare(event)
        delivery.arm(event, event.result)
        delivery.typing_pause = AsyncMock(return_value=True)
        sticker = Chain([Image.fromURL("https://example.invalid/a")])
        await event.send(sticker)
        delivery.typing_pause.assert_not_awaited()
        self.assertIs(event.sent[0], sticker)
        await event.send(event.result)
        self.assertIn(semantic_quote, event.sent[1].chain)
        self.assertIn(semantic_at, event.sent[1].chain)
        delivery.typing_pause.assert_awaited_once()

    async def test_private_and_invalid_message_id_do_not_fake_headers(self):
        guard = plugin(gate_config(expression_takeover_enabled=True, reply_quote_enabled=True,
                                   reply_mention_enabled=True))
        for group, mid, expected_quote in (("", "m1", True), ("1001", "0", False)):
            event = Event(group=group, mid=mid)
            delivery = guard._delivery()
            await delivery.prepare(event)
            delivery.arm(event, event.result)
            await event.send(event.result)
            chain = event.sent[0].chain
            self.assertEqual(any(isinstance(c, Reply) for c in chain), expected_quote)
            if not group:
                self.assertFalse(any(isinstance(c, At) for c in chain))
