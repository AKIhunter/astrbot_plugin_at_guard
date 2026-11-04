import unittest

from guard_engine import Decision
from guard_flow import build_flow


class FlowTests(unittest.TestCase):
    def nodes(self, decision):
        flow=build_flow({"gate_mode":"enforce"}, decision=decision, inputs={"消息":"测试"})
        return {n["id"]:n for n in flow["gate"]["nodes"]},flow

    def test_rule_short_circuit_does_not_claim_model_executed(self):
        nodes,flow=self.nodes(Decision(reason="too_short",source="rule"))
        self.assertEqual(nodes["rules"]["status"],"blocked")
        self.assertEqual(nodes["text"]["status"],"skipped")
        self.assertTrue(all(n["status"]=="skipped" for n in flow["generate"]["nodes"]))

    def test_attempts_preserve_model_failure_and_missing_timings(self):
        d=Decision(reason="timeout",source="error",attempts=[dict(stage="text",model="test-model",status="timeout",called=True,ms=17)])
        nodes,_=self.nodes(d)
        self.assertEqual(nodes["text"]["status"],"error")
        self.assertEqual(nodes["text"]["evidence"]["实际尝试"][0]["ms"],17)
        self.assertIsNone(nodes["memory"]["evidence"]["实际耗时记录"])

    def test_semantic_veto_even_with_zero_threshold(self):
        nodes,_=self.nodes(Decision(reason="others_talking",source="model",threshold=0,raw_score=99,score=20,understood=True))
        self.assertEqual(nodes["veto"]["status"],"blocked")
        self.assertEqual(nodes["result"]["status"],"blocked")
        self.assertEqual(nodes["veto"]["evidence"]["原分数"],99)

    def test_allowed_reply_is_only_generation_plan(self):
        _,flow=self.nodes(Decision(reason="allowed",source="model",action="reply",score=88))
        self.assertTrue(all(n["status"]=="planned" for n in flow["generate"]["nodes"]))
        ids={n["id"] for n in flow["gate"]["nodes"]}
        self.assertTrue(all(e["source"] in ids and e["target"] in ids for e in flow["gate"]["edges"]))
