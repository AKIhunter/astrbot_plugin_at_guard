"""检索证据复用、过期及会话/人格/权限隔离，不运行真实工具。"""
import unittest
from types import SimpleNamespace

from guard_evidence import EvidenceMemory


class Event:
    def __init__(self, origin="qq:private:1", sender="1", persona="ling"):
        self.unified_msg_origin = origin
        self.sender = sender
        self.extra = {"congling:persona_token": persona}
        self.message_str = "Python字典怎么排序"

    def get_sender_id(self):
        return self.sender

    def get_extra(self, name, fallback=None):
        return self.extra.get(name, fallback)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.now = 10
        self.config = {"provider_settings": {"default_personality": "ling"}}
        self.cache = EvidenceMemory(SimpleNamespace(context=SimpleNamespace(
            get_config=lambda _: self.config)), clock=lambda: self.now)
        self.tool = SimpleNamespace(name="web_search")
        self.event = Event()
        self.req = SimpleNamespace(extra_user_content_parts=[],
                                   func_tool=SimpleNamespace(tools=[self.tool]))

    def remember(self, result="来源：https://docs.python.org ；字典保持插入顺序"):
        self.cache.remember(self.event, self.tool, {"query": "Python字典"}, result)

    def test_same_topic_reuses_temp_evidence_without_duplicate_prompt(self):
        self.remember()
        self.cache.attach(self.event, self.req)
        self.cache.attach(self.event, self.req)
        self.assertEqual(len(self.req.extra_user_content_parts), 1)
        self.assertTrue(self.req.extra_user_content_parts[0].no_save)
        self.assertIn("docs.python.org", self.req.extra_user_content_parts[0].text)

    def test_expiry_removes_evidence_and_existing_prompt(self):
        self.remember()
        self.cache.attach(self.event, self.req)
        self.now = 310
        self.cache.attach(self.event, self.req)
        self.assertEqual(self.req.extra_user_content_parts, [])
        self.assertEqual(len(self.cache.rows), 0)

    def test_scope_persona_sender_provider_tool_and_topic_must_match(self):
        self.remember()
        for event in (Event(origin="qq:group:1"), Event(sender="2"), Event(persona="other")):
            self.cache.attach(event, self.req)
            self.assertEqual(self.req.extra_user_content_parts, [])
        self.config["provider_settings"]["default_personality"] = "new"
        self.cache.attach(self.event, self.req)
        self.assertEqual(self.req.extra_user_content_parts, [])
        self.config["provider_settings"]["default_personality"] = "ling"
        self.req.func_tool.tools = []
        self.cache.attach(self.event, self.req)
        self.assertEqual(self.req.extra_user_content_parts, [])
        self.req.func_tool.tools = [self.tool]
        self.event.message_str = "今天吃什么"
        self.cache.attach(self.event, self.req)
        self.assertEqual(self.req.extra_user_content_parts, [])

    def test_errors_and_side_effects_never_become_evidence(self):
        for result in ('{"ok":false,"text":"失败"}', "查询失败", None,
                       SimpleNamespace(isError=True, content=[])):
            self.remember(result)
        for name in ("search_memory", "web_publish", "send_message"):
            self.cache.remember(self.event, SimpleNamespace(name=name),
                                {"query": "Python字典"}, "应忽略")
        self.assertEqual(len(self.cache.rows), 0)

    def test_mcp_content_and_bounded_retention(self):
        self.remember(SimpleNamespace(isError=False, content=[SimpleNamespace(text="官方文字")]))
        self.assertEqual(next(iter(self.cache.rows.values()))["text"], "官方文字")
        for index in range(300):
            self.cache.remember(self.event, self.tool, {"query": str(index)}, "资料")
        self.assertEqual(len(self.cache.rows), 256)
