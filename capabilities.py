"""出站发送能力注册表。

## 设计

每种消息段类型对应一个 SendCapability：怎么从段里取字节、怎么落盘、
调 hook 的哪个端点、怎么判定成功。dispatcher（plugin.py）只按 type 查表，
不再维护五段 if-elif。

新增媒体类型（视频/小程序卡片/名片）= 注册一个新 capability，不动 dispatcher。

所有 capability 的 handle 都是协程，签名统一：

    async def handle(ctx: SendContext, segment: Mapping) -> None

ctx 里带 wxid / display / account / hook / cfg / loop / sent / errors，
失败用 ctx.fail(type, msg) 记录，成功用 ctx.mark(kind) 计数。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from . import media, sticker
from .perf import MediaLeaseRegistry

log = logging.getLogger('wxsupervisor.capabilities')


@dataclass
class SendContext:
    """一次出站消息的共享上下文。"""

    wxid: str
    display: str
    account: str
    hook: Any                      # HookNative
    cfg: Mapping[str, Any]
    loop: asyncio.AbstractEventLoop
    sent: dict[str, int] = field(default_factory=lambda: {
        'text': 0, 'sticker': 0, 'image': 0, 'voice': 0, 'file': 0})
    errors: list[str] = field(default_factory=list)

    def mark(self, kind: str) -> None:
        if kind in self.sent:
            self.sent[kind] += 1

    def fail(self, kind: str, msg: str) -> None:
        self.errors.append(f'{kind}: {msg}')

    async def run_hook(self, fn: Callable[..., Any], *args: Any) -> dict[str, Any]:
        """V2-7: classify native call errors. Response lost (timeout or
        connection drop) is wrapped into _NativeTimeoutError so the send
        entry records UNKNOWN instead of FAILED (no auto retry)."""
        from .plugin import _classify_native_error
        try:
            return await self.loop.run_in_executor(None, fn, *args)
        except (TimeoutError, asyncio.TimeoutError, ConnectionError,
                ConnectionResetError, ConnectionAbortedError) as exc:
            raise _classify_native_error(exc) from exc


Handler = Callable[[SendContext, Mapping[str, Any]], Awaitable[None]]


def _decode_bytes(segment: Mapping[str, Any]) -> bytes:
    """段里的字节。voice/emoji/image 都走 sticker.decode_segment_data 的多形态解析。"""
    return sticker.decode_segment_data(segment)


async def _send_with_lease(ctx: 'SendContext', path, hook_call, kind: str,
                           ok_log: str) -> dict[str, Any]:
    """V2-8：媒体发送统一租约包装。

    staging 已登记租约（引用计数）；hook 调用期间清理器看到租约不删；
    回执（ret==0，含 accepted_unconfirmed）或明确失败后释放。
    语音 accepted_unconfirmed 属异步投递 —— 释放推迟到 hook 返回，
    与现有语义一致（回执以 reader 为准，hook 已完成本地职责）。
    """
    registry = MediaLeaseRegistry.shared()
    if not registry.is_leased(path):
        registry.acquire(path)   # 兜底：staging 未登记时补
    try:
        result = await (hook_call() if callable(hook_call) and not asyncio.iscoroutine(hook_call) else hook_call)
        if int(result.get('ret', -1)) == 0:
            ctx.mark(kind)
            log.info(ok_log)
        else:
            ctx.fail(kind, str(result.get('error') or f'ret={result.get("ret")}'))
        return result
    finally:
        registry.release(path)


# ---------------------------------------------------------------- 文本

async def _handle_text(ctx: SendContext, segment: Mapping[str, Any]) -> None:
    data = segment.get('data')
    if isinstance(data, Mapping):
        text = str(data.get('text') or '')
    else:
        text = str(data or '')
    if not text.strip():
        return
    result = await ctx.run_hook(ctx.hook.send_text, ctx.wxid, text, ctx.account)
    if int(result.get('ret', -1)) == 0:
        ctx.mark('text')
    else:
        ctx.fail('text', f'ret={result.get("ret")}')


# ---------------------------------------------------------------- 图片

async def _handle_image(ctx: SendContext, segment: Mapping[str, Any]) -> None:
    raw = _decode_bytes(segment)
    path = sticker.stage_image(ctx.cfg['project_root'], raw)
    await _send_with_lease(
        ctx, path,
        lambda: ctx.run_hook(ctx.hook.send_image, ctx.wxid, str(path), ctx.account),
        'image', f'图片已发送 -> {ctx.display} {path.name} {len(raw)} 字节')


# ---------------------------------------------------------------- 表情

async def _handle_emoji(ctx: SendContext, segment: Mapping[str, Any]) -> None:
    raw = _decode_bytes(segment)
    gif = sticker.to_native_gif(raw)
    path, md5, length = sticker.stage_gif(ctx.cfg['project_root'], gif)
    await _send_with_lease(
        ctx, path,
        lambda: ctx.run_hook(ctx.hook.send_sticker, ctx.wxid, str(path), md5, length, ctx.account),
        'sticker', f'表情已发送 -> {ctx.display} md5={md5} {length} 字节')


# ---------------------------------------------------------------- 语音

async def _handle_voice(ctx: SendContext, segment: Mapping[str, Any]) -> None:
    raw = _decode_bytes(segment)
    sample_rate = int(ctx.cfg.get('tts_sample_rate') or 24000)
    silk, duration = media.to_silk(raw, sample_rate)
    path = media.stage_silk(ctx.cfg['project_root'], silk)
    plugin = ctx.cfg.get('_plugin_ref')
    if plugin is not None and hasattr(plugin, '_send_voice_with_backoff'):
        await _send_with_lease(
            ctx, path,
            lambda: plugin._send_voice_with_backoff(
                ctx.wxid, str(path), duration, ctx.account),
            'voice', f'语音已提交 -> {ctx.display} {duration} ms')
    else:
        await _send_with_lease(
            ctx, path,
            lambda: ctx.run_hook(ctx.hook.send_voice, ctx.wxid, str(path), duration, ctx.account),
            'voice', f'语音已提交 -> {ctx.display} {duration} ms')


# ---------------------------------------------------------------- 文件

async def _handle_file(ctx: SendContext, segment: Mapping[str, Any]) -> None:
    raw, filename = sticker.decode_file_segment(segment)
    path, md5, size = media.stage_file(ctx.cfg['project_root'], raw, filename)
    await _send_with_lease(
        ctx, path,
        lambda: ctx.run_hook(ctx.hook.send_file, ctx.wxid, str(path), md5, size, ctx.account),
        'file', f'文件已发送 -> {ctx.display} {path.name} {size} 字节')


@dataclass(frozen=True)
class SendCapability:
    seg_types: tuple[str, ...]          # 匹配的段类型（含别名）
    kind: str                           # 计数键（sent 字典的 key）
    handle: Handler
    inter_seg_delay_s: float = 0.4      # 同类多段之间的物理间隔


_REGISTRY: dict[str, SendCapability] = {}


def register(cap: SendCapability) -> None:
    for t in cap.seg_types:
        _REGISTRY[t] = cap


register(SendCapability(('text',), 'text', _handle_text, 0.1))
register(SendCapability(('image',), 'image', _handle_image, 0.4))
register(SendCapability(('emoji', 'sticker'), 'sticker', _handle_emoji, 0.4))
register(SendCapability(('voice', 'record', 'audio'), 'voice', _handle_voice, 0.5))
register(SendCapability(('file',), 'file', _handle_file, 0.5))


def capability_for(seg_type: str) -> SendCapability | None:
    return _REGISTRY.get(str(seg_type or '').strip().lower())


async def dispatch_segments(ctx: SendContext, segments: list[Mapping[str, Any]]) -> None:
    """按注册表分发段列表。未注册类型降级为占位文本记进 errors。"""
    from .plugin import _NativeTimeoutError
    for index, segment in enumerate(segments):
        if not isinstance(segment, Mapping):
            continue
        stype = str(segment.get('type') or '').strip().lower()
        # custom 段解包：{"type":"custom","data":{"custom_type":"voice","data":b64}}
        if stype == 'custom' and isinstance(segment.get('data'), Mapping):
            inner = segment['data']
            stype = str(inner.get('custom_type') or inner.get('type') or '').lower()
            segment = {'type': stype, 'data': inner.get('data', inner.get('content'))}

        cap = capability_for(stype)
        if cap is None:
            if stype and stype not in ('reply',):
                ctx.fail(stype, f'未注册的段类型，跳过: [{stype}]')
            continue
        try:
            await cap.handle(ctx, segment)
        except asyncio.CancelledError:
            raise
        except _NativeTimeoutError:
            # V2-7：结果未知 —— 上抛给发送入口记 UNKNOWN
            raise
        except Exception as exc:
            log.warning('段处理失败 type=%s: %s', stype, exc)
            ctx.fail(stype, str(exc))
        if index + 1 < len(segments):
            await asyncio.sleep(cap.inter_seg_delay_s)
