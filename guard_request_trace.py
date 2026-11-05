"""有界请求诊断：只采集步骤与受控元数据，存储失败不阻塞业务。"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import re
import sqlite3
import time
import uuid
from collections import OrderedDict
from pathlib import Path

ID_KEY = "_livinghello_request_id"
TRACE_KEY = "_livinghello_trace"
ID_RE = re.compile(r"^[0-9a-f]{32}$")
TERMINAL = {"completed", "cancelled", "failed", "merged", "incomplete", "silent"}
LISTENERS = {
    "receive": ("AdapterMessageEvent", "observe_message_fragments"),
    "input_parse": ("AdapterMessageEvent", "parse_reply_input"),
    "fragments": ("AdapterMessageEvent", "coalesce_message_fragments"),
    "recall": ("AdapterMessageEvent", "prepare_reply_delivery"),
    "understanding": ("OnLLMRequestEvent", "understand_reply_input"),
    "decision": ("OnLLMRequestEvent", "guard_llm_request"),
    "contracts": ("OnLLMRequestEvent", "attach_reply_contract"),
    "main_model": ("OnAgentBeginEvent", "trace_agent_begin"),
    "response": ("OnLLMResponseEvent", "suppress_no_reply_and_track"),
    "tool_start": ("OnUsingLLMToolEvent", "trace_tool_start"),
    "tool_end": ("OnLLMToolRespondEvent", "remember_tool_evidence"),
    "agent_done": ("OnAgentDoneEvent", "trace_agent_done"),
    "decorate": ("OnDecoratingResultEvent", "recheck_before_send"),
    "send": ("OnAfterMessageSentEvent", "track_sent_message"),
}
META_KEYS = {"reason", "model", "calls", "ms", "span_id", "tool", "count", "before",
             "after", "synced", "judged", "types", "source", "collected", "enabled", "parent_span",
             "merged_into", "api_calls", "packets", "error_type", "role"}


def emit(event, feature, status, **metadata):
    """事件局部引用避免跨任务/会话串线；不接受正文、参数或响应。"""
    getter = getattr(event, "get_extra", None)
    service = getter(TRACE_KEY) if callable(getter) else None
    if service is not None:
        return service.record(event, feature, status, **metadata)
    return None


def touch(event, name):
    getter = getattr(event, "get_extra", None)
    service = getter(TRACE_KEY) if callable(getter) else None
    if service is not None:
        service.last_seen[name] = time.time()


def registry_coverage(plugin, registry=None, event_types=None, stars=None):
    """核对激活注册表与当前实例，不用装饰器替身或工具缓存证明覆盖。"""
    try:
        if registry is None:
            from astrbot.core.star.star_handler import star_handlers_registry, EventType
            from astrbot.core.star.star import star_map
            registry, event_types, stars = star_handlers_registry, EventType, star_map
        module = type(plugin).__module__
        owner = stars.get(module)
        current = bool(owner and owner.activated and owner.star_cls is plugin)
        result = {}
        for key, (event_name, handler_name) in LISTENERS.items():
            kind = getattr(event_types, event_name, None)
            status = "unsupported" if kind is None else "unregistered"
            if kind is not None and not current:
                status = "unverified"
            elif kind is not None:
                found = [h for h in registry.get_handlers_by_event_type(kind, only_activated=True)
                         if h.handler_module_path == module and h.handler_name == handler_name]
                status = "registered" if len(found) == 1 else "unregistered"
            result[key] = {"status": status, "event": event_name, "handler": handler_name}
        return result
    except Exception:
        return {key: {"status": "unverified", "event": kind, "handler": handler}
                for key, (kind, handler) in LISTENERS.items()}


class RequestTrace:
    MAX_REQUESTS = 600
    MAX_EVENTS = 96
    MAX_BYTES = 64 * 1024
    MAX_PENDING = 128

    def __init__(self, plugin, data_dir):
        self.plugin = plugin
        self.path = Path(data_dir) / "request_traces.sqlite3" if data_dir else None
        self.instance_id = uuid.uuid4().hex
        self.rows = OrderedDict()
        self.pending = OrderedDict()
        self.last_seen = {}
        self.writer = None
        self.closed = False
        self.health = {"storage": "pending" if self.path else "unavailable",
                       "dropped": 0, "write_errors": 0}
        self._coverage = None
        self._coverage_at = 0.0

    def listeners(self):
        if self._coverage is None or time.monotonic() - self._coverage_at > 5:
            self._coverage = registry_coverage(self.plugin)
            self._coverage_at = time.monotonic()
        config = getattr(self.plugin, "config", {})
        return {"instance_id": self.instance_id, "coverage": copy.deepcopy(self._coverage),
                "last_seen": dict(self.last_seen), "health": dict(self.health),
                "pending": len(self.pending), "active": sum(r["state"] not in TERMINAL for r in self.rows.values()),
                "features": {key: config.get(key) for key in (
                    "gate_mode", "expression_takeover_enabled", "reply_typing_enabled",
                    "reply_recall_group", "reply_recall_private", "understand_image_enabled",
                    "understand_voice_enabled")}}

    def begin(self, event):
        request_id = event.get_extra(ID_KEY)
        if not isinstance(request_id, str) or not ID_RE.fullmatch(request_id):
            request_id = uuid.uuid4().hex
            event.set_extra(ID_KEY, request_id)
        event.set_extra(TRACE_KEY, self)
        if request_id in self.rows or self.closed:
            return request_id
        if len(self.rows) >= self.MAX_REQUESTS:
            ended = next((key for key, row in self.rows.items() if row["state"] in TERMINAL), None)
            if ended:
                self.rows.pop(ended)
            else:
                self.health["dropped"] += 1
                event.set_extra("_livinghello_trace_limited", True)
                return request_id
        stamp = time.time()
        self.rows[request_id] = {
            "schema_version": 1, "request_id": request_id,
            "session_id": str(getattr(event, "unified_msg_origin", "") or "")[:1024],
            "message_id": str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")[:256],
            "started_at": stamp, "updated_at": stamp, "instance_id": self.instance_id,
            "state": "active", "coverage": self.listeners()["coverage"], "events": [],
            "features": {}, "spans": {}, "next_span": 0, "source_ids": [],
            "summary": {"tool_calls": 0, "image_api_calls": 0, "sent_packets": 0},
            "config_snapshot": self.listeners()["features"],
            "truncated": False,
        }
        self.record(event, "receive", "success")
        return request_id

    def identity(self, event):
        rid = self.begin(event)
        return {"request_id": rid,
                "message_id": str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")[:256]}

    @staticmethod
    def _metadata(values):
        clean = {}
        for key, value in values.items():
            if key not in META_KEYS:
                continue
            if isinstance(value, (bool, int, float)) or value is None:
                if isinstance(value, (int, float)) and (
                        abs(value) > 1e12 or not math.isfinite(value)):
                    continue
                clean[key] = value
            elif isinstance(value, str):
                # 名称不是认证地址；不存储异常原文。
                if "://" not in value:
                    clean[key] = value[:160]
            elif key == "types" and isinstance(value, list):
                clean[key] = [str(v)[:40] for v in value[:20]]
        return clean

    def record(self, event, feature, status, **values):
        rid = event.get_extra(ID_KEY)
        row = self.rows.get(rid)
        if row is None or self.closed:
            return None
        feature = str(feature)[:80]
        # 动态工具名不作为无限增长的feature key。
        if feature not in row["features"] and len(row["features"]) >= 48:
            feature = "other"
            row["truncated"] = True
        clean = self._metadata(values)
        entry = {"at": time.time(), "feature": feature, "status": status, **clean}
        row["updated_at"] = entry["at"]
        row["events"].append(entry)
        row["features"][feature] = {"status": status, **clean}
        if len(row["events"]) > self.MAX_EVENTS:
            row["events"] = row["events"][:1] + row["events"][-(self.MAX_EVENTS - 1):]
            row["truncated"] = True
        self._queue(row)
        return entry

    def start(self, event, feature, **values):
        rid = self.begin(event)
        row = self.rows.get(rid)
        if not row:
            return None
        row["next_span"] += 1
        span = row["next_span"]
        if len(row["spans"]) < self.MAX_EVENTS:
            row["spans"][str(span)] = {"feature": feature, "started": time.monotonic()}
        self.record(event, feature, "running", span_id=span, **values)
        return span

    def end(self, event, span, status="success", **values):
        row = self.rows.get(event.get_extra(ID_KEY))
        started = row["spans"].pop(str(span), None) if row else None
        if started:
            self.record(event, started["feature"], status, span_id=span,
                        ms=round((time.monotonic() - started["started"]) * 1000, 2), **values)

    def increment(self, event, key):
        row = self.rows.get(event.get_extra(ID_KEY))
        if row and key in row["summary"]:
            row["summary"][key] += 1

    def finish(self, event, state, reason=""):
        row = self.rows.get(event.get_extra(ID_KEY))
        if row:
            row["state"] = state
            self.record(event, "lifecycle", state, reason=reason)

    def merge(self, event, sources):
        canonical = self.begin(event)
        row = self.rows.get(canonical)
        for source in sources:
            old = self.begin(source)
            if old == canonical:
                continue
            previous = self.rows.get(old)
            if previous:
                previous["state"] = "merged"
                previous["merged_into"] = canonical
                self.record(source, "fragments", "merged", merged_into=canonical)
                if row:
                    # 有界保留早期输入证据；不持有来源event。
                    row["events"].extend(previous["events"][:8])
                    row["features"].update({k: v for k, v in previous["features"].items()
                                            if k in {"input_parse", "input_vision", "speech_to_text"}})
            if row and len(row["source_ids"]) < self.MAX_EVENTS:
                row["source_ids"].append(old)
            source.set_extra(ID_KEY, canonical)
            source.set_extra("_livinghello_merged_into", canonical)
            source.set_extra(TRACE_KEY, self)
        self.record(event, "fragments", "success", count=len(sources))

    def metrics(self, event, timings):
        for key in ("text", "vision", "main_fallback", "memory", "embedding",
                    "vector_index", "memory_read", "input_parse", "input_vision", "speech_to_text"):
            value = timings.get(key)
            if isinstance(value, dict):
                state = {"ok": "success", "empty": "success", "timeout": "timeout",
                         "error": "failed", "cancelled": "cancelled"}.get(value.get("status"), "uncollected")
                self.record(event, key, state, ms=value.get("ms"), calls=value.get("calls"),
                            collected=timings.get("retrieval_collected") if key in {
                                "embedding", "vector_index", "memory_read"} else True)
            elif key in {"embedding", "vector_index", "memory_read"}:
                self.record(event, key, "uncollected", reason="retrieval_detail_missing")
            elif key not in self.rows.get(event.get_extra(ID_KEY), {}).get("features", {}):
                self.record(event, key, "skipped", calls=0, reason="decision_branch_not_called")
        for key, value in timings.items():
            if (key == "vector_adapter" or key.startswith("retrieval_")) and isinstance(value, dict):
                self.record(event, key, {"ok": "success", "empty": "success", "error": "failed",
                    "timeout": "timeout", "cancelled": "cancelled"}.get(value.get("status"), "uncollected"),
                    ms=value.get("ms"), calls=value.get("calls"))

    def _snapshot(self, row):
        result = {key: copy.deepcopy(value) for key, value in row.items()
                  if key not in {"spans", "next_span"}}
        if len(result["events"]) > self.MAX_EVENTS:
            result["events"] = result["events"][:1] + result["events"][-(self.MAX_EVENTS - 1):]
            result["truncated"] = True
        while len(json.dumps(result, ensure_ascii=False).encode()) > self.MAX_BYTES:
            result["truncated"] = True
            if len(result["events"]) > 2:
                del result["events"][1]
            elif result["source_ids"]:
                result["source_ids"].pop(0)
            else:
                # 固定结构与字段上限使此处通常不需要执行。
                break
        return result

    def _queue(self, row):
        if not self.path or self.closed:
            return
        rid = row["request_id"]
        if rid not in self.pending and len(self.pending) >= self.MAX_PENDING:
            victim = next((key for key, value in self.pending.items()
                           if value["state"] not in TERMINAL), None)
            if victim is None or row["state"] not in TERMINAL:
                self.health["dropped"] += 1
                return
            self.pending.pop(victim)
            self.health["dropped"] += 1
        self.pending[rid] = self._snapshot(row)
        try:
            if self.writer is None or self.writer.done():
                self.writer = asyncio.get_running_loop().create_task(self._drain())
        except RuntimeError:
            pass

    def _write(self, batch):
        new = not self.path.exists()
        with sqlite3.connect(str(self.path), timeout=1) as db:
            if new:
                self.path.chmod(0o600)
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY, schema_version INTEGER, session_id TEXT,
                message_id TEXT, started_at REAL, updated_at REAL, instance_id TEXT,
                state TEXT, payload_json TEXT)""")
            db.executemany("INSERT OR REPLACE INTO requests VALUES (?,?,?,?,?,?,?,?,?)", [
                (r["request_id"], 1, r["session_id"], r["message_id"], r["started_at"],
                 r["updated_at"], r["instance_id"], r["state"], json.dumps(r, ensure_ascii=False))
                for r in batch])
            db.execute("""DELETE FROM requests WHERE request_id IN (
                SELECT request_id FROM requests ORDER BY
                CASE WHEN state='active' THEN 1 ELSE 0 END, updated_at ASC
                LIMIT MAX(0, (SELECT COUNT(*) FROM requests)-?))""", (self.MAX_REQUESTS,))

    async def _drain(self):
        # 同一writer串行to_thread，关闭不取消正在写入的线程。
        while self.pending:
            batch = list(self.pending.values())
            self.pending.clear()
            try:
                await asyncio.to_thread(self._write, batch)
                self.health["storage"] = "ready"
            except Exception:
                self.health["storage"] = "unavailable"
                self.health["write_errors"] += 1
                self.health["dropped"] += len(batch)

    def _read(self, ids):
        if not self.path or not self.path.exists():
            return {}
        with sqlite3.connect("file:" + str(self.path) + "?mode=ro", uri=True, timeout=1) as db:
            query = "SELECT request_id, payload_json FROM requests WHERE request_id IN (" + ",".join("?" for _ in ids) + ")"
            found = {}
            for key, text in db.execute(query, ids):
                row = json.loads(text)
                if row.get("state") not in TERMINAL and row.get("instance_id") != self.instance_id:
                    row["state"] = "incomplete"
                    row["completion_note"] = "结束状态未采集"
                found[key] = row
            return found

    async def batch(self, ids):
        ids = list(dict.fromkeys(key for key in ids if isinstance(key, str) and ID_RE.fullmatch(key)))[:600]
        if not ids:
            return {}
        result = {key: self._snapshot(self.rows[key]) for key in ids if key in self.rows}
        missing = [key for key in ids if key not in result]
        if missing:
            try:
                result.update(await asyncio.to_thread(self._read, missing))
            except Exception:
                self.health["storage"] = "unavailable"
        return result

    async def get(self, rid):
        result = (await self.batch([rid])).get(rid)
        return result

    async def enrich(self, decisions):
        found = await self.batch([row.get("request_id") for row in decisions])
        return [{**row, "trace_summary": {key: found[row["request_id"]][key]
                    for key in ("state", "summary", "truncated", "instance_id")}
                 if row.get("request_id") in found else None} for row in decisions]

    async def close(self):
        if self.closed:
            return
        for row in self.rows.values():
            if row["state"] not in TERMINAL:
                row["state"] = "incomplete"
                row["completion_note"] = "结束状态未采集"
                self._queue(row)
        self.closed = True
        if self.writer:
            try:
                await asyncio.wait_for(asyncio.shield(self.writer), 3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.health["storage"] = "flush_incomplete"
