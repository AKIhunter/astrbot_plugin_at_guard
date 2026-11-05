"""隔离执行实际宿主保存、页面查询与落盘方法；不会加载真实AstrBot服务。"""
import ast
import asyncio
import copy
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from guard_native_settings import NativeSettings
from guard_native_reply import NativeReply, NativeReplyError, values
from test_native_reply import NATIVE

SOURCE = Path(os.environ.get("ASTRBOT_NATIVE_SOURCE", "/nonexistent-astrbot-source"))


def host_classes():
    namespace = dict(asyncio=asyncio, copy=copy, json=json, logging=logging, os=os,
                     tempfile=tempfile, threading=threading, traceback=traceback,
                     logger=logging.getLogger("native-config-test"))
    inputs = (
        ("core/config/astrbot_config.py", "AstrBotConfig", {
            "save_config", "_prepare_config_snapshot", "_write_config_snapshot"}),
        ("dashboard/services/config_service.py", "ConfigProfileService", {
            "update_profile", "get_profile"}),
    )
    for relative, name, methods in inputs:
        tree = ast.parse((SOURCE / relative).read_text())
        selected = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
        selected.decorator_list = []
        selected.body = [n for n in selected.body
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in methods]
        nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                 selected]
        if name == "ConfigProfileService":
            nodes.extend(n for n in tree.body if isinstance(n, ast.FunctionDef)
                         and n.name == "save_config")
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                     str(SOURCE / relative), "exec"), namespace)
    namespace.update(
        CONFIG_METADATA_2={}, CONFIG_METADATA_3={},
        ConfigMetadataI18n=SimpleNamespace(convert_to_i18n_keys=lambda value: value),
        normalize_agent_runner=lambda value: value or {},
        validate_config=lambda data, *args, **kw: ([], data),
        _log_computer_config_changes=lambda *args: None,
        _protected_2fa_config_changed=lambda *args: False,
        is_totp_enabled=lambda *args: False,
        _get_nested_value=lambda data, keys: True,
        set_pending_totp_secret=lambda *args: None,
        computer_client=SimpleNamespace(local_booter=None),
        _validate_neo_connectivity=AsyncMock(return_value=None),
    )
    return namespace["AstrBotConfig"], namespace["ConfigProfileService"]


@unittest.skipUnless((SOURCE / "core/config/astrbot_config.py").is_file(), "需提供实际宿主配置源码")
class NativeHostConfigTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        config_class, service_class = host_classes()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "native.json"
        self.config = config_class(
            platform_settings={"segmented_reply": copy.deepcopy(NATIVE)},
            unrelated={"preserve": True},
            agent_runner={"runner_type": "local", "config": {
                "model": {"provider_id": "", "fallback_provider_ids": [], "request_max_retries": 2},
                "persona": {"persona_id": "[%None]"}}},
            provider_settings={"streaming_response": False},
            provider_tts_settings={"enable": False, "provider_id": "tts-a",
                                   "dual_output": False, "use_file_service": False,
                                   "trigger_probability": 0.5},
            t2i=False, t2i_word_threshold=150, t2i_strategy="remote",
            t2i_endpoint="", t2i_active_template="base")
        for key, value in dict(config_path=str(self.path), _save_state_lock=threading.Lock(),
                               _save_commit_lock=threading.Lock(), _save_revision=0,
                               _save_committed_revision=0).items():
            object.__setattr__(self.config, key, value)
        self.config.save_config()
        self.reload = AsyncMock()
        self.manager = SimpleNamespace(confs={"default": self.config},
                                       get_conf_list=lambda: [{"id": "default", "name": "默认"}])
        self.service = service_class()
        self.service.acm, self.service.runtime, self.service.db = self.manager, {}, None
        self.service.core_lifecycle = SimpleNamespace(reload_pipeline_scheduler=self.reload)
        self.bridge = NativeReply(SimpleNamespace(astrbot_config_mgr=self.manager))
        self.request = SimpleNamespace(_request=SimpleNamespace(app=SimpleNamespace(
            state=SimpleNamespace(services=SimpleNamespace(config_profiles=self.service)))))

    def body(self, **patch):
        return {"config_id": "default", "expected_revision": self.bridge.read()["revision"],
                "values": patch}

    async def test_both_pages_share_actual_host_save_disk_and_scheduler(self):
        await self.bridge.save(self.request, self.body(enable=True, interval="0,0"))
        native = self.service.get_profile("default")["config"]
        self.assertTrue(native["platform_settings"]["segmented_reply"]["enable"])
        self.assertEqual(self.reload.await_args.args, ("default",))
        next_config = copy.deepcopy(dict(native))
        next_config["platform_settings"]["segmented_reply"].update(enable=False, split_mode="words")
        await self.service.update_profile("default", next_config)
        self.assertEqual(self.bridge.read()["values"], values(next_config))
        self.assertEqual(values(json.loads(self.path.read_text(encoding="utf-8-sig"))),
                         self.bridge.read()["values"])
        self.assertEqual(self.config["unrelated"], {"preserve": True})
        self.assertEqual(self.reload.await_count, 2)

    async def test_voice_bridge_and_native_page_share_actual_host_fields(self):
        plugin = SimpleNamespace(
            context=SimpleNamespace(astrbot_config_mgr=self.manager, persona_manager=None),
            data_dir=Path(self.folder.name), logger=logging.getLogger("native-voice-test"))
        settings = NativeSettings(plugin, self.bridge)
        providers = [
            {"id": "chat-a", "role": "chat", "kind": "provider"},
            {"id": "tts-a", "role": "tts", "kind": "provider"},
            {"id": "tts-b", "role": "tts", "kind": "provider"},
        ]
        current = settings.read()
        saved = await settings.save(self.request, {
            "config_id": "default", "expected_revision": current["revision"],
            "values": {"tts_enabled": True, "tts_provider_id": "tts-b"}}, providers)
        self.assertTrue(saved["values"]["tts_enabled"])
        self.assertEqual(saved["values"]["tts_provider_id"], "tts-b")
        disk = json.loads(self.path.read_text(encoding="utf-8-sig"))
        self.assertTrue(disk["provider_tts_settings"]["enable"])
        self.assertEqual(disk["provider_tts_settings"]["provider_id"], "tts-b")
        self.assertEqual(disk["provider_tts_settings"]["trigger_probability"], 0.5)

        stale = {"config_id": "default", "expected_revision": saved["revision"],
                 "values": {"tts_enabled": True, "tts_provider_id": "tts-b"}}
        native = copy.deepcopy(dict(self.service.get_profile("default")["config"]))
        native["provider_tts_settings"].update(enable=False, provider_id="tts-a")
        await self.service.update_profile("default", native)
        self.assertFalse(settings.read()["values"]["tts_enabled"])
        self.assertEqual(settings.read()["values"]["tts_provider_id"], "tts-a")
        with self.assertRaises(NativeReplyError) as error:
            await settings.save(self.request, stale, providers)
        self.assertEqual(error.exception.status, 409)

    async def test_second_period_uses_actual_host_save_and_zero_interval(self):
        await self.bridge.save(self.request, self.body(
            enable=True, second_period=True, interval_method="random", interval="0,0"))
        disk = json.loads(self.path.read_text(encoding="utf-8-sig"))
        self.assertEqual(values(disk), self.bridge.read()["values"])
        self.assertTrue(values(disk)["second_period"])
        self.assertNotIn("second_period", disk["platform_settings"]["segmented_reply"])
        self.reload.assert_awaited_once_with("default")

    async def test_native_field_save_preserves_transient_prefix_and_dynamic_provider(self):
        self.config["provider"] = [{"id": "original"}]
        self.config["wake_prefix"] = []
        self.config.save_config()
        self.config["provider"].append({"id": "runtime-only-model"})
        self.config["wake_prefix"] = ["runtime-only-name"]
        self.service.update_profile = AsyncMock(side_effect=AssertionError("不允许整份保存"))
        await self.bridge.save(self.request, self.body(enable=True, second_period=True))
        disk = json.loads(self.path.read_text(encoding="utf-8-sig"))
        self.assertEqual(disk["provider"], [{"id": "original"}])
        self.assertEqual(disk["wake_prefix"], [])
        self.assertEqual(len(self.config["provider"]), 2)
        self.assertEqual(self.config["wake_prefix"], ["runtime-only-name"])
        self.assertEqual(values(disk), self.bridge.read()["values"])
        self.service.update_profile.assert_not_awaited()

    async def test_actual_host_scheduler_failure_recovers_disk_and_runtime(self):
        self.reload.side_effect = [RuntimeError("injected scheduler failure"), None]
        with self.assertRaisesRegex(NativeReplyError, "已恢复"):
            await self.bridge.save(self.request, self.body(enable=True))
        self.assertFalse(self.bridge.read()["values"]["enable"])
        self.assertFalse(json.loads(self.path.read_text(encoding="utf-8-sig"))[
            "platform_settings"]["segmented_reply"]["enable"])
        self.assertEqual(self.reload.await_count, 2)
