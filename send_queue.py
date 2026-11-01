"""统一出站发送调度（WX-004 / 返修 R2 R7 R11）。

设计：

* 所有实际发送入口（MaiBot 网关、外部 OneBot、TTS）在发送前必须先
  `acquire()` 拿到许可（permit）。额度在**实际发送前**原子预留。
* **R2**：permit 由调用方通过 `finish(permit=...)` 归还，`finish` 幂等
  （内部 once 标志），成功/拒绝/异常/取消/部分成功都恰好收尾一次；
  兜底 `release_permit()` 供异常路径。in_flight 严格配对。
* **R7**：幂等键 = (account, target, request_id)。命中已提交条目时
  返回 `DedupHit`（携带原结果）；首个请求在途时第二个请求等待其
  Future 完成（并发重复），不返回限流失败；幂等记录不因重复查询
  被覆盖。覆盖顺序重复、并发重复、结果未知、部分成功。
* **R11**：额度按**最终发送计划**计：文本段非空计 1；语音（TTS）计 1；
  兜底文字（want_voice 且 want_text=False 且 fallback 开）计 1；媒体逐段计。
  纯图片 = 1 条额度，不凭空多算文本。
* 时钟统一用注入的单调时钟。轮次语义保留：一轮 = 相邻间隔 <
  turn_quiet_ms 且总时长 < round_window_ms；轮内 round_min_gap_ms；
  跨轮 min_interval_ms。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger('wxsupervisor.send_queue')


class SendStatus(str, Enum):
    UNATTEMPTED = 'unattempted'
    REJECTED = 'rejected'
    SUBMITTED = 'submitted'          # 已提交（ret=0），未确认送达
    CONFIRMED = 'confirmed'          # 有回执确认送达
    UNKNOWN = 'unknown'              # 超时/中断，结果未知
    PARTIAL = 'partial'              # 部分成功（多段）
    FAILED = 'failed'                # 明确失败


@dataclass
class SendRequest:
    target: str
    chat_type: str
    account: str = ''
    native_count: int = 1            # 按最终发送计划的原生发送条数（R11）
    request_id: str = ''


@dataclass
class Permit:
    """acquire 成功后持有的发送许可。finish 恰好一次（R2）。"""
    request: SendRequest
    reserved: int
    acquired_at: float
    reserved_entries: list[list[float]] = field(default_factory=list)
    _finished: bool = False

    @property
    def finished(self) -> bool:
        return self._finished


@dataclass
class DedupHit:
    """R7：幂等命中。result 是首个请求的最终网关返回（或未知态快照）。"""
    request_id: str
    result: dict[str, Any]
    status: SendStatus
    waited_for_inflight: bool = False


@dataclass
class _Turn:
    start: float
    last: float


@dataclass
class _IdemEntry:
    status: SendStatus
    expires_at: float
    result: dict[str, Any] | None
    future: asyncio.Future[Any] | None = None   # 在途请求的完成信号（R7）


class _MonotonicClock:
    def monotonic(self) -> float:
        return time.monotonic()


class SendScheduler:
    """账号级全局额度 + 会话级间隔/轮次，原子预留，幂等在途等待。"""

    def __init__(self, plugin: Any, clock: Any | None = None,
                 max_concurrent: int | None = None) -> None:
        self.plugin = plugin
        self.clock = clock or _MonotonicClock()
        self._lock = asyncio.Lock()
        self.history_entries: list[list[float]] = []
        self.turns: dict[str, _Turn] = {}
        # (account, target, request_id) -> entry
        self._idempotency: dict[tuple[str, str, str], _IdemEntry] = {}
        self._idempotency_ttl = 600.0     # 覆盖宿主超时重试窗口
        self._in_flight = 0
        self._max_concurrent_override = max_concurrent
        self.last_results: dict[str, SendStatus] = {}

    @staticmethod
    def default_clock() -> _MonotonicClock:
        return _MonotonicClock()

    # ------------------------------------------------------------ 配置

    def _cfg(self, key: str, default: int) -> int:
        value = (self.plugin.cfg or {}).get(key)
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @property
    def max_concurrent(self) -> int:
        if self._max_concurrent_override:
            return int(self._max_concurrent_override)
        return max(1, self._cfg('max_concurrent_sends', 4))

    # ------------------------------------------------------------ 幂等（R7）

    @staticmethod
    def idem_key(request: SendRequest) -> tuple[str, str, str]:
        return (str(request.account or ''), str(request.target or ''),
                str(request.request_id or ''))

    def _gc_idempotency(self) -> None:
        now = self.clock.monotonic()
        expired = [k for k, e in self._idempotency.items() if e.expires_at <= now]
        for key in expired:
            self._idempotency.pop(key, None)

    def _lookup_idempotent(self, request: SendRequest
                           ) -> tuple[bool, dict[str, Any] | None, asyncio.Future[Any] | None]:
        if not request.request_id:
            return False, None, None
        self._gc_idempotency()
        entry = self._idempotency.get(self.idem_key(request))
        if entry is None:
            return False, None, None
        if entry.future is not None and not entry.future.done():
            # 在途：返回 future 供调用方等待首个请求完成
            return True, None, entry.future
        if entry.status in (SendStatus.SUBMITTED, SendStatus.CONFIRMED,
                            SendStatus.PARTIAL):
            result = entry.result if entry.result is not None else \
                {'success': True, 'metadata': {'dedup': True}}
            return True, result, None
        if entry.status == SendStatus.UNKNOWN:
            # V2-7：结果未知 —— 原生调用可能已提交。不能自动重发；
            # 返回未知快照，由调用方决定（等待回执/人工介入）。
            result = entry.result if entry.result is not None else \
                {'success': False, 'error': 'result unknown (native call may have committed); '
                                            'not auto-retrying to avoid duplicate send',
                 'metadata': {'dedup': True, 'delivery': 'unknown'}}
            return True, result, None
        # FAILED/REJECTED：允许重新尝试（明确失败，原生未提交）
        return False, None, None

    def remember_result(self, request: SendRequest, status: SendStatus,
                        result: dict[str, Any] | None = None) -> None:
        if not request.request_id:
            return
        key = self.idem_key(request)
        entry = self._idempotency.get(key)
        if entry is None:
            entry = _IdemEntry(status=status,
                               expires_at=self.clock.monotonic() + self._idempotency_ttl,
                               result=result)
            self._idempotency[key] = entry
        else:
            # R7/V2-7：不因重复查询覆盖已有终结状态（SUBMITTED/UNKNOWN
            # 不被 REJECTED/FAILED 覆盖 —— UNKNOWN 重试会造成重复提交）
            terminal = (SendStatus.SUBMITTED, SendStatus.CONFIRMED,
                        SendStatus.PARTIAL, SendStatus.UNKNOWN)
            if entry.status in terminal and status in (
                    SendStatus.REJECTED, SendStatus.FAILED):
                log.info('幂等条目保持 %s，忽略 %s 覆盖 (key=%s)',
                         entry.status.value, status.value, request.request_id)
                return
            entry.status = status
            if result is not None:
                entry.result = result
            entry.expires_at = self.clock.monotonic() + self._idempotency_ttl
        fut = entry.future
        if fut is not None and not fut.done():
            fut.set_result(result if result is not None
                           else {'success': status in (SendStatus.SUBMITTED,
                                                       SendStatus.CONFIRMED,
                                                       SendStatus.PARTIAL)})
            entry.future = None
        self.last_results[request.request_id] = status

    def mark_unknown(self, request: SendRequest,
                     reason: str = 'native response lost') -> None:
        """V2-7：原生调用可能已提交、响应丢失 → 结果未知。

        UNKNOWN 不允许同 request_id 自动重试；可用 resolve_unknown()
        在回执到达后恢复真实状态。
        """
        self.remember_result(request, SendStatus.UNKNOWN,
                             {'success': False, 'error': reason,
                              'metadata': {'delivery': 'unknown'}})

    def resolve_unknown(self, request_id: str, confirmed: bool,
                        result: dict[str, Any] | None = None) -> bool:
        """V2-7：通过回执/查询恢复 UNKNOWN 请求的真实状态。

        找不到该 key 返回 False；找到则更新为 CONFIRMED/SUBMITTED（确认）
        或 FAILED（确认未提交，此后允许重试）。
        """
        for key, entry in self._idempotency.items():
            if key[2] == str(request_id or ''):
                if confirmed:
                    entry.status = SendStatus.CONFIRMED if confirmed else SendStatus.SUBMITTED
                else:
                    entry.status = SendStatus.FAILED
                if result is not None:
                    entry.result = result
                return True
        return False

    # ------------------------------------------------------------ 额度

    def _prune(self, now: float) -> None:
        horizon = now - 660.0
        if self.history_entries and self.history_entries[0][0] < horizon:
            self.history_entries = [e for e in self.history_entries if e[0] >= horizon]

    def _counts(self, now: float) -> tuple[int, int]:
        per_min = sum(int(n) for t, n in self.history_entries if now - t < 60)
        ten_min = sum(int(n) for t, n in self.history_entries if now - t < 600)
        return per_min, ten_min

    def _session_gap_ms(self, chat_type: str) -> int:
        cfg = self.plugin.cfg or {}
        if chat_type == 'group':
            return int(cfg.get('min_interval_ms') or 15000)
        return int(cfg.get('private_min_interval_ms') or 1000)

    def _turn_quiet_ms(self) -> int:
        return self._cfg('turn_quiet_ms', 10000)

    def _round_window_ms(self) -> int:
        return self._cfg('round_window_ms', 30000)

    def _round_min_gap_ms(self) -> int:
        return self._cfg('round_min_gap_ms', 200)

    def in_turn(self, target: str, now: float | None = None) -> bool:
        now = self.clock.monotonic() if now is None else now
        turn = self.turns.get(target)
        if turn is None:
            return False
        if (now - turn.start) * 1000 >= self._round_window_ms():
            return False
        return (now - turn.last) * 1000 < self._turn_quiet_ms()

    def probe(self, chat_type: str, target: str = '') -> tuple[float, str]:
        now = self.clock.monotonic()
        self._prune(now)
        per_min, ten_min = self._counts(now)
        if per_min >= self._cfg('max_per_minute', 20):
            return 0.0, f'每分钟上限 {self._cfg("max_per_minute", 20)} 已达'
        if ten_min >= self._cfg('max_per_ten_minutes', 100):
            return 0.0, f'每十分钟上限 {self._cfg("max_per_ten_minutes", 100)} 已达'
        events = [t for t, _n in self.history_entries]
        if not events:
            return 0.0, ''
        waited_ms = (now - events[-1]) * 1000
        if self.in_turn(target, now):
            gap = self._round_min_gap_ms()
            if waited_ms >= gap:
                return 0.0, ''
            return (gap - waited_ms) / 1000.0, ''
        gap = self._session_gap_ms(chat_type)
        if waited_ms >= gap:
            return 0.0, ''
        need = (gap - waited_ms) / 1000.0
        max_wait = self._cfg('max_wait_ms', 8000) / 1000.0
        if need > max_wait:
            return 0.0, (f'间隔不足（需要 {gap}ms，已等 {int(waited_ms)}ms，'
                         f'超出可等待上限 {int(max_wait * 1000)}ms）')
        return need, ''

    # ------------------------------------------------------------ 预留/完成

    async def acquire(self, request: SendRequest,
                      max_wait_s: float = 8.0
                      ) -> tuple[Permit | DedupHit | None, str]:
        """原子预留额度。

        返回：
          (Permit, '')            —— 可发送
          (DedupHit, '')          —— R7 幂等命中（含原结果或等待后结果）
          (None, 原因)            —— 明确拒绝
        """
        hit, cached, future = self._lookup_idempotent(request)
        if hit:
            if future is not None:
                # 并发重复：等待首个请求完成（有界）
                try:
                    await asyncio.wait_for(asyncio.shield(future),
                                           timeout=max_wait_s)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
                hit2, cached, _ = self._lookup_idempotent(request)
                if not (hit2 and cached is not None):
                    entry = self._idempotency.get(self.idem_key(request))
                    cached = {'success': False,
                              'error': 'duplicate request still in flight',
                              'metadata': {'dedup': True}}
                    if entry is not None and entry.result is not None:
                        cached = entry.result
                return DedupHit(request_id=request.request_id, result=cached,
                                status=self._idempotency.get(
                                    self.idem_key(request)).status
                                if self._idempotency.get(self.idem_key(request))
                                else SendStatus.UNKNOWN,
                                waited_for_inflight=True), ''
            return DedupHit(request_id=request.request_id,
                            result=cached or {'success': True},
                            status=self._idempotency.get(
                                self.idem_key(request)).status), ''

        deadline = self.clock.monotonic() + max_wait_s
        while True:
            async with self._lock:
                now = self.clock.monotonic()
                self._prune(now)
                per_min, ten_min = self._counts(now)
                if (per_min + request.native_count > self._cfg('max_per_minute', 20)
                        or ten_min + request.native_count
                        > self._cfg('max_per_ten_minutes', 100)):
                    return None, ('额度已达上限: 每分钟 '
                                  f'{per_min}/{self._cfg("max_per_minute", 20)}，'
                                  f'十分钟 {ten_min}/{self._cfg("max_per_ten_minutes", 100)}')
                if self._in_flight >= self.max_concurrent:
                    return None, f'并发发送已达上限 {self.max_concurrent}'

                events = [t for t, _n in self.history_entries]
                wait = 0.0
                if events:
                    waited_ms = (now - events[-1]) * 1000
                    if self.in_turn(request.target, now):
                        gap = self._round_min_gap_ms()
                        if waited_ms < gap:
                            wait = (gap - waited_ms) / 1000.0
                    else:
                        gap = self._session_gap_ms(request.chat_type)
                        if waited_ms < gap:
                            wait = (gap - waited_ms) / 1000.0
                            if wait > max_wait_s:
                                return None, (f'间隔不足（需要 {gap}ms，'
                                              f'超出可等待上限 {int(max_wait_s * 1000)}ms）')
                if wait <= 0:
                    entry: list[float] = [now, float(request.native_count)]
                    self.history_entries.append(entry)
                    self._in_flight += 1
                    permit = Permit(request=request,
                                    reserved=request.native_count,
                                    acquired_at=now,
                                    reserved_entries=[entry])
                    # R7：在途占位（并发重复等待此 future）
                    if request.request_id:
                        self._idempotency[self.idem_key(request)] = _IdemEntry(
                            status=SendStatus.UNATTEMPTED,
                            expires_at=now + self._idempotency_ttl,
                            result=None,
                            future=asyncio.get_running_loop().create_future())
                    return permit, 0.0
            if self.clock.monotonic() >= deadline:
                return None, f'等待发送间隔超时（预算 {max_wait_s:.1f}s）'
            await asyncio.sleep(min(wait, 0.2))

    def finish(self, request: SendRequest, status: SendStatus,
               reason: str = '', result: dict[str, Any] | None = None,
               permit: Permit | None = None) -> None:
        """发送尝试结束：更新状态；REJECTED/取消回滚自己的预留条目。

        R2：幂等收尾 —— permit._finished 保证恰好一次；重复调用记日志忽略。
        """
        if permit is not None:
            if permit._finished:
                log.warning('permit 重复收尾被忽略: target=%s status=%s',
                            request.target, status.value)
                return
            permit._finished = True
            if status == SendStatus.REJECTED:
                self._rollback_entries(permit.reserved_entries)
            self._in_flight = max(0, self._in_flight - 1)
        log.info('发送结束 status=%s target=%s reason=%s',
                 status.value, request.target, reason)
        if status is not SendStatus.UNATTEMPTED or result is not None:
            self.remember_result(request, status, result)

    def release_permit(self, permit: Permit | None) -> None:
        """R2 兜底：异常路径强制归还并发名额（不记账、不覆盖幂等）。"""
        if permit is None or permit._finished:
            return
        permit._finished = True
        self._rollback_entries(permit.reserved_entries)
        self._in_flight = max(0, self._in_flight - 1)

    def _rollback_entries(self, entries: list[list[float]]) -> None:
        for entry in entries:
            try:
                self.history_entries.remove(entry)
            except ValueError:
                pass

    def record_native(self, target: str, chat_type: str) -> None:
        """记录一次实际原生发送（无 permit 的直发路径兜底）。"""
        now = self.clock.monotonic()
        self.history_entries.append([now, 1.0])

    def finish_turn(self, target: str, chat_type: str,
                    native_count: int = 1) -> None:
        now = self.clock.monotonic()
        turn = self.turns.get(target)
        if turn is None or (now - turn.start) * 1000 >= self._round_window_ms() \
                or (now - turn.last) * 1000 >= self._turn_quiet_ms():
            self.turns[target] = _Turn(start=now, last=now)
        else:
            turn.last = now
        if len(self.turns) > 256:
            oldest = sorted(self.turns.items(), key=lambda kv: kv[1].last)[:64]
            for key, _ in oldest:
                self.turns.pop(key, None)

    def snapshot(self) -> dict[str, Any]:
        now = self.clock.monotonic()
        per_min, ten_min = self._counts(now)
        return {
            'native_per_minute': per_min,
            'native_per_ten_minutes': ten_min,
            'in_flight': self._in_flight,
            'max_concurrent': self.max_concurrent,
        }


@dataclass
class SendPlan:
    """V2-3：发送计划一次构建，计数与执行共用。

    has_text / want_voice / want_text 语义同 _tts_plan 返回值；
    voice_request_session 是 peek 判定时用的会话键（执行 commit 时消费）。
    """
    has_text: bool
    want_voice: bool
    want_text: bool
    voice_fallback_enabled: bool
    media_count: int
    voice_request_session: str = ''

    @property
    def native_count(self) -> int:
        count = 0
        if self.has_text and self.want_text:
            count += 1
        if self.want_voice:
            count += 1
            if self.voice_fallback_enabled and not self.want_text:
                count += 1          # 语音失败时的文字兜底
        count += int(self.media_count)
        return count


def plan_native_count(*, has_text: bool, want_voice: bool, want_text: bool,
                      voice_fallback_enabled: bool, media_count: int) -> int:
    """R11：按最终发送计划计算原生发送次数（兼容入口）。

    has_text     清洗后仍有正文
    want_voice   TTS 计划要合成语音
    want_text    TTS 计划要发文本
    voice_fallback_enabled   语音失败补文字开关
    media_count  图片/表情/文件/语音段数量
    """
    plan = SendPlan(has_text=has_text, want_voice=want_voice,
                    want_text=want_text,
                    voice_fallback_enabled=voice_fallback_enabled,
                    media_count=media_count)
    return plan.native_count
