"""reader SSE 消费端（WX-005 + 返修 R1）。

reader 推送端点：
    GET http://127.0.0.1:5031/api/v1/push/messages
    Authorization: Bearer <reader_token>
    Content-Type: text/event-stream

R1：`_run()` 统一从 `asyncio.get_running_loop()` 取 loop（实例字段
`_loop` 仅作 stop() 的跨线程回调目标），真实 start → _run → 流读取 →
回调 → stop 全链路可用。

有界通道：读取线程写有界 queue.Queue（阻塞 put），事件循环 pump
搬运到有界 asyncio.Queue —— 总积压 = queue_size + thread_channel_size。
停止：设事件 → 关响应解锁阻塞读 → 取消任务（3s shield 等待）→
关线程池。统一重连退避只在 _run 内部做；on_reconnected 供补收挂钩。
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue as thread_queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

log = logging.getLogger('wxsupervisor.sse')

HEARTBEAT_PREFIX = ':'


def to_onebot_event(payload: dict[str, Any], self_id: int,
                    id_of: Callable[[str, str], int]) -> dict[str, Any] | None:
    """reader 推送 -> OneBot v11 message 事件。

    发言人用 talkerId（缺失回退 sender），群名用 groupName；
    sourceName 群聊时是发言人显示名，不当群名。
    """
    if not isinstance(payload, dict):
        return None

    session_id = str(payload.get('sessionId') or '')
    content = payload.get('content')
    if not session_id or content is None:
        return None

    session_type = str(payload.get('sessionType') or '').lower()
    is_group = session_type == 'group' or session_id.endswith('@chatroom')
    source_name = str(payload.get('sourceName') or '')
    group_name = str(payload.get('groupName') or '')
    sender = str(payload.get('talkerId') or payload.get('sender') or '')
    sender_name = str(payload.get('senderName') or '') or source_name
    text = str(content)

    ts = payload.get('timestamp')
    try:
        ts = int(float(ts)) if ts is not None else int(time.time())
    except (TypeError, ValueError):
        ts = int(time.time())

    event: dict[str, Any] = {
        'time': ts,
        'self_id': self_id,
        'post_type': 'message',
        'message_type': 'group' if is_group else 'private',
        'sub_type': 'normal',
        'message_id': id_of(f"{session_id}:{payload.get('rawid') or ts}", ''),
        'user_id': id_of(sender or session_id, sender_name),
        'message': [{'type': 'text', 'data': {'text': text}}],
        'raw_message': text,
        'font': 0,
        'sender': {'user_id': id_of(sender or session_id, sender_name),
                   'nickname': sender_name},
    }

    if is_group:
        event['group_id'] = id_of(session_id, group_name or source_name)
        event['group_name'] = group_name or source_name
    else:
        event['target_id'] = id_of(session_id, source_name)

    return event


class SseConsumer:
    """持续消费 reader 的 SSE，回调 on_event。统一重连退避。"""

    def __init__(self, base_url: str, token: str,
                 on_event: Callable[[dict[str, Any]], Any],
                 *, queue_size: int = 512,
                 thread_channel_size: int = 8,
                 max_backoff_s: float = 15.0,
                 open_timeout_s: float = 35.0) -> None:
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.on_event = on_event
        self.queue_size = max(1, int(queue_size))
        self._thread_channel_size = max(1, int(thread_channel_size))
        self._max_backoff_s = float(max_backoff_s)
        self._open_timeout_s = float(open_timeout_s)

        self._task: asyncio.Task[Any] | None = None
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.connected = False
        self.received = 0
        self.last_error = ''
        self.gave_up = False
        self.on_reconnected: Callable[[], Any] | None = None
        self._thread_channel: thread_queue.Queue[Any] | None = None
        self._response: Any = None
        self._executor: ThreadPoolExecutor | None = None
        self._pump_error: BaseException | None = None
        self.event_concurrency: int = 16
        self._inflight_events: set[asyncio.Task[Any]] = set()
        # V2-2b：会话级有序投递锁
        self._session_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self.gave_up = False
            self._loop = asyncio.get_running_loop()   # R1：stop 回调目标
            self._task = asyncio.create_task(self._run(), name='wx-sse-consumer')

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def is_active(self) -> bool:
        # G3: lifecycle state used by config rollback; False after stop()
        return self.running()

    async def stop(self) -> None:
        """可中断停止。读取线程阻塞在线程里不可取消：设事件 + 关响应
        解锁，任务带 3s shield 超时等待；线程池 shutdown 不等待。"""
        self._stop.set()
        self._close_response()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        self._drain_thread_channel()
        self._shutdown_executor()
        self.connected = False

    def _close_response(self) -> None:
        resp = self._response
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
            self._response = None

    def _drain_thread_channel(self) -> None:
        ch = self._thread_channel
        if ch is None:
            return
        try:
            while True:
                ch.get_nowait()
        except thread_queue.Empty:
            pass

    def _get_executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=2, thread_name_prefix='wx-sse-reader')
        return self._executor

    def _shutdown_executor(self) -> None:
        ex = self._executor
        self._executor = None
        if ex is not None:
            ex.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------ 主循环（R1）

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()      # R1：每次循环显式取得 loop
        backoff = 2.0
        while not self._stop.is_set():
            channel: thread_queue.Queue[Any] = thread_queue.Queue(
                maxsize=self._thread_channel_size)
            self._thread_channel = channel
            pump_done = asyncio.Event()
            self._pump_error = None
            pump_task = asyncio.create_task(self._pump(channel, pump_done))
            try:
                await loop.run_in_executor(self._get_executor(),
                                           self._stream_once, channel)
                self.last_error = ''
            except asyncio.CancelledError:
                self._close_response()
                pump_task.cancel()
                raise
            except Exception as exc:
                self.connected = False
                self.last_error = f'{type(exc).__name__}: {exc}'
                log.warning('SSE 断开，%.1fs 后重连: %s', backoff, exc)
            finally:
                if self._stop.is_set():
                    self._offer_sentinel_nonblocking(channel)
                try:
                    await asyncio.wait_for(pump_done.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    pump_task.cancel()
                try:
                    await pump_task
                except (asyncio.CancelledError, Exception):
                    pass

            if self._stop.is_set():
                return
            # R1：通道上传来的异常（urlopen 失败等）→ 计入 last_error 触发重连
            if self._pump_error is not None:
                exc = self._pump_error
                self._pump_error = None
                self.connected = False
                self.last_error = f'{type(exc).__name__}: {exc}'
                log.warning('SSE 断开，%.1fs 后重连: %s', backoff, exc)
            woke = await self._sleep_interruptible(backoff)
            if woke:
                return
            backoff = min(backoff * 1.5, self._max_backoff_s)

    async def _sleep_interruptible(self, seconds: float) -> bool:
        """等待，返回 True 表示被 stop 唤醒。"""
        try:
            await asyncio.wait_for(self._wait_stop_event(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _wait_stop_event(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(0.05)

    # ------------------------------------------------------------ 读取线程

    def _stream_once(self, channel: thread_queue.Queue[Any]) -> None:
        """在线程里做流式读；数据经有界 channel 交给事件循环。

        channel 条目：(kind, payload)，kind ∈ data|eof|exc|stop。
        put 阻塞：channel 满时读取线程等待，积压受 maxsize 约束。
        """
        import urllib.request
        url = self.base_url + '/api/v1/push/messages'
        req = urllib.request.Request(url, headers={
            'Authorization': 'Bearer ' + self.token,
            'Accept': 'text/event-stream',
            'Cache-Control': 'no-cache',
        })
        resp = None
        try:
            resp = urllib.request.urlopen(req, timeout=self._open_timeout_s)
            self._response = resp
            ctype = resp.headers.get('Content-Type', '')
            if 'text/event-stream' not in ctype:
                raise RuntimeError(f'unexpected content-type: {ctype}')
            self._signal_connected()
            for raw in resp:
                if self._stop.is_set():
                    break
                line = raw.decode('utf-8', 'replace').strip()
                if not line or line.startswith(HEARTBEAT_PREFIX):
                    continue
                if line.startswith('data:'):
                    payload = line[5:].strip()
                    if payload:
                        channel.put(('data', payload))
            channel.put(('eof', None))
        except Exception as exc:
            if not self._stop.is_set():
                channel.put(('exc', exc))
        finally:
            self._response = None
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass

    def _offer_sentinel_nonblocking(self, channel: thread_queue.Queue[Any]) -> None:
        try:
            channel.put_nowait(('stop', None))
        except thread_queue.Full:
            pass

    def _signal_connected(self) -> None:
        was_connected = self.connected
        self.connected = True
        self.last_error = ''
        if not was_connected:
            log.info('SSE 已连接到 reader')
            cb = self.on_reconnected
            if cb is not None and self._loop is not None and self._loop.is_running():
                try:
                    asyncio.run_coroutine_threadsafe(self._fire_reconnected(),
                                                     self._loop)
                except RuntimeError:
                    pass

    async def _run_on_event(self, data: dict[str, Any]) -> None:
        """单条事件处理：按规范化会话串行（V2-2b 同会话保序），
        不同会话并行；on_event 异常记日志不杀消费链。"""
        session = ''
        if isinstance(data, dict):
            session = str(data.get('sessionId') or '')
        try:
            if session:
                lock = self._session_lock(session)
                async with lock:
                    result = self.on_event(data)
                    if asyncio.iscoroutine(result):
                        await result
            else:
                result = self.on_event(data)
                if asyncio.iscoroutine(result):
                    await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('处理消息失败: %s', exc)

    def _session_lock(self, session: str) -> asyncio.Lock:
        """V2-2b：会话级锁（完整投递保序，不止媒体解码）。"""
        lock = self._session_locks.get(session)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session] = lock
            if len(self._session_locks) > 256:
                # 防膨胀：清理最旧的一半（dict 保插入序）
                for key in list(self._session_locks)[:128]:
                    self._session_locks.pop(key, None)
        return lock

    async def _fire_reconnected(self) -> None:
        try:
            result = self.on_reconnected()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            log.warning('SSE 重连回调失败: %s', exc)

    # ------------------------------------------------------------ pump

    async def _pump(self, channel: thread_queue.Queue[Any],
                    done: asyncio.Event) -> None:
        """线程 channel -> 事件循环队列 -> on_event。

        积压上限 = queue_size + thread_channel_size。坏 JSON 丢弃；
        on_event 异常记日志不杀消费链。
        """
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=self.queue_size)

        async def feeder() -> None:
            while not (self._stop.is_set() and channel.empty()):
                try:
                    kind, item = await loop.run_in_executor(
                        self._get_executor(), channel.get, True, 0.5)
                except thread_queue.Empty:
                    continue
                except Exception:
                    break
                await q.put((kind, item))
                if kind in ('eof', 'exc', 'stop'):
                    break

        feeder_task = asyncio.create_task(feeder())

        try:
            while True:
                try:
                    kind, item = await asyncio.wait_for(q.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    if self._stop.is_set() and channel.empty() and q.empty():
                        break
                    continue
                if kind in ('stop', 'eof'):
                    break
                if kind == 'exc':
                    self._pump_error = item
                    break
                try:
                    data = json.loads(item)
                except json.JSONDecodeError:
                    continue
                self.received += 1
                # 复核（WX-009/V2-2）：on_event 在独立任务中执行；
                # 有界并发由 inflight 上限约束（变量名不与 done Event 冲突）。
                while len(self._inflight_events) >= self.event_concurrency:
                    completed, _pending = await asyncio.wait(
                        self._inflight_events, return_when=asyncio.FIRST_COMPLETED)
                    for t in completed:
                        self._inflight_events.discard(t)
                        if t.exception() and not isinstance(
                                t.exception(), asyncio.CancelledError):
                            log.warning('处理消息失败: %s', t.exception())
                task = asyncio.create_task(self._run_on_event(data))
                self._inflight_events.add(task)
        finally:
            feeder_task.cancel()
            try:
                await feeder_task
            except (asyncio.CancelledError, Exception):
                pass

            if self._inflight_events:
                finished, _pending = await asyncio.wait(
                    set(self._inflight_events), timeout=5)
                for t in finished:
                    if t.done() and t.exception() and not isinstance(
                            t.exception(), asyncio.CancelledError):
                        log.warning('处理消息失败: %s', t.exception())
                self._inflight_events.clear()
            self.connected = False
            done.set()

    # ------------------------------------------------------------ 测试钩子

    async def _stream_once_test(self, fake_stream: Any, collect=None) -> None:
        """测试入口：注入假流对象，走真实 pump 路径（无网络）。"""
        channel: thread_queue.Queue[Any] = thread_queue.Queue(
            maxsize=self._thread_channel_size)
        done = asyncio.Event()

        def fake_thread() -> None:
            try:
                for raw in fake_stream:
                    if self._stop.is_set():
                        break
                    line = raw.decode('utf-8', 'replace').strip()
                    if not line or line.startswith(':'):
                        continue
                    if line.startswith('data:'):
                        channel.put(('data', line[5:].strip()))
                channel.put(('eof', None))
            except Exception as exc:
                channel.put(('exc', exc))

        thread = threading.Thread(target=fake_thread, daemon=True)
        thread.start()
        pump_task = asyncio.create_task(self._pump(channel, done))
        await asyncio.wait_for(done.wait(), timeout=10.0)
        thread.join(timeout=2.0)
