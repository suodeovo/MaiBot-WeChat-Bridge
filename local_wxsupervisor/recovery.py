"""失败重放与断线补收（WX-006 / 返修 R3-R6 / V2-4 V2-5 V2-9）。

## 确认事务（V2-4）

事件生命周期：pending（已暂存未投递）→ in-flight（投递中）→
confirmed（Host accepted）/ failed（重试）。

* **去重标记只在 confirmed 时写入** —— pending/in-flight 状态绝不
  写成 received。投递被取消（CancelledError）/RPC 响应丢失 → 事件
  回到 pending，重建后恢复重放；去重表不残留假阳性。
* 暂存、去重、重放键与状态目录统一携带**来源账号**（V2-9）：
  `stage_unconfirmed` 记录 `origin_account`；当前账号与来源账号不一致
  时事件隔离保留（不重归属、不投递），日志明确暴露。
* 实时、补收、重放共用 `confirm_delivery()`：确认 = dedup.mark +
  pending ack + 水位尝试推进（V2-5 最早未确认边界）。

## 最早未确认边界（V2-5）

`on_message_processed` 不再无条件推进水位：只有当该会话没有更早的
pending 事件（按会话内未确认集合的最小时间戳判断）时才推进到本条；
否则水位保持，等缺口确认后由 `advance_watermark_to()` 推进。

## 补收分页进展（V2-5）

分页进展 = 页**开始游标**与页**结束游标**的完整键比较（含响应 cursor）；
重复行/过滤行也推进扫描游标；全部重复的页不再阻断下一页。
补收成功路径显式 `confirm_delivery` → dedup + watermark。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger('wxsupervisor.recovery')

OVERLAP_S = 5.0
MAX_PAGES = 20
DEDUP_TTL_S = 24 * 3600.0


def fetch_messages(base: str, token: str, talker: str, limit: int,
                   from_time: int | None = None, timeout: float = 20.0
                   ) -> list[dict[str, Any]]:
    """reader /api/v1/messages（旧契约，兼容入口）。"""
    params: dict[str, Any] = {'talker': talker, 'limit': int(limit)}
    if from_time is not None:
        params['from_time'] = int(from_time)
    url = f'{base.rstrip("/")}/api/v1/messages?{urllib.parse.urlencode(params)}'
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    return list(data.get('messages') or [])


def fetch_messages_page(base: str, token: str, talker: str, limit: int,
                        after_time: int | None = None, after_id: str = '',
                        timeout: float = 20.0,
                        after_local_id: int | None = None
                        ) -> dict[str, Any]:
    """reader 游标契约（V2-1/G4 修复后的 reader 补丁）。

    after_local_id：完整排序键 (createTime, localId) 的后半部分，
    锚点行不在窗口时仍能精确比较同秒邻居。
    """
    params: dict[str, Any] = {'talker': talker, 'limit': int(limit)}
    if after_time is not None:
        params['after_time'] = int(after_time)
    if after_id:
        params['after_id'] = str(after_id)
    if after_local_id is not None:
        params['after_local_id'] = int(after_local_id)
    url = f'{base.rstrip("/")}/api/v1/messages?{urllib.parse.urlencode(params)}'
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    return {
        'messages': list(data.get('messages') or []),
        'cursor': data.get('cursor'),
    }


def _row_order_key(row: dict[str, Any]) -> tuple[float, int]:
    try:
        ts = float(row.get('createTime') or 0)
    except (TypeError, ValueError):
        ts = 0.0
    try:
        lid = int(row.get('localId') or 0)
    except (TypeError, ValueError):
        lid = 0
    return ts, lid


class ReceiveDedup:
    """R6/V2-4：接收去重（账号+会话+rawid 作用域，持久化）。

    V2-4：只保存 confirmed 状态 —— pending/in-flight 绝不写入。
    """

    def __init__(self, path: Path, ttl_s: float = DEDUP_TTL_S,
                 max_entries: int = 20000) -> None:
        self.path = Path(path)
        self.ttl_s = float(ttl_s)
        self.max_entries = int(max_entries)
        self._seen: dict[tuple[str, str, str], float] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding='utf-8-sig'))
                now = time.monotonic()
                for item in raw or []:
                    key = (item.get('account', ''), item.get('session', ''),
                           item.get('rawid', ''))
                    self._seen[key] = now - float(item.get('age', 0))
        except Exception as exc:
            log.warning('接收去重文件读取失败，按空启动: %s', exc)
            self._seen = {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            now = time.monotonic()
            payload = [{'account': k[0], 'session': k[1], 'rawid': k[2],
                        'age': now - v}
                       for k, v in list(self._seen.items())[-self.max_entries:]]
            tmp = self.path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(payload), encoding='utf-8')
            tmp.replace(self.path)
        except Exception as exc:
            log.warning('接收去重写盘失败: %s', exc)

    def seen_before(self, account: str, session: str, rawid: str) -> bool:
        key = (account, session, rawid)
        now = time.monotonic()
        expired = [k for k, at in self._seen.items() if now - at > self.ttl_s]
        for k in expired:
            self._seen.pop(k, None)
        at = self._seen.get(key)
        return at is not None and now - at <= self.ttl_s

    def mark(self, account: str, session: str, rawid: str) -> None:
        self._seen[(account, session, rawid)] = time.monotonic()
        if len(self._seen) > self.max_entries:
            for k in sorted(self._seen, key=self._seen.get)[:len(self._seen) // 4]:
                self._seen.pop(k, None)
        self.save()

    def unmark(self, account: str, session: str, rawid: str) -> None:
        self._seen.pop((account, session, rawid), None)
        self.save()


class WatermarkStore:
    """接收水位（confirmed 边界）：{account: {session: {'rawid','ts'}}}。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                self._data = json.loads(
                    self.path.read_text(encoding='utf-8-sig'))
        except Exception as exc:
            log.warning('水位文件读取失败，按空启动: %s', exc)
            self._data = {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1),
                           encoding='utf-8')
            tmp.replace(self.path)
        except Exception as exc:
            log.warning('水位写盘失败: %s', exc)

    def get(self, account: str, session: str) -> dict[str, Any] | None:
        return self._data.get(account, {}).get(session)

    def update(self, account: str, session: str, rawid: str, ts: float) -> None:
        self._data.setdefault(account, {})
        current = self._data[account].get(session)
        if current is None or float(ts) >= float(current.get('ts') or 0):
            self._data[account][session] = {'rawid': rawid, 'ts': float(ts)}
            self.save()

    def sessions(self, account: str) -> dict[str, dict[str, Any]]:
        return self._data.get(account, {})


class ReplayPersistence:
    """V2-4/V2-9：未确认事件持久化 —— 含来源账号与会话身份。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> list[dict[str, Any]]:
        try:
            if self.path.exists():
                return list(json.loads(self.path.read_text(encoding='utf-8-sig')))
        except Exception as exc:
            log.warning('未确认事件文件读取失败: %s', exc)
        return []

    def save(self, events: list[dict[str, Any]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(events, ensure_ascii=False),
                           encoding='utf-8')
            tmp.replace(self.path)
        except Exception as exc:
            log.warning('未确认事件写盘失败: %s', exc)


class RecoveryManager:
    """重放 worker + 补收。插件 on_load 构建、start()、on_unload stop()。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._task: asyncio.Task[Any] | None = None
        self._stopped = asyncio.Event()
        self._inflight: set[str] = set()
        state_dir = Path(str(plugin.cfg.get('project_root') or '')) / 'state' / 'wxsupervisor'
        self.watermarks = WatermarkStore(state_dir / 'recovery-state.json')
        self.dedup = ReceiveDedup(state_dir / 'receive-dedup.json')
        self.replay_store = ReplayPersistence(state_dir / 'replay-pending.json')
        self._account_override: str = ''
        self.gaps: list[dict[str, Any]] = []
        # G2：会话内未确认事件边界 —— 基于全部 pending 的时间戳集合派生
        # （确认后重算），不再只存 min_ts 标志。
        # {account: {session: sorted [ts, ...]}}
        self._pending_ts: dict[str, dict[str, list[float]]] = {}
        # 会话内已确认的最新时间戳（缺口清空后水位推进目标）
        self._confirmed_latest: dict[str, dict[str, float]] = {}
        # G1：来源账号映射为实例数据（进程重建从磁盘完整恢复）
        self._event_accounts: dict[str, str] = {}
        self._restore_pending()

    # ------------------------------------------------------------ 账号（V2-9）

    async def _current_account(self) -> str:
        if self._account_override:
            return self._account_override
        try:
            st = await self.plugin._loop.run_in_executor(
                None, self.plugin.reader.status)
            return str(st.get('ownWxid') or '')
        except Exception:
            return ''

    # ------------------------------------------------------------ 确认事务（V2-4）

    def _restore_pending(self) -> None:
        """G1：进程重建 —— 完整读回未确认事件、来源账号与会话身份。

        来源缺失的旧记录（无 origin_account 的历史文件）保持 pending 但
        不归属到当前账号：重放时隔离处理，不猜账号。
        """
        events = self.replay_store.load()
        for item in events:
            key = str(item.get('key') or '')
            payload = item.get('payload')
            origin = str(item.get('origin_account') or '')
            if key and payload is not None:
                self.plugin.replay.stage(key, payload)
                # G1：实例级映射完整恢复（含空串占位，防止回退当前账号）
                self._event_accounts[key] = origin
                try:
                    ts = float(payload.get('timestamp') or 0)
                    session = str(payload.get('sessionId') or '')
                    if origin and session and ts > 0:
                        self._add_pending_ts(origin, session, ts)
                except (TypeError, ValueError):
                    pass
        if events:
            log.info('恢复 %d 条未确认事件（含来源账号）继续重放', len(events))

    def _add_pending_ts(self, account: str, session: str, ts: float) -> None:
        bucket = self._pending_ts.setdefault(account, {})
        bucket.setdefault(session, [])
        if ts not in bucket[session]:
            bucket[session].append(ts)
            bucket[session].sort()

    def _remove_pending_ts(self, account: str, session: str, ts: float) -> None:
        bucket = self._pending_ts.get(account, {})
        ts_list = bucket.get(session)
        if ts_list and ts in ts_list:
            ts_list.remove(ts)
            if not ts_list:
                bucket[session] = []

    def _persist_pending(self) -> None:
        events = []
        for key in list(self.plugin.replay._order):
            payload = self.plugin.replay.take(key)
            if payload is not None:
                origin = self._origin_account_of(key)
                events.append({'key': key, 'payload': payload,
                               'origin_account': origin})
        self.replay_store.save(events)

    def _origin_account_of(self, key: str) -> str:
        """V2-9：事件来源账号（暂存时绑定）。"""
        return self._event_accounts.get(key, '')

    def stage_unconfirmed(self, key: str, payload: dict[str, Any],
                          origin_account: str = '') -> None:
        """入站失败暂存 + 立即持久化，绑定来源账号（V2-4/V2-9）。"""
        self.plugin.replay.stage(key, payload)
        self._event_accounts[key] = str(origin_account or '')
        ts = float(payload.get('timestamp') or 0)
        session = str(payload.get('sessionId') or '')
        if origin_account and session and ts > 0:
            self._add_pending_ts(origin_account, session, ts)
        self._persist_pending()

    def ack_event(self, key: str, account: str = '',
                  payload: dict[str, Any] | None = None) -> None:
        """确认：删除暂存 + 按确切事件重算边界 + 持久化（G2）。"""
        removed_payload = payload if payload is not None \
            else self.plugin.replay.take(key)
        origin = self._event_accounts.get(key, '')
        self.plugin.replay.ack(key)
        self._event_accounts.pop(key, None)
        use_account = account or origin
        if use_account and removed_payload is not None:
            try:
                self._remove_pending_ts(
                    use_account,
                    str(removed_payload.get('sessionId') or ''),
                    float(removed_payload.get('timestamp') or 0))
            except (TypeError, ValueError):
                pass
        self._persist_pending()

    async def confirm_delivery(self, key: str, payload: dict[str, Any],
                               account: str) -> None:
        """V2-4/G2 统一确认事务（实时/补收/重放共用）：

        dedup.mark（confirmed）→ ack（按确切事件重算 pending 集合）→
        水位按剩余最早未确认边界推进。
        """
        session = str(payload.get('sessionId') or '')
        self.dedup.mark(account, session, key)
        self.ack_event(key, account=account, payload=payload)
        self.plugin.usage.count_recv(session, len(str(payload.get('content') or '')))
        await self._try_advance_watermark(account, session, payload)

    async def _try_advance_watermark(self, account: str, session: str,
                                     payload: dict[str, Any]) -> None:
        """G2：水位推进基于全部剩余 pending 的时间戳集合。

        本事件确认（ack 已从 _pending_ts 移除）后：
        * 若该会话仍有 pending（无论早晚）→ 水位最多推进到
          min(剩余 pending) 之前的已确认边界（confirmed_latest 与
          剩余最早 pending 的较小者之前不越）；
        * 若无剩余 pending → 推进到 confirmed_latest（可能大于本事件）。
        水位保存的 rawid 与 ts 来自同一 payload（身份一致）。
        """
        try:
            ts = float(payload.get('timestamp') or 0)
        except (TypeError, ValueError):
            return
        if ts <= 0 or not session:
            return
        latest = self._confirmed_latest.setdefault(account, {})
        if ts > latest.get(session, 0):
            latest[session] = ts

        bucket = self._pending_ts.get(account, {})
        remaining = sorted(bucket.get(session) or [])
        if remaining:
            # 仍有未确认缺口：水位不得越过最早缺口（min_ts 不含在本事件）
            boundary = min(remaining)
            target = min(latest.get(session, ts), boundary)
        else:
            target = latest.get(session, ts)
        mark = self.watermarks.get(account, session)
        if mark is None or target >= float(mark.get('ts') or 0):
            self.watermarks.update(account, session,
                                   str(payload.get('rawid') or ''), target)

    # ------------------------------------------------------------ R6：接收去重

    async def classify_inbound(self, payload: dict[str, Any]) -> str:
        """入站/补收分类（R6/V2-4）：new | duplicate。只查 confirmed 表。"""
        account = await self._current_account()
        session = str(payload.get('sessionId') or '')
        rawid = str(payload.get('rawid') or '')
        if rawid and self.dedup.seen_before(account, session, rawid):
            return 'duplicate'
        return 'new'

    def mark_received(self, account: str, payload: dict[str, Any]) -> None:
        """兼容入口：确认语义请用 confirm_delivery。"""
        self.dedup.mark(account, str(payload.get('sessionId') or ''),
                        str(payload.get('rawid') or ''))

    def unmark_received(self, account: str, payload: dict[str, Any]) -> None:
        """投递未确认时回滚去重标记（重放必须能再次投递）。"""
        self.dedup.unmark(account, str(payload.get('sessionId') or ''),
                          str(payload.get('rawid') or ''))

    # ------------------------------------------------------------ worker

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopped.clear()
            self._task = asyncio.create_task(self._run(), name='wx-recovery')

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self._replay_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug('重放循环异常: %s', exc)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=5.0)
                return
            except asyncio.TimeoutError:
                pass

    async def _replay_once(self) -> None:
        replay = self.plugin.replay
        ready = replay.take_ready()
        for key, payload in ready:
            if key in self._inflight:
                continue
            self._inflight.add(key)
            try:
                await self._redeliver(key, payload)
            finally:
                self._inflight.discard(key)

    async def _redeliver(self, key: str, payload: dict[str, Any]) -> None:
        """V2-4/V2-9：重放使用来源账号；确认走统一 confirm_delivery。"""
        origin = self._origin_account_of(key)
        account = origin or await self._current_account()
        if origin and origin != await self._current_account():
            # V2-9：账号已切换 —— 事件隔离保留，不重新归属、不投递
            log.info('暂存事件 %s 属于账号 %s，当前账号不同，隔离保留',
                     key[:12], origin)
            self.plugin.replay.failed(key, 'account-changed')
            self._persist_pending()
            return
        session_id = str(payload.get('sessionId') or '')

        if self.dedup.seen_before(account, session_id, key):
            log.info('重放事件 %s 为已确认重复，ack 丢弃', key[:12])
            self.ack_event(key, account=account, payload=payload)
            return

        decision = self.plugin.inbound.policy.allows_outbound(session_id)
        if not decision:
            log.info('重放事件 %s 策略拒绝，ack 丢弃: %s', key, decision.reason)
            self.ack_event(key, account=account, payload=payload)
            return

        try:
            accepted = await self._deliver_payload(key, payload, replay=True,
                                                   account=account)
        except asyncio.CancelledError:
            # V2-4：取消不是失败 —— 事件保持 pending，去重无残留
            raise
        if accepted is True:
            # V2-4：重放成功同样写 ReceiveDedup（统一确认事务）
            await self.confirm_delivery(key, payload, account)
        elif accepted is False:
            self.plugin.replay.failed(key, 'host-rejected')
            self._persist_pending()
        # accepted is None → 保持待重试

    async def _deliver_payload(self, key: str, payload: dict[str, Any],
                               replay: bool = False,
                               account: str = '') -> bool | None:
        """统一投递。True=accepted，False=rejected，None=异常。

        V2-9：投递使用传入的来源账号（不重读当前 reader 账号）。
        """
        gateway = getattr(getattr(self.plugin, 'ctx', None), 'gateway', None)
        if gateway is None:
            return None
        from .gateway import GATEWAY_NAME, build_message_dict
        from . import wx_message
        from .identity import is_group_session
        if not account:
            account = await self._current_account()
        session_id = str(payload.get('sessionId') or '')
        text = str(payload.get('content') or '')
        try:
            _plain, segments = wx_message.build_raw_segments(payload)
        except Exception:
            segments = None
        msg = build_message_dict(
            self_wxid=account,
            session_id=session_id,
            source_name=str(payload.get('groupName') or '') if is_group_session(session_id)
            else str(payload.get('sourceName') or ''),
            sender=str(payload.get('talkerId') or payload.get('sender') or ''),
            sender_name=str(payload.get('senderName') or ''),
            text=text,
            timestamp=payload.get('timestamp'),
            message_id=key,
            raw_segments=segments,
        )
        try:
            return bool(await gateway.route_message(
                gateway_name=GATEWAY_NAME,
                message=msg,
                route_metadata={'self_id': account, 'replay': replay},
                external_message_id=key,
                dedupe_key=key,
            ))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.plugin.replay.failed(key, str(exc))
            return None

    # ------------------------------------------------------------ R3/V2-5：水位

    def on_message_processed(self, account: str, payload: dict[str, Any]) -> None:
        """入站成功（Host accepted）后调用：确认事务（同步部分）。"""
        session = str(payload.get('sessionId') or '')
        rawid = str(payload.get('rawid') or '')
        ts = payload.get('timestamp') or 0
        if not session or not rawid:
            return
        self.dedup.mark(account, session, rawid)
        self.ack_event(rawid, account=account, payload=payload)
        # V2-5：最早未确认边界 —— 有更早 pending 时不推进
        try:
            self._try_advance_watermark_sync(account, session, float(ts), rawid)
        except Exception as exc:
            log.warning('水位推进失败: %s', exc)
        self._persist_pending()

    def _try_advance_watermark_sync(self, account: str, session: str,
                                    ts: float, rawid: str) -> None:
        """G2：实时确认路径 —— 与 async 版同一集合语义。

        调用前 ack 已把本事件从 _pending_ts 移除；剩余 pending 决定边界。
        """
        latest = self._confirmed_latest.setdefault(account, {})
        if ts > latest.get(session, 0):
            latest[session] = ts
        bucket = self._pending_ts.get(account, {})
        remaining = sorted(bucket.get(session) or [])
        if remaining:
            boundary = min(remaining)
            target = min(latest.get(session, ts), boundary)
        else:
            target = latest.get(session, ts)
        mark = self.watermarks.get(account, session)
        if mark is None or target >= float(mark.get('ts') or 0):
            self.watermarks.update(account, session, rawid, target)

    # ------------------------------------------------------------ R4/V2-5：补收

    async def backfill_gap(self, *, reason: str = 'reconnect',
                           on_delivered: Any = None) -> int:
        """按水位补收断线窗口（R4/R5/V2-5）。

        游标分页：进展 = 页开始/结束游标完整键比较；重复/过滤行也推进
        扫描；确认走 confirm_delivery（dedup+watermark）。
        """
        plugin = self.plugin
        account = await self._current_account()
        if not account:
            return 0

        base = plugin.cfg['reader_base_url']
        token = plugin._reader_token()
        total = 0
        for session, mark in list(self.watermarks.sessions(account).items()):
            decision = plugin.inbound.policy.allows_outbound(session)
            if not decision:
                continue
            ts = float(mark.get('ts') or 0)
            rawid = str(mark.get('rawid') or '')
            if ts <= 0:
                continue
            after_time = int(ts - OVERLAP_S)
            after_id = rawid if after_time <= ts else ''
            # G4：完整排序键 (ts, localId) —— 锚点缺失时同秒邻居可精确比较
            after_local_id = None
            page_start = (after_time, after_id)
            fetched = 0
            stalled = False
            for _page in range(MAX_PAGES):
                try:
                    page = await plugin._loop.run_in_executor(
                        None, fetch_messages_page, base, token, session,
                        500, after_time, after_id, after_local_id)
                except Exception as exc:
                    log.info('补收查询失败 %s: %s', session, exc)
                    self.gaps.append({'session': session, 'reason': reason,
                                      'note': f'query failed: {exc}'})
                    stalled = True
                    break
                rows = page.get('messages') or []
                if not rows:
                    break
                rows = sorted(rows, key=_row_order_key)
                for row in rows:
                    row_ts, row_lid = _row_order_key(row)
                    payload = self._row_to_payload(session, row)
                    if payload is None:
                        # 过滤行也推进扫描游标（V2-5）
                        after_time = int(row_ts)
                        after_id = ReaderRawid.of(session, row)
                        after_local_id = row_lid
                        continue
                    rkey = str(payload.get('rawid') or '')
                    if not rkey:
                        after_time = int(row_ts)
                        after_id = ReaderRawid.of(session, row)
                        after_local_id = row_lid
                        continue
                    if self.dedup.seen_before(account, session, rkey):
                        # 重复行：推进扫描游标（不重投），不确认新东西
                        after_time = int(row_ts)
                        after_id = rkey
                        after_local_id = row_lid
                        continue
                    try:
                        accepted = await self._deliver_payload(
                            rkey, payload, account=account)
                    except asyncio.CancelledError:
                        raise
                    if accepted is True:
                        # V2-4：统一确认事务（dedup + watermark）
                        await self.confirm_delivery(rkey, payload, account)
                        fetched += 1
                        if on_delivered is not None:
                            try:
                                on_delivered(payload)
                            except Exception:
                                pass
                        after_time = int(row_ts)
                        after_id = rkey
                        after_local_id = row_lid
                    elif accepted is False:
                        # V2-5：未确认 → 记 pending + 停止（不越过缺口），
                        # 但扫描游标仍推进（该行已尝试，重复补收由去重兜底）
                        self.stage_unconfirmed(rkey, payload,
                                               origin_account=account)
                        stalled = True
                        break
                    else:
                        stalled = True
                        break
                # V2-5：分页进展 = 页结束游标与页开始游标的完整比较
                if stalled:
                    break
                page_end = (after_time, after_id)
                cursor = page.get('cursor')
                if cursor:
                    cur_key = (int(cursor.get('after_time') or 0),
                               str(cursor.get('after_id') or ''))
                    if cur_key > page_end:
                        after_time = int(cursor.get('after_time') or after_time)
                        after_id = str(cursor.get('after_id') or after_id)
                        after_local_id = int(
                            cursor.get('after_local_id') or after_local_id or 0)
                if page_end <= page_start and not cursor:
                    # 无任何进展（空窗/全被边界吞掉）→ 防死循环退出
                    break
                if page_end > page_start:
                    page_start = page_end
                if not rows or (cursor is None and len(rows) < 500):
                    break
            if stalled:
                self.gaps.append({'session': session, 'reason': reason,
                                  'fetched': fetched,
                                  'note': 'delivery pending; watermark held at confirmed boundary'})
            total += fetched
        if total:
            log.info('断线补收(%s): %d 条', reason, total)
        return total

    def _row_to_payload(self, session: str, row: dict[str, Any]) -> dict[str, Any] | None:
        from .history_backfill import payload_from_reader_row
        return payload_from_reader_row(session, row)

    def snapshot(self) -> dict[str, Any]:
        return {
            'watermark_sessions': sum(len(v) for v in self.watermarks._data.values()),
            'gaps': list(self.gaps[-20:]),
            'dedup_entries': len(self.dedup._seen),
            'earliest_unconfirmed': {
                a: dict(s) for a, s in self._earliest_unconfirmed.items()},
            'replay': self.plugin.replay.snapshot(),
        }


class ReaderRawid:
    """reader 行 rawid（与 push_payload 同算法），供扫描游标使用。"""

    @staticmethod
    def of(talker: str, row: dict[str, Any]) -> str:
        import hashlib
        digest = json.dumps([talker, row.get('serverId'), row.get('localId'),
                             row.get('createTime')], separators=(',', ':'))
        return hashlib.sha256(digest.encode()).hexdigest()
