"""原生模型/人格/语音的范围隔离、故障恢复及热重载回归。"""
import asyncio
import copy
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import unittest
import threading

from guard_native_settings import NativeSettings
from guard_native_reply import NativeReplyError
import test_native_reply as fixture


class NativeSettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture.NativeTests.setUp(self)
        # 有宿主源码时直接使用真实原子保存方法，仍只写临时fixture。
        host_root = Path(os.environ.get("ASTRBOT_NATIVE_SOURCE", "/nonexistent"))
        if (host_root / "core/config/astrbot_config.py").exists():
            from test_native_host_config import host_classes
            config_class, _ = host_classes()
            for key, before in list(self.mgr.confs.items()):
                conf = config_class(**copy.deepcopy(dict(before)))
                for attr, value in dict(config_path=before.config_path,
                        _save_state_lock=threading.Lock(), _save_commit_lock=threading.Lock(),
                        _save_revision=0, _save_committed_revision=0).items():
                    object.__setattr__(conf, attr, value)
                self.mgr.confs[key] = conf
            self.conf, self.other = self.mgr.confs["default"], self.mgr.confs["other"]
        for key, conf in self.mgr.confs.items():
            conf["agent_runner"] = dict(runner_type="local", config=dict(
                model=dict(provider_id="chat-a", fallback_provider_ids=[], request_max_retries=2),
                persona=dict(persona_id="persona-a")))
            conf["provider_settings"] = dict(streaming_response=False,
                default_image_caption_provider_id="", image_caption_prompt="描述画面")
            conf["provider_tts_settings"] = dict(enable=False, provider_id="tts-a",
                dual_output=False, use_file_service=False, trigger_probability=0.5)
            conf.update(t2i=False, t2i_word_threshold=150, t2i_strategy="remote",
                        t2i_endpoint="", t2i_active_template="base")
            conf.config_path = str(Path(self.folder.name) / (key + ".json"))
            conf.save_config()
        self.resolver = AsyncMock(return_value=("conversation-old", {}, None, False))
        self.persona_mgr = SimpleNamespace(personas_v3=[dict(name="persona-a", system_prompt="A"),
            dict(name="persona-b", system_prompt="B")], resolve_selected_persona=self.resolver)
        self.mgr.get_conf = lambda umo: self.mgr.confs["other" if umo == "other" else "default"]
        self.context = SimpleNamespace(astrbot_config_mgr=self.mgr, persona_manager=self.persona_mgr)
        self.plugin = SimpleNamespace(context=self.context, data_dir=Path(self.folder.name), logger=logging.getLogger())
        self.settings = NativeSettings(self.plugin, self.bridge)
        self.settings.install()
        self.providers = [dict(id="chat-a", role="chat", kind="provider", supports_images=False),
                          dict(id="chat-b", role="chat", kind="provider", supports_images=True),
                          dict(id="tts-a", role="tts", kind="provider"),
                          dict(id="tts-b", role="tts", kind="provider")]

    def settings_body(self, config_id="default", **patch):
        return dict(config_id=config_id, expected_revision=self.settings.read(config_id)["revision"], values=patch)

    async def test_selected_profile_persists_without_changing_persona_or_model(self):
        before = {key: copy.deepcopy(dict(conf)) for key, conf in self.mgr.confs.items()}
        await self.settings.select_profile({"config_id": "other"})
        replacement = NativeSettings(self.plugin, self.bridge)
        self.assertEqual(replacement.selected_profile(), {"config_id": "other", "saved": True})
        self.assertEqual(before, {key: dict(conf) for key, conf in self.mgr.confs.items()})
        self.assertEqual(replacement.pins, {})
        with self.assertRaises(NativeReplyError):
            await replacement.select_profile({"config_id": "missing"})
        self.assertEqual(replacement.selected_profile()["config_id"], "other")
        del self.mgr.confs["other"]
        with self.assertRaisesRegex(NativeReplyError, "不存在"):
            replacement.selected_profile()

    async def test_registered_router_upstream_can_be_native_main_and_fallback(self):
        self.providers.append(dict(id="router:upstream_2", role="chat", kind="router_upstream"))
        result = await self.settings.save(self.request, self.settings_body(
            provider_id="router:upstream_2", fallback_provider_ids=["router:upstream_2"]), self.providers)
        self.assertEqual(result["values"]["provider_id"], "router:upstream_2")
        self.assertEqual(result["values"]["fallback_provider_ids"], ["router:upstream_2"])

    async def resolve(self, umo="default"):
        return await self.persona_mgr.resolve_selected_persona(
            umo=umo, conversation_persona_id="conversation-old", platform_name="webchat")

    async def test_model_saved_only_to_target_and_runtime_prefix_not_persisted(self):
        self.conf["wake_prefix"] = ["temporary"]
        result = await self.settings.save(self.request,
            self.settings_body("other", provider_id="chat-b", fallback_provider_ids=["chat-a"]), self.providers)
        self.assertEqual(result["values"]["provider_id"], "chat-b")
        self.assertEqual(self.conf["agent_runner"]["config"]["model"]["provider_id"], "chat-a")
        self.assertNotIn("wake_prefix", json.loads(Path(self.other.config_path).read_text(encoding="utf-8-sig")))
        self.assertEqual(self.other["unrelated"], {"preserve": True})

    async def test_voice_settings_roundtrip_only_target_and_preserve_tts_siblings(self):
        result = await self.settings.save(self.request, self.settings_body(
            "other", tts_enabled=True, tts_provider_id="tts-b"), self.providers)
        self.assertTrue(result["values"]["tts_enabled"])
        self.assertEqual(result["values"]["tts_provider_id"], "tts-b")
        self.assertFalse(self.conf["provider_tts_settings"]["enable"])
        self.assertEqual(self.conf["provider_tts_settings"]["provider_id"], "tts-a")
        self.assertEqual(self.other["provider_tts_settings"]["trigger_probability"], 0.5)
        self.assertFalse(self.other["provider_tts_settings"]["dual_output"])
        disk = json.loads(Path(self.other.config_path).read_text(encoding="utf-8-sig"))
        self.assertEqual(disk["provider_tts_settings"], self.other["provider_tts_settings"])

    async def test_native_voice_change_is_visible_and_old_revision_conflicts(self):
        stale = self.settings_body(tts_enabled=True, tts_provider_id="tts-b")
        self.conf["provider_tts_settings"].update(enable=True, provider_id="tts-b")
        self.conf.save_config()
        self.assertTrue(self.settings.read()["values"]["tts_enabled"])
        self.assertEqual(self.settings.read()["values"]["tts_provider_id"], "tts-b")
        with self.assertRaises(NativeReplyError) as error:
            await self.settings.save(self.request, stale, self.providers)
        self.assertEqual(error.exception.status, 409)

    async def test_fixed_persona_overrides_old_conversation_and_survives_reload(self):
        await self.settings.save(self.request,self.settings_body(persona_id="persona-b"),self.providers)
        self.assertEqual((await self.resolve())[:1], ("persona-b",))
        self.assertEqual((await self.resolve("other"))[:1], ("conversation-old",))
        self.conf["agent_runner"]["config"]["persona"]["persona_id"]="persona-a"
        self.assertEqual((await self.resolve())[:1], ("persona-b",))
        self.settings.close()
        replacement=NativeSettings(self.plugin,self.bridge); replacement.install()
        self.assertEqual((await self.resolve())[:1], ("persona-b",))
        replacement.close()
        self.assertIs(self.persona_mgr.resolve_selected_persona,self.resolver)

    async def test_manual_change_and_none_are_explicit(self):
        await self.settings.save(self.request,self.settings_body(persona_id="persona-a"),self.providers)
        await self.settings.save(self.request,self.settings_body(persona_id="[%None]"),self.providers)
        self.assertEqual(await self.resolve(), ("[%None]",None,"[%None]",False))
        await self.settings.save(self.request,self.settings_body(persona_id="persona-b"),self.providers)
        self.assertEqual((await self.resolve())[0],"persona-b")

    async def test_missing_fixed_persona_never_falls_back(self):
        await self.settings.save(self.request,self.settings_body(persona_id="persona-b"),self.providers)
        self.persona_mgr.personas_v3 = [dict(name="persona-a")]
        with self.assertRaisesRegex(NativeReplyError,"不存在"):
            await self.resolve()

    async def test_refresh_failure_restores_disk_memory_and_pin(self):
        self.reload.side_effect=[OSError("refresh failed"),None]
        with self.assertRaisesRegex(NativeReplyError,"已恢复"):
            await self.settings.save(self.request,self.settings_body(persona_id="persona-b",provider_id="chat-b"),self.providers)
        self.assertEqual(self.settings.read()["values"]["provider_id"],"chat-a")
        self.assertIsNone(self.settings.fixed_persona_id("default"))
        self.assertEqual(json.loads(Path(self.conf.config_path).read_text(encoding="utf-8-sig"))["agent_runner"]["config"]["persona"]["persona_id"],"persona-a")

    async def test_pin_write_failure_never_activates_draft(self):
        self.settings._write_pins=lambda _: (_ for _ in ()).throw(OSError("pin disk"))
        with self.assertRaisesRegex(NativeReplyError,"已恢复"):
            await self.settings.save(self.request,self.settings_body(persona_id="persona-b"),self.providers)
        self.assertIsNone(self.settings.fixed_persona_id("default"))
        self.assertEqual((await self.resolve())[0],"conversation-old")

    async def test_corrupted_pin_data_never_silently_unfixes_persona(self):
        self.settings.close()
        self.settings.pin_path.write_text("{broken")
        replacement = NativeSettings(self.plugin,self.bridge); replacement.install()
        with self.assertRaisesRegex(NativeReplyError,"文件损坏"):
            await self.resolve()
        with self.assertRaises(NativeReplyError):
            await replacement.save(self.request,dict(config_id="default",
                expected_revision=replacement.read()["revision"],values={"persona_id":"persona-b"}),self.providers)
        replacement.close()

    async def test_conflict_and_unknown_provider_fail_without_changes(self):
        stale=self.settings_body(provider_id="chat-b")
        self.conf["agent_runner"]["config"]["model"]["provider_id"]=""
        with self.assertRaises(NativeReplyError):
            await self.settings.save(self.request,stale,self.providers)
        for patch in [dict(provider_id="chat-a::virtual"),dict(persona_id="unknown"),
                      dict(tts_enabled=1),dict(tts_provider_id="chat-a"),
                      dict(tts_provider_id="missing"),
                      dict(request_max_retries=True),dict(request_max_retries=0),
                      dict(IMAGE_GEN_API_KEY="secret")]:
            with self.assertRaises(NativeReplyError):
                await self.settings.save(self.request,self.settings_body(**patch),self.providers)
        self.reload.assert_not_awaited()

    async def test_disabled_external_runner_cannot_change_hidden_model_fields(self):
        self.conf["agent_runner"]["runner_type"]="external"
        values = self.settings.read()["values"]
        self.assertNotIn("provider_id", values)
        self.assertIn("tts_enabled", values)
        self.assertIn("tts_provider_id", values)
        with self.assertRaises(NativeReplyError):
            await self.settings.save(self.request,self.settings_body(provider_id="chat-b"),self.providers)
        saved = await self.settings.save(
            self.request, self.settings_body(tts_enabled=True, tts_provider_id="tts-b"), self.providers)
        self.assertTrue(saved["values"]["tts_enabled"])

    async def test_shared_save_lock_and_stale_concurrent_writer(self):
        self.assertIs(self.settings.lock,self.bridge.lock)
        body=self.settings_body(provider_id="chat-b")
        result=await asyncio.gather(
            self.settings.save(self.request,body,self.providers),
            self.settings.save(self.request,body,self.providers),return_exceptions=True)
        self.assertEqual(sum(isinstance(row,NativeReplyError) for row in result),1)

    def test_read_excludes_prompts_credentials_and_unrelated_config(self):
        self.conf["provider_settings"]["api_key"]="secret-marker"
        payload=json.dumps(self.settings.read())
        self.assertNotIn("secret-marker",payload)
        self.assertNotIn("system_prompt",payload)
        self.assertNotIn("unrelated",payload)

    async def test_voice_refresh_failure_restores_enable_model_and_disk(self):
        self.reload.side_effect = [OSError("refresh failed"), None]
        with self.assertRaisesRegex(NativeReplyError, "已恢复"):
            await self.settings.save(self.request, self.settings_body(
                tts_enabled=True, tts_provider_id="tts-b"), self.providers)
        self.assertFalse(self.settings.read()["values"]["tts_enabled"])
        self.assertEqual(self.settings.read()["values"]["tts_provider_id"], "tts-a")
        disk = json.loads(Path(self.conf.config_path).read_text(encoding="utf-8-sig"))
        self.assertFalse(disk["provider_tts_settings"]["enable"])
        self.assertEqual(disk["provider_tts_settings"]["provider_id"], "tts-a")
        self.assertEqual(disk["provider_tts_settings"]["trigger_probability"], 0.5)

    async def test_missing_voice_paths_are_hidden_and_cannot_be_created(self):
        del self.conf["provider_tts_settings"]
        self.conf.save_config()
        current = self.settings.read()
        self.assertNotIn("tts_enabled", current["values"])
        self.assertNotIn("tts_provider_id", current["values"])
        with self.assertRaises(NativeReplyError):
            await self.settings.save(self.request, {
                "config_id": "default", "expected_revision": current["revision"],
                "values": {"tts_enabled": True}}, self.providers)

    async def test_removed_native_vision_fields_are_rejected_and_preserved_on_disk(self):
        before=Path(self.conf.config_path).read_bytes()
        self.assertNotIn("image_caption_provider_id",self.settings.read()["values"])
        self.assertTrue(all(f["group"] != "图片理解" for f in self.settings.read()["fields"]))
        with self.assertRaises(NativeReplyError):
            await self.settings.save(self.request,self.settings_body(image_caption_provider_id="chat-b"),self.providers)
        self.assertEqual(Path(self.conf.config_path).read_bytes(),before)
