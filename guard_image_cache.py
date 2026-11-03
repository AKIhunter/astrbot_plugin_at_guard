"""同一完整会话的短期图片观察缓存；不缓存动作/分数，不下载图片。"""
import asyncio
import copy
import hashlib
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit


class ImageObservationCache:
    def __init__(self, max_entries=256, clock=time.monotonic):
        self.max_entries = max_entries
        self.clock = clock
        self.entries = OrderedDict()
        self.locks = {}
        self.stats = {"hits": 0, "misses": 0, "expired": 0, "evicted": 0, "writes": 0}

    @staticmethod
    def fingerprint(reference):
        value = str(reference or "")
        # 本地路径变化须立即失效；URL/内嵌图片按完整引用哈希，不删除签名参数。
        path = None
        if value.startswith("file://"):
            path = Path(unquote(urlsplit(value).path))
        elif value.startswith("/"):
            path = Path(value)
        if path is not None:
            try:
                info = path.stat()
                value += f"|{info.st_size}|{info.st_mtime_ns}"
            except OSError:
                return None
        return hashlib.sha256(value.encode()).hexdigest() if value else None

    def _expire(self):
        now = self.clock()
        for key, entry in list(self.entries.items()):
            if now >= entry["expires"]:
                self.entries.pop(key, None)
                self.stats["expired"] += 1

    def lookup(self, scope, namespace, images, ttl):
        self._expire()
        found = []
        if not scope or ttl <= 0:
            return found
        now = self.clock()
        for index, image in enumerate(images[:3], 1):
            digest = self.fingerprint(image.get("url"))
            key = (scope, namespace, digest)
            entry = self.entries.get(key) if digest else None
            # 调小TTL立即生效，命中不续期。
            if entry and now - entry["written"] < ttl:
                self.entries.move_to_end(key)
                self.stats["hits"] += 1
                found.append({
                    **copy.deepcopy(entry["summary"]),
                    "image": index, "cached": True,
                    "cache_age_seconds": round(now - entry["written"], 1),
                    "model": entry["model"],
                })
            else:
                self.stats["misses"] += 1
        return found

    def store(self, scope, namespace, images, summaries, model, ttl):
        self._expire()
        if not scope or ttl <= 0:
            return
        now = self.clock()
        for summary in summaries[:3]:
            index = summary.get("image")
            if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= len(images):
                continue
            # 只来自已通过协议校验的视觉观察；未知/空图不能作为有效结果。
            content = summary.get("summary")
            if not isinstance(content, str) or not content.strip():
                continue
            digest = self.fingerprint(images[index - 1].get("url"))
            if not digest:
                continue
            value = {
                "summary": content[:400],
                "uncertainty": str(summary.get("uncertainty", ""))[:200],
                "needs_original": summary.get("needs_original") is not False,
            }
            self.entries[(scope, namespace, digest)] = {
                "summary": value, "model": str(model)[:200],
                "written": now, "expires": now + ttl,
            }
            self.entries.move_to_end((scope, namespace, digest))
            self.stats["writes"] += 1
            while len(self.entries) > self.max_entries:
                self.entries.popitem(last=False)
                self.stats["evicted"] += 1

    @asynccontextmanager
    async def session_lock(self, scope, timeout=2):
        # 同会话并发先等待观察写入，再按各自最新消息重新判定，不能复用旧的reply。
        state = self.locks.setdefault(scope, {"lock": asyncio.Lock(), "users": 0})
        state["users"] += 1
        acquired = False
        try:
            await asyncio.wait_for(state["lock"].acquire(), timeout)
            acquired = True
            yield
        finally:
            if acquired:
                state["lock"].release()
            state["users"] -= 1
            if not state["users"]:
                self.locks.pop(scope, None)

    def snapshot(self):
        self._expire()
        return {**self.stats, "entries": len(self.entries), "capacity": self.max_entries}
