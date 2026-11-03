"""从绫准备耗时与发送保护回归；不用真实模型或群消息。"""
import asyncio
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from astrbot.api.message_components import Plain
from guard_webui import EDITABLE, _coerce, _congling_compat, _load_schema_subset, _state
from test_gate import GateEvent, gate_config, plugin


class PreparationTest(unittest.TestCase):
    def setUp(self):
        self.guard = plugin(gate_config(gate_reply_max_age_seconds=30))
        self.event = GateEvent("这梗编得挺完整")
        self.event.set_extra("_at_guard_recorded_at", 100)
        self.event.set_extra("_at_guard_judged_revision", 0)
        self.event.set_extra("_at_guard_proactive", True)
        self.event.set_extra("_at_guard_group_key", "1001")

    def reason(self, age=60):
        with patch("main.time.monotonic", return_value=100 + age):
            return self.guard._stale_reason(self.event, "1001")

    def test_preparation_allows_reply_but_later_generation_expires(self):
        self.event.set_extra("congling:wake_prepare_seconds", 45)
        self.assertEqual(self.reason(60), "")
        self.assertEqual(self.reason(80), "message_expired")

    def test_exact_deadline_and_submillisecond_overrun(self):
        self.event.set_extra("congling:wake_prepare_seconds", 45)
        self.assertEqual(self.reason(75), "")
        self.assertEqual(self.reason(75.00001), "message_expired")

    def test_disabling_compat_uses_original_age(self):
        self.guard.config["gate_congling_prepare_compat"] = False
        self.event.set_extra("congling:wake_prepare_seconds", 45)
        self.assertEqual(self.reason(), "message_expired")

    def test_invalid_preparation_never_extends_deadline(self):
        for value in (None, "", 0, -1, True, False, "bad", [], {},
                      float("nan"), float("inf"), -float("inf"), 61, 10 ** 1000):
            with self.subTest(value=repr(value)[:30]):
                self.event.set_extra("congling:wake_prepare_seconds", value)
                self.assertEqual(self.reason(), "message_expired")

    def test_missing_preparation_preserves_original_rule(self):
        self.assertEqual(self.reason(), "message_expired")

    def test_numeric_string_is_accepted(self):
        self.event.set_extra("congling:wake_prepare_seconds", "45.0")
        self.assertEqual(self.reason(), "")

    def test_preparation_is_isolated_per_event(self):
        self.event.set_extra("congling:wake_prepare_seconds", 45)
        self.assertEqual(self.reason(), "")
        self.event.extra.pop("congling:wake_prepare_seconds")
        self.assertEqual(self.reason(), "message_expired")

    def test_missing_recorded_timestamp_does_not_create_timeout(self):
        self.event.extra.pop("_at_guard_recorded_at")
        self.event.set_extra("congling:wake_prepare_seconds", 45)
        self.assertEqual(self.reason(), "")
        self.guard.context_revision["1001"] = 1
        self.assertEqual(self.reason(), "context_changed")

    def test_new_message_still_revokes_prepared_reply(self):
        self.event.set_extra("congling:wake_prepare_seconds", 45)
        self.guard.context_revision["1001"] = 1
        self.assertEqual(self.reason(), "context_changed")

    def test_generation_hook_clears_stale_response_after_preparation(self):
        self.event.set_extra("congling:wake_prepare_seconds", 45)
        for age, revision, expected in ((80, 0, "message_expired"), (60, 1, "context_changed")):
            with self.subTest(expected=expected):
                self.event.extra.pop("_at_guard_delivery_blocked", None)
                self.guard.context_revision["1001"] = revision
                response = SimpleNamespace(completion_text="旧回复", result_chain=None,
                                           tools_call_args=[], tools_call_name=[])
                with patch("main.time.monotonic", return_value=100 + age):
                    asyncio.run(self.guard.suppress_no_reply_and_track(self.event, response))
                self.assertEqual(response.completion_text, "")
                self.assertEqual(self.event.get_extra("_at_guard_delivery_blocked"), expected)

    def test_final_send_recheck_keeps_prepared_reply_and_cancels_new_context(self):
        self.event.set_extra("congling:wake_prepare_seconds", 45)
        result = SimpleNamespace(chain=[Plain("当前答复")])
        self.event.get_result = lambda: result
        with patch("main.time.monotonic", return_value=160):
            asyncio.run(self.guard.recheck_before_send(self.event))
            self.assertTrue(result.chain)
            self.guard.context_revision["1001"] = 1
            asyncio.run(self.guard.recheck_before_send(self.event))
        self.assertEqual(result.chain, [])
        self.assertTrue(self.event.stopped)


class ConsoleCompatTest(unittest.TestCase):
    def setUp(self):
        self.guard = plugin(gate_config())

    def status(self, priority=9000, active=True, config=True, alias=False):
        module = types.ModuleType("_at_guard_test_congling")
        module.PRIO_PERSONA = priority
        instance = type("Congling", (), {"__module__": module.__name__})()
        metadata = SimpleNamespace(activated=active, version="1.1.0", star_cls=instance)
        self.guard.context = SimpleNamespace(get_registered_star=lambda name:
            metadata if name == ("congling" if alias else "astrbot_plugin_congling") else None)
        self.guard.config["gate_congling_prepare_compat"] = config
        with patch.dict(sys.modules, {module.__name__: module}):
            return _congling_compat(self.guard)

    def test_disabled_or_absent_plugin_is_optional(self):
        self.guard.context = SimpleNamespace(get_registered_star=lambda _: None)
        self.assertEqual(_congling_compat(self.guard), {
            "detected": False, "active": False, "order": "unknown", "prepare_accounting": True})
        self.assertFalse(self.status(active=False)["active"])

    def test_priority_is_checked_from_loaded_module(self):
        self.assertEqual(self.status()["order"], "after_guard")
        self.assertEqual(self.status(priority=12000)["order"], "before_or_equal_guard")
        self.assertEqual(self.status(priority=10000)["order"], "before_or_equal_guard")

    def test_unknown_priority_does_not_claim_compatibility(self):
        for priority in (None, "9000", True):
            with self.subTest(priority=priority):
                self.assertEqual(self.status(priority=priority)["order"], "unknown")

    def test_alias_lookup_and_disabled_accounting(self):
        self.assertTrue(self.status(alias=True)["detected"])
        self.assertFalse(self.status(config=False)["prepare_accounting"])

    def test_registry_failure_does_not_break_console(self):
        def unavailable(_):
            raise RuntimeError("未提供注册表")
        self.guard.context = SimpleNamespace(get_registered_star=unavailable)
        self.assertFalse(_congling_compat(self.guard)["detected"])

    def test_schema_and_editable_fields_include_switch(self):
        key = "gate_congling_prepare_compat"
        self.assertIn(key, EDITABLE)
        self.assertIs(_load_schema_subset()[key]["default"], True)
        self.assertIs(_coerce(key, "false"), False)
        self.assertIs(_coerce(key, False), False)
        self.assertIs(_coerce(key, "true"), True)

    def test_console_state_exposes_metadata_and_saved_switch(self):
        self.guard.context = SimpleNamespace(get_registered_star=lambda _: None,
                                             get_all_providers=lambda: [])
        self.guard.config["gate_congling_prepare_compat"] = False
        state = asyncio.run(_state(self.guard))
        self.assertIs(state["config"]["gate_congling_prepare_compat"], False)
        self.assertIs(state["congling_compat"]["prepare_accounting"], False)
        self.assertFalse(state["congling_compat"]["detected"])


if __name__ == "__main__":
    unittest.main()
