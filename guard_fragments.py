"""同会话、同发送者的安静期合并；原始平台消息由宿主照常归档。"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass(eq=False)
class Ticket:
    event: object
    snapshot: dict
    batch: Batch | None = None
    reason: str = ""


@dataclass(eq=False)
class Batch:
    key: tuple[str, str]
    deadline: float
    quiet_seconds: float
    clock: object = time.monotonic
    explicit: bool = False
    items: list[Ticket] = field(default_factory=list)
    latest: Ticket | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    characters: int = 0
    blocked: bool = False
    finished: bool = False


class FragmentWait:
    CAPACITY = 256
    MAX_MESSAGES = 128
    MAX_CHARACTERS = 65536
    DEDUP_CAPACITY = 4096
    DEDUP_TTL = 120

    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.batches = OrderedDict()
        self.seen = OrderedDict()
        self.tasks = set()
        self.closed = False

    def observe(self, event, key, snapshot, seconds, explicit=False, message_id=""):
        """同步登记到达时间，不能被撤回窗口、归档或模型耗时推迟。"""
        now = self.clock()
        ticket = Ticket(event, snapshot)
        if self.closed:
            ticket.reason = "fragment_closed"
            return ticket
        while self.seen and now - next(iter(self.seen.values())) >= self.DEDUP_TTL:
            self.seen.popitem(last=False)
        if message_id and message_id not in {"0", "None", "null"}:
            identity = (key[0], message_id)
            if identity in self.seen:
                ticket.reason = "fragment_duplicate"
                return ticket
            self.seen[identity] = now
            while len(self.seen) > self.DEDUP_CAPACITY:
                self.seen.popitem(last=False)
        # 已经过安静期的新消息属于下一批，不能追溯延长已经结束的倒计时。
        for old_key, old in list(self.batches.items()):
            if old.deadline <= now:
                self.batches.pop(old_key)
        batch = self.batches.get(key)
        if batch is None:
            if len(self.batches) >= self.CAPACITY:
                ticket.reason = "fragment_capacity"
                return ticket
            batch = Batch(key, now + seconds, seconds, clock=self.clock)
            self.batches[key] = batch
        ticket.batch = batch
        batch.latest = ticket
        batch.deadline = now + seconds
        batch.explicit = batch.explicit or explicit
        batch.characters += len(snapshot.get("text", ""))
        if (len(batch.items) >= self.MAX_MESSAGES
                or batch.characters > self.MAX_CHARACTERS):
            # 不截掉半句话后作判断；本批静默，归档仍保留全部原文。
            batch.blocked = True
            batch.items.clear()
        if not batch.blocked:
            batch.items.append(ticket)
        batch.changed.set()
        return ticket

    async def wait(self, ticket):
        """只让最后一条触发继续；同用户续发唤醒旧等待者并淘汰旧触发。"""
        batch = ticket.batch
        if batch is None:
            return None
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            while True:
                if self.closed:
                    raise asyncio.CancelledError
                if batch.blocked or batch.finished:
                    return None
                if batch.latest is not ticket:
                    return None
                remaining = batch.deadline - self.clock()
                if remaining <= 0:
                    batch.finished = True
                    self._remove(batch)
                    return tuple(batch.items)
                batch.changed.clear()
                try:
                    await asyncio.wait_for(batch.changed.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            if batch.latest is ticket:
                batch.blocked = True
                batch.changed.set()
                self._remove(batch)
            raise
        finally:
            self.tasks.discard(task)

    def _remove(self, batch):
        if self.batches.get(batch.key) is batch:
            self.batches.pop(batch.key)

    async def close(self):
        self.closed = True
        for batch in self.batches.values():
            batch.blocked = True
            batch.changed.set()
        tasks = [task for task in self.tasks
                 if task is not asyncio.current_task() and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.batches.clear()
        self.seen.clear()
