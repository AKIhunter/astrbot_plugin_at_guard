"""原生模型/人格的范围隔离、故障恢复及热重载回归。"""
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
from guard_image_settings import ImageSettings, FIELDS as IMAGE_FIELDS
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
                          dict(id="chat-b", role="chat", kind="provider", supports_images=True)]

    def settings_body(self, config_id="default", **patch):
        return dict(config_id=config_id, expected_revision=self.settings.read(config_id)["revision"], values=patch)

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
                      dict(request_max_retries=True),dict(request_max_retries=0),
                      dict(IMAGE_GEN_API_KEY="secret")]:
            with self.assertRaises(NativeReplyError):
                await self.settings.save(self.request,self.settings_body(**patch),self.providers)
        self.reload.assert_not_awaited()

    async def test_disabled_external_runner_cannot_change_hidden_model_fields(self):
        self.conf["agent_runner"]["runner_type"]="external"
        self.assertNotIn("provider_id",self.settings.read()["values"])
        with self.assertRaises(NativeReplyError):
            await self.settings.save(self.request,self.settings_body(provider_id="chat-b"),self.providers)

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

    async def test_image_settings_public_patch_retains_key_and_other_fields(self):
        self.conf.update({key: 128 if key.endswith(("WIDTH","HEIGHT")) else 2
                          if key.endswith(("STEPS","SCALE")) else "" for key in IMAGE_FIELDS})
        self.conf["IMAGE_GEN_BACKEND"]="openai"
        self.conf["IMAGE_GEN_API_KEY"]="secret-marker"
        self.conf.save_config()
        self.context.get_registered_star=lambda _: SimpleNamespace(activated=True,star_cls=SimpleNamespace(config=self.conf))
        bridge=ImageSettings(self.context,self.bridge.lock)
        self.assertNotIn("secret-marker",json.dumps(bridge.read()))
        before=bridge.read()
        saved=await bridge.save(dict(expected_revision=before["revision"],values={"IMAGE_GEN_MODEL":"image-b"}))
        self.assertEqual(saved["values"]["IMAGE_GEN_MODEL"],"image-b")
        self.assertEqual(json.loads(Path(self.conf.config_path).read_text(encoding="utf-8-sig"))["IMAGE_GEN_API_KEY"],"secret-marker")
        with self.assertRaises(NativeReplyError):
            await bridge.save(dict(expected_revision=saved["revision"],values={"IMAGE_GEN_API_KEY":"replacement"}))
