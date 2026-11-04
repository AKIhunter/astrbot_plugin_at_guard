"""执行从绫实际交接方法，隔离框架初始化、平台发送和数据库写入。"""
import ast
import logging
import os
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from astrbot.api.message_components import Plain


ROOT = Path(os.environ.get(
    "CONGLING_HANDOFF_SOURCE",
    str(Path(__file__).parent.parent / "astrbot_plugin_congling"),
))


class PerceivedMessageStub(SimpleNamespace):
    def __init__(self, **kwargs):
        super().__init__(meta={}, **kwargs)


def methods(relative, selected):
    """只去掉框架装饰器，执行源码中的方法体，避免启动插件后台任务。"""
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8-sig"))
    nodes = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in selected:
            node.decorator_list = []
            nodes.append(node)
    assert {node.name for node in nodes} == set(selected)
    namespace = {
        "time": time, "Any": object, "AstrMessageEvent": object,
        "logger": logging.getLogger("handoff-test"),
        "Block": SimpleNamespace, "PerceivedMessage": PerceivedMessageStub,
        "component_to_block": lambda comp: SimpleNamespace(kind="text", text=comp.text),
        "blocks_to_text": lambda blocks: "\n".join(block.text for block in blocks),
        "add_temp_part": lambda req, text, marker: req.parts.append((text, marker)),
        "MARKER_PREFIX": "<!--congling:perceived:",
        "SELF_ACTION_RULE": "原生身份", "DREAM_RECALL_RULE": "原生隐私",
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / relative), "exec"), namespace)
    return SimpleNamespace(**{name: namespace[name] for name in selected})


class Event:
    def __init__(self, owner=True, group="42"):
        self.extra = {"_at_guard_reply_owner": "at_guard"} if owner else {}
        self.unified_msg_origin = "test:" + (group or "private")
        self.group = group
        self.message_obj = SimpleNamespace(message_id="123", message=[Plain("原始文字")])
        self.result = SimpleNamespace(chain=[Plain("保留。")], is_llm_result=lambda: True)
    def get_extra(self, key, default=None):
        return self.extra.get(key, default)
    def get_group_id(self):
        return self.group
    def get_sender_id(self):
        return "24"
    def get_sender_name(self):
        return "用户"
    def get_self_id(self):
        return "bot"
    def get_platform_name(self):
        return "test"
    def get_result(self):
        return self.result


@unittest.skipUnless((ROOT / "main.py").is_file(), "从绫源码未安装")
class HandoffTests(unittest.IsolatedAsyncioTestCase):
    def service(self):
        actual = methods("perception/service.py", ("perceive", "inject", "_safe"))
        service = SimpleNamespace(
            plugin=SimpleNamespace(), _safe=actual._safe,
            config=SimpleNamespace(), _enrich_with_timeout=AsyncMock(),
            archive=AsyncMock(), _prompt_sections=Mock(return_value=["自身补充"]),
            _recent_context=AsyncMock(return_value=""),
        )
        service.perceive = lambda event: actual.perceive(service, event)
        return actual, service

    async def test_guard_evidence_does_not_override_native_perception(self):
        actual, service = self.service()
        events = [Event(group="42"), Event(group="")]
        for index, event in enumerate(events):
            event.extra["_at_guard_understanding"] = SimpleNamespace(
                text=f"原作者: 全文{index}", prompt="头尾摘录", quoted="引用全文", nodes=3)
        results = [await actual.perceive(service, event) for event in events]
        self.assertEqual([row.text for row in results], ["原始文字", "原始文字"])
        self.assertEqual([row.session_kind for row in results], ["group", "private"])
        self.assertFalse(results[0].is_forward_expanded)
        self.assertEqual(results[0].quoted, "")
        self.assertEqual(results[0].meta["self_id"], "bot")
        self.assertIs(service.plugin._current_event, events[-1])
        self.assertEqual(service._enrich_with_timeout.await_count, 2)

    async def test_guard_attached_flag_does_not_skip_native_context_or_media(self):
        actual, service = self.service()
        event = Event()
        event.extra.update(
            _at_guard_understanding=SimpleNamespace(text="全文", quoted="", nodes=1),
            _at_guard_understanding_attached=True)
        request = SimpleNamespace(parts=[])
        await actual.inject(service, event, request)
        service.archive.assert_awaited_once()
        service._recent_context.assert_awaited_once()
        service._prompt_sections.assert_called_once()
        service._enrich_with_timeout.assert_awaited_once()
        self.assertEqual(len(request.parts), 1)
        self.assertEqual(event._congling_msg.text, "原始文字")

    async def test_switch_off_returns_to_congling_perception(self):
        actual, service = self.service()
        event = Event(owner=False)
        event.extra["_at_guard_understanding"] = SimpleNamespace(
            text="接管证据不能复用", quoted="", nodes=0)
        perceived = await actual.perceive(service, event)
        self.assertEqual(perceived.text, "原始文字")
        service._enrich_with_timeout.assert_awaited_once()
        self.assertIs(service.plugin._current_event, event)

    def expression(self):
        actual = methods("main.py", ("express_hook",))
        plugin = SimpleNamespace(
            enabled_for=lambda event: True, diag=None,
            store=SimpleNamespace(kv_set=AsyncMock()),
            guard=SimpleNamespace(clean_chain=Mock()),
            composer=SimpleNamespace(decorate=AsyncMock()),
            delayer=SimpleNamespace(mode=Mock(return_value="standalone"), patch=Mock()),
            persona=SimpleNamespace(resolve=AsyncMock(return_value={"token": "new"})),
            night=SimpleNamespace(wake_idle=True, take_sleepy_line=Mock(return_value="困意")),
        )
        return actual, plugin

    async def test_owner_does_not_clean_split_or_apply_old_delay(self):
        actual, plugin = self.expression()
        event = Event()
        await actual.express_hook(plugin, event)
        self.assertEqual([part.text for part in event.result.chain], ["困意", "保留。"])
        plugin.guard.clean_chain.assert_not_called()
        plugin.composer.decorate.assert_not_awaited()
        plugin.delayer.patch.assert_not_called()

    async def test_switch_off_returns_to_congling_expression(self):
        actual, plugin = self.expression()
        event = Event(owner=False)
        await actual.express_hook(plugin, event)
        plugin.composer.decorate.assert_awaited_once_with(event, event.result)
        plugin.delayer.patch.assert_called_once()

    async def test_wake_or_persona_guard_still_cancels_owned_expression(self):
        for flag, token in ((True, None), (False, "old")):
            actual, plugin = self.expression()
            event = Event()
            event.extra.update({"congling:wake_blocked": flag, "congling:persona_token": token})
            await actual.express_hook(plugin, event)
            self.assertEqual(event.result.chain, [])
            plugin.night.take_sleepy_line.assert_not_called()
            plugin.composer.decorate.assert_not_awaited()

    async def test_tool_result_does_not_get_congling_cleanup_when_owned(self):
        actual, plugin = self.expression()
        event = Event()
        event.result.is_llm_result = lambda: False
        await actual.express_hook(plugin, event)
        plugin.guard.clean_chain.assert_not_called()
        self.assertEqual([part.text for part in event.result.chain], ["保留。"])

    async def test_only_native_persona_switch_controls_identity_and_state(self):
        actual = methods("humanize/persona.py", ("inject",))
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                event = Event()
                persona = SimpleNamespace(
                    config=SimpleNamespace(bool=lambda *a: enabled),
                    resolve=AsyncMock(return_value={"token": "identity"}),
                    _replace_framework_persona=AsyncMock(),
                    plugin=SimpleNamespace(probe=SimpleNamespace(should_yield=lambda _: False)),
                    _relation_line=AsyncMock(return_value="关系"),
                    _state_line=AsyncMock(return_value="状态"),
                    _scene_line=lambda _: "场景",
                )
                request = SimpleNamespace(conversation=None, parts=[])
                await actual.inject(persona, event, request)
                persona._replace_framework_persona.assert_awaited_once()
                self.assertEqual(bool(request.parts), enabled)


if __name__ == "__main__":
    unittest.main()
