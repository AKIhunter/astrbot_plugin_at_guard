"""0关闭时间过滤：条数/图片对齐/字符限制及回复有效期独立保留。"""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from astrbot.api.message_components import Image, Reply
from test_gate import plugin, gate_config, GateEvent
from guard_webui import _coerce


class UnlimitedContextTests(unittest.TestCase):
    def setUp(self):
        self.guard=plugin(gate_config(gate_context_max_age_seconds=0,gate_context_messages=2))
        now=time.monotonic()
        self.guard.recent_messages["test"].extend((now-10000+i,f"消息{i}") for i in range(4))

    def test_zero_saves_and_invalid_small_positive_does_not(self):
        self.assertEqual(_coerce("gate_context_max_age_seconds",0),0)
        self.assertEqual(_coerce("gate_context_max_age_seconds",30),30)
        for value in (-1,1,29,3601,float("inf")):
            with self.assertRaises(ValueError):
                _coerce("gate_context_max_age_seconds",value)

    def test_unlimited_retains_old_messages_but_only_requested_rows(self):
        rows=self.guard._context_rows("test",2)
        self.assertEqual(len(rows),2)
        self.assertIn("消息2",rows[0][1])
        self.assertIn("消息3",rows[1][1])
        self.assertEqual(self.guard._context_rows("test",0),[])

    def test_positive_restores_filter_and_current_message_is_excluded(self):
        self.guard.config["gate_context_max_age_seconds"]=300
        self.assertEqual(self.guard._context_rows("test",2),[])
        self.guard.config["gate_context_max_age_seconds"]=0
        stamp=self.guard.recent_messages["test"][-1][0]
        self.assertTrue(all(row[0]!=stamp for row in self.guard._context_rows("test",2,stamp)))

    def test_previous_bot_line_obeys_same_toggle(self):
        self.guard.last_bot_line["test"]="旧回复"
        self.guard.last_bot_line_at["test"]=time.monotonic()-10000
        self.assertEqual(self.guard._fresh_bot_line("test"),"旧回复")
        self.guard.config["gate_context_max_age_seconds"]=300
        self.assertEqual(self.guard._fresh_bot_line("test"),"")

    def test_old_reply_image_follows_time_toggle(self):
        event=GateEvent()
        event.get_sender_id=lambda:"sender"
        event.get_messages=lambda:[Reply(id="reference",time=time.time()-10000,
                                       chain=[Image(url="https://example.invalid/current.png")])]
        self.assertEqual(len(self.guard._event_images(event)),1)
        self.guard.config["gate_context_max_age_seconds"]=300
        self.assertEqual(self.guard._event_images(event),[])

    def test_zero_context_does_not_disable_reply_expiration(self):
        event=GateEvent()
        event.set_extra("_at_guard_recorded_at",time.monotonic()-1000)
        self.assertEqual(self.guard._stale_reason(event,"test"),"message_expired")

    def test_character_cap_remains(self):
        self.guard.recent_messages["test"].clear()
        now=time.monotonic()-10000
        self.guard.recent_messages["test"].extend((now+i,"长"*3000) for i in range(30))
        rows=self.guard._context_rows("test",30)
        self.assertLessEqual(sum(len(row[1]) for row in rows),2400)
