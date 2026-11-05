"""由真实Decision构建可解释流程；文字试跑不会执行主回复生成。"""
from __future__ import annotations

import copy

GATE = [
    ("input", "候选输入", "取得当前消息、上一句、上下文；群聊明确呼叫与公开监听采用不同入口。"),
    ("fragment", "碎片等待与撤回", "等待同一用户的碎片消息；只在等待窗口内拦截撤回。文字试跑不模拟平台事件。"),
    ("rules", "本地规则与预算", "检查最短长度、有效内容和每群小时预算。试跑跳过预算计数；规则不通过立即静默。"),
    ("context", "组织判定上下文", "有效期0不筛时间，只取已配置条数；正数先筛时间再取条数。保留字符上限、会话隔离及从旧到新的顺序。"),
    ("text", "文字模型初判", "返回动作、参与分数、理解状态、依据类型和原因。协议不完整或超时按静默处理。"),
    ("memory", "按需记忆与复判", "只有模型需要记忆时补查一次；命中后复判，未找到不能猜。文字试跑不读取真实用户记忆。"),
    ("vision", "必要时看图", "只有需要画面证据时调用视觉模型；有效静默结论不触发兜底。文字试跑不上传原图。"),
    ("fallback", "视觉异常主模型兜底", "仅视觉接口或协议异常才尝试不同主模型；预算耗尽或总时限到期不兜底。"),
    ("veto", "语义否决与分数修正", "互聊、停口、已答、无价值、未理解等原因优先否决。分数压至20或40，阈值为0也不能越过。"),
    ("threshold", "比较已保存阈值", "没有语义否决且修正分数达到阈值才放行。"),
    ("result", "判定结果", "硬闸门用结论控制生成；观察模式只记录结论，线上继续回复。"),
]
GENERATE = [
    ("native", "选原生模型与固定人格", "按当前会话的原生配置选主模型。页面保存的人格在宿主选择入口生效，从绫也复用同一选择。"),
    ("perception", "复用感知与图片证据", "从绫启用时使用从绫感知；独立模式使用内容理解配置。充分图片摘要优先复用，不足时传原图。"),
    ("retrieve", "补足话题依据", "复用本次记忆、联网搜索和向量查询结果；确实缺信息时由主模型按工具能力补查。"),
    ("writeback", "证据与记忆回写", "同次请求复用查询缓存；有效长期事实可由记忆工具回写，避免后续反复查询。是否调用由实际工具执行记录确认。"),
    ("generate", "主模型生成正文", "以当前问题、已验证证据及唯一人格生成回复。主模型接口失败时按原生设置重试和回退。"),
    ("image", "按需生成或编辑图片", "主模型可调用LivingHello独立AI生图工具；使用已注册模型或路由上游，按日共享额度，图生图仅用本次图片。"),
    ("segments", "正文清理与分段", "模型自行分段和多消息发送独立。第二句号、分段词需各自开启；最多分段数超出后合并保留正文。"),
    ("freshness", "最后一次时效检查", "检查当前消息是否被新消息替代、重复、超时或等待窗口内撤回；从绫准备耗时按配套设置扣除。"),
    ("send", "原生发送", "经AstrBot原生管线处理TTS、文本排版转图、引用与@并发送；文字试跑到此仅展示计划。"),
]
GATE_EDGES = [
    ("input", "fragment", "线上入口"), ("fragment", "rules", "窗口内未撤回"),
    ("rules", "context", "规则通过"), ("rules", "result", "短路静默"),
    ("context", "text", "送入模型"), ("text", "memory", "需要记忆"),
    ("text", "vision", "需要画面"), ("text", "veto", "证据足够"),
    ("memory", "text", "命中后复判"), ("memory", "veto", "无记忆/复判完成"),
    ("vision", "fallback", "接口或协议异常"), ("vision", "veto", "有效结论"),
    ("fallback", "veto", "兜底成功"), ("veto", "threshold", "无语义否决"),
    ("veto", "result", "否决静默"), ("threshold", "result", "放行/静默"),
]
MODES = {
    "enforce": {
        "id": "enforce",
        "title": "硬闸门",
        "summary": "完整判断；静默立即停止，放行才生成。",
        "decision_effect": "判断结论直接控制是否进入主回复生成。",
        "probe_note": "试跑结论等同硬闸门的线上参与处理，但试跑本身不发送消息。",
    },
    "shadow": {
        "id": "shadow",
        "title": "观察模式",
        "summary": "同步完整判断并记录；无论结论都继续回复。",
        "decision_effect": "判断结论只用于记录，不执行参与拦截。",
        "probe_note": "试跑仍执行真实判断；线上观察模式不会按该结论停止主回复。",
    },
}


def graph(rows, edges):
    return {"nodes": [dict(id=k, title=t, explanation=e, status="unrecorded", evidence={})
                      for k, t, e in rows],
            "edges": [dict(source=a, target=b, label=c) for a, b, c in edges]}


def build_flow(config, native=None, decision=None, inputs=None):
    mode_id = str(config.get("gate_mode", "enforce") or "enforce").strip().lower()
    mode_id = mode_id if mode_id in MODES else "enforce"
    mode = copy.deepcopy(MODES[mode_id])
    gate = graph(GATE, GATE_EDGES)
    generate = graph(GENERATE, [(GENERATE[i][0], GENERATE[i + 1][0], "下一步")
                               for i in range(len(GENERATE) - 1)])
    nodes = {n["id"]: n for n in gate["nodes"]}
    plan = {n["id"]: n for n in generate["nodes"]}
    native = native or {}
    values = native.get("values", {})
    for node in plan.values():
        node.update(status="planned", evidence={"执行状态": "仅生成计划，未调用主模型或发送消息"})
    plan["native"]["evidence"].update(
        原生配置=native.get("config_id", "default"),
        主模型=values.get("provider_id") or "宿主默认",
        原生人格=values.get("persona_id", "当前宿主不支持"),
        固定人格=native.get("fixed_persona_id") or "尚未由此页固定",
        回退模型=values.get("fallback_provider_ids", []))
    plan["perception"]["evidence"].update(
        图片理解=config.get("understand_image_provider_id") or "跟随独立内容理解配置",
        说明="从绫启用状态与具体工具结果需以实际会话记录为准")
    plan["send"]["evidence"]["文本排版转图"] = values.get("t2i", False)
    nodes["rules"]["evidence"] = dict(最短长度=config.get("gate_min_length", 2),
                                        小时预算=config.get("gate_hourly_budget", 120))
    nodes["threshold"]["evidence"] = {"阈值": config.get("participation_confidence_threshold", 60)}
    nodes["result"]["explanation"] = mode["decision_effect"]
    nodes["result"]["evidence"] = {
        "工作模式": mode["title"],
        "线上处理": "按判断结论决定是否生成" if mode_id == "enforce" else "继续回复（判断仅记录）",
    }
    if decision is not None:
        d = decision.as_dict()
        raw = getattr(decision, "raw", "")
        inputs = inputs or {}
        nodes["input"].update(status="passed", evidence=copy.deepcopy(inputs))
        nodes["fragment"].update(status="skipped", evidence={"原因": "文字试跑不执行平台碎片等待与撤回"})
        early = d["source"] in {"rule", "error"} and not d["attempts"]
        nodes["rules"]["status"] = "blocked" if d["source"] == "rule" else "passed"
        nodes["rules"]["evidence"].update(预算="试跑跳过计数与预算消耗", 结果=d["reason"] if d["source"] == "rule" else "本地规则通过")
        nodes["context"].update(status="skipped" if early else "passed",
                                evidence={"送给模型的数据": d.get("chat_data", {}),
                                          "是否发送": not early})
        for key, stage in (("text", "text"), ("vision", "vision"), ("fallback", "main_fallback")):
            attempts = [a for a in d["attempts"] if a.get("stage") == stage]
            node = nodes[key]
            node["status"] = ("passed" if attempts[-1].get("status") == "ok" else "error") if attempts else "skipped"
            node["evidence"] = {"实际尝试": attempts, "原因": "查看实际尝试中的模型、耗时和结果" if attempts
                                else ("取判定模型失败" if key == "text" and d["reason"] == "no_provider"
                                      else "本次未执行此分支")}
        metric = d.get("timings", {}).get("memory")
        nodes["memory"].update(status="passed" if metric else "skipped",
                               evidence={"实际耗时记录": metric, "命中记忆": list(decision.memory_lines),
                                         "说明": "文字试跑不读取真实用户记忆，缺失记忆会否决"})
        successful = d["source"] == "model"
        nodes["veto"].update(status=("blocked" if d["action"] == "silence" and d["reason"] != "below_threshold"
                                    else "passed") if successful else "skipped",
                             evidence={"模型动作": d["act"], "已理解": d["understood"], "依据类型": d["ground"],
                                       "模型原因": d["model_reason"], "原分数": d["raw_score"],
                                       "修正分数": d["score"], "最终模型原话": raw})
        nodes["threshold"].update(status=("passed" if d["action"] == "reply" else "blocked") if successful else "skipped",
                                  evidence={"修正分数": d["score"], "阈值": d["threshold"],
                                            "说明": "阈值通过仍不能绕过语义否决"})
        observation_rules = list((decision.chat_data or {}).get("observation_rules", []))
        online_action = (
            "继续生成" if d["action"] == "reply" else "停止生成"
        ) if mode_id == "enforce" else "继续回复（观察模式不拦截）"
        nodes["result"].update(
            status=("passed" if d["action"] == "reply" else "blocked")
            if mode_id == "enforce" else "observed",
            evidence={
                "判断动作": d["action"],
                "判断原因": d["reason"],
                "线上处理": online_action,
                "观察但未拦截的本地规则": observation_rules,
                "实际模型调用数": d["model_calls"],
                "判定总耗时ms": d["latency_ms"],
                "工作模式": mode["title"],
            },
        )
        if d["action"] != "reply" and mode_id == "enforce":
            for node in plan.values():
                node.update(status="skipped", evidence={**node["evidence"], "原因": "硬闸门未放行，线上不会生成"})
    note = (
        "点击节点查看依据。硬闸门未放行不会生成；试跑只执行判断，不发送消息。"
        if mode_id == "enforce"
        else "点击节点查看依据。观察模式的判断只记录，线上仍继续回复；试跑不发送消息。"
    )
    return {"mode": mode, "gate": gate, "generate": generate,
            "note": note + " 未采集的数据不等于0耗时。"}
