"""用量统计与事件重放（WX-006 修正 EventReplay）。

旧 `take()` 先 pop 再返回，恒为 None 且数据已删 —— 暂存只是堆积。
新契约：

* `take(key)` **peek**：返回暂存事件且不删除；无事件返回 None。
* `ack(key)`：投递确认后才删除。
* `stage(key, payload)`：同 key 覆盖（保留最新），超过上限淘汰最旧。
* `failed(key, error)`：记录失败计数与退避，供重放器做有界重试。

重放策略（RecoveryManager）：
* 有界退避：base 5s，×2 封顶 10 分钟；超过 max_attempts（默认 8）
  进入 dead 状态并保留事件（可观测），不静默丢弃。
* 容量 / 淘汰 / 终止状态都可通过 snapshot() 观察。
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any


class UsageStats:
    """每会话的收发计数 + 最近错误。"""

    def __init__(self, max_sessions: int = 200) -> None:
        self._recv: dict[str, int] = defaultdict(int)
        self._sent: dict[str, int] = defaultdict(int)
        self._recv_bytes: dict[str, int] = defaultdict(int)
        self._started = time.time()
        self._max_sessions = max_sessions

    def count_recv(self, session_id: str, text_len: int = 0) -> None:
        self._recv[session_id] += 1
        self._recv_bytes[session_id] += int(text_len or 0)
        self._trim()

    def count_sent(self, session_id: str) -> None:
        self._sent[session_id] += 1
        self._trim()

    def _trim(self) -> None:
        if len(self._recv) <= self._max_sessions and len(self._sent) <= self._max_sessions:
            return
        drop = max(1, self._max_sessions // 4)
        for store in (self._recv, self._sent, self._recv_bytes):
            for key in list(store)[:drop]:
                store.pop(key, None)

    def top(self, limit: int = 10) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for sid, n in self._recv.items():
            merged.setdefault(sid, {'session': sid, 'recv': 0, 'sent': 0})['recv'] = n
        for sid, n in self._sent.items():
            merged.setdefault(sid, {'session': sid, 'recv': 0, 'sent': 0})['sent'] = n
        rows = sorted(merged.values(),
                      key=lambda r: r['recv'] + r['sent'], reverse=True)
        return rows[:limit]

    def snapshot(self, top_limit: int = 10) -> dict[str, Any]:
        return {
            'uptime_s': int(time.time() - self._started),
            'total_recv': sum(self._recv.values()),
            'total_sent': sum(self._sent.values()),
            'top': self.top(top_limit),
        }


class EventReplay:
    """投递失败的事件暂存：peek -> 重放 -> ack 成功才删除。"""

    BASE_BACKOFF_S = 5.0
    MAX_BACKOFF_S = 600.0

    def __init__(self, max_events: int = 200, max_attempts: int = 8) -> None:
        self._events: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._max = max_events
        self._attempts: dict[str, int] = defaultdict(int)
        self._last_error: dict[str, str] = {}
        self._next_retry_at: dict[str, float] = {}
        self._max_attempts = max_attempts
        self._dead: set[str] = set()
        self._acked_total = 0
        self._staged_total = 0

    # ------------------------------------------------------------ 暂存

    def stage(self, key: str, payload: dict[str, Any]) -> None:
        if key not in self._events:
            self._order.append(key)
        self._events[key] = payload
        self._staged_total += 1
        self._dead.discard(key)
        self._next_retry_at.pop(key, None)
        if len(self._order) > self._max:
            old = self._order.pop(0)
            self._events.pop(old, None)
            self._attempts.pop(old, None)
            self._last_error.pop(old, None)
            self._next_retry_at.pop(old, None)
            self._dead.discard(old)

    # ------------------------------------------------------------ 取出/确认

    def take(self, key: str) -> dict[str, Any] | None:
        """peek：返回暂存事件且**不删除**（旧实现恒 None 的缺陷已修）。"""
        return self._events.get(key)

    def take_ready(self, now: float | None = None) -> list[tuple[str, dict[str, Any]]]:
        """按顺序返回到达重试时间且未 dead 的事件。"""
        now = time.time() if now is None else now
        out: list[tuple[str, dict[str, Any]]] = []
        for key in list(self._order):
            if key in self._dead:
                continue
            if self._next_retry_at.get(key, 0) > now:
                continue
            payload = self._events.get(key)
            if payload is not None:
                out.append((key, payload))
        return out

    def ack(self, key: str) -> bool:
        """投递确认：删除暂存。返回是否存在。"""
        existed = self._events.pop(key, None) is not None
        if existed:
            self._acked_total += 1
            self._order.remove(key) if key in self._order else None
            self._attempts.pop(key, None)
            self._last_error.pop(key, None)
            self._next_retry_at.pop(key, None)
            self._dead.discard(key)
        return existed

    def failed(self, key: str, error: str) -> None:
        """重放失败：累计次数并安排退避；超限进入 dead（保留事件）。"""
        if key not in self._events:
            return
        self._attempts[key] += 1
        self._last_error[key] = str(error)[:200]
        if self._attempts[key] >= self._max_attempts:
            self._dead.add(key)
            return
        delay = min(self.BASE_BACKOFF_S * (2 ** (self._attempts[key] - 1)),
                    self.MAX_BACKOFF_S)
        self._next_retry_at[key] = time.time() + delay

    # ------------------------------------------------------------ 观测

    def pending(self) -> int:
        return len(self._events)

    def dead(self) -> list[str]:
        return sorted(self._dead)

    def clear(self) -> None:
        self._events.clear()
        self._order.clear()
        self._attempts.clear()
        self._last_error.clear()
        self._next_retry_at.clear()
        self._dead.clear()

    def snapshot(self) -> dict[str, Any]:
        return {
            'pending': self.pending(),
            'dead': self.dead(),
            'staged_total': self._staged_total,
            'acked_total': self._acked_total,
            'attempts': dict(self._attempts),
            'last_errors': dict(self._last_error),
        }
