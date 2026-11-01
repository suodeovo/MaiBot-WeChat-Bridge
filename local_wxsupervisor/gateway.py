"""MaiBot 消息网关对接：入站投递 + 出站发送。

MaiBot 的插件不是 OneBot 客户端，它靠 `MessageGateway` 装饰器声明一条网关，
然后双向走：

    入站  本插件收到微信消息
          → ctx.gateway.route_message(gateway_name=..., message=..., ...)
          → MaiBot 聊天管线

    出站  MaiBot 要把消息发到微信
          → 调用被 @MessageGateway 标记的 handle_xxx(message, route, metadata)
          → 本插件转成 hook 原生发送

关键约束（旧插件 event_router 的注释里写得很清楚）：
`route_message` 是 RPC，必须在 **MaiBot Runner 的 loop 内** 调用。
我们这个插件的 on_load 就跑在 Runner loop 内，所以只要保证
不是从 SSE 的 reader 线程直接调，而是 `loop.call_soon_threadsafe` /
`run_coroutine_threadsafe` 回到主 loop，就没问题。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Mapping

log = logging.getLogger('wxsupervisor.gateway')

GATEWAY_NAME = 'wxsupervisor_gateway'
PLATFORM = 'wechat'
PROTOCOL = 'ob11'

CHAT_TYPE_GROUP = 'group'
CHAT_TYPE_PRIVATE = 'private'


def build_message_dict(*, self_wxid: str, session_id: str, source_name: str,
                       sender: str, sender_name: str, text: str,
                       timestamp: float | None = None,
                       message_id: str = '', is_at_self: bool = False,
                       raw_segments: list[dict[str, Any]] | None = None,
                       is_notify: bool = False) -> dict[str, Any]:
    """reader 的一条推送 -> MaiBot 的 message_dict。

    字段对齐旧插件 `event_router._build_message_dict`，其中 group_info /
    additional_config 是 MaiBot 路由回写时必须看的。

    is_notify=True 的消息只入历史、不触发回复（N4 历史回填用）。

    WX-001：user_id 是发言人 wxid，绝不能回退成群 ID —— 旧实现空 sender
    回退群 ID，导致两个成员发言的 user_id 都是群 ID（身份合并缺陷）。
    """
    is_group = str(session_id).endswith('@chatroom')
    chat_type = CHAT_TYPE_GROUP if is_group else CHAT_TYPE_PRIVATE
    ts = float(timestamp or time.time())
    mid = message_id or f'wxsup-{int(ts * 1000)}'
    plain = str(text or '')
    user_id = str(sender or '').strip()
    if not user_id:
        # 发言人缺失：群聊不落群 ID（会合并成员身份），留空并诊断；
        # 私聊 session 即对方 wxid，允许作为发言人。
        if is_group:
            log.warning('群消息缺少发言人 wxid，保留空 user_id: session=%s mid=%s',
                        session_id, mid)
        else:
            user_id = session_id

    additional: dict[str, Any] = {
        'self_id': self_wxid,
        'wemai_msg_kind': 'text',
        'wemai_chat_type': chat_type,
    }
    if is_group:
        additional['platform_io_target_group_id'] = session_id
    else:
        additional['platform_io_target_user_id'] = user_id or session_id

    message_info: dict[str, Any] = {
        'user_info': {
            'user_id': user_id,
            'user_nickname': sender_name or user_id or session_id,
            'user_cardname': None,
        },
        'additional_config': additional,
    }
    if is_group:
        message_info['group_info'] = {
            'group_id': session_id,
            'group_name': source_name or session_id,
        }

    # 段列表：wx_message.build_raw_segments 产出的标准组件 dict
    # （image/voice/emoji/file/reply/forward），核心 _component_from_dict 直接消费。
    # 没传时退回单文本段（旧路径兼容）。
    if raw_segments:
        segments = raw_segments
    else:
        segments = [{'type': 'text', 'data': plain}]

    return {
        'message_id': mid,
        'timestamp': str(ts),
        'platform': PLATFORM,
        'message_info': message_info,
        'raw_message': segments,
        'is_mentioned': bool(is_at_self),
        'is_at': bool(is_at_self),
        'is_emoji': False,
        'is_picture': False,
        'is_command': plain.startswith('/'),
        'is_notify': bool(is_notify),
        'session_id': session_id if is_notify else '',
        'processed_plain_text': plain,
        'display_message': plain,
    }


def resolve_outbound(message: Mapping[str, Any],
                     route: Mapping[str, Any] | None = None) -> tuple[str, str, str]:
    """从 MaiBot 的出站请求里取出 (target, chat_type, plain_text)。

    MaiBot 给回来的 route 可能带 platform_io_target_group_id / _user_id，
    也可能只有 message_info.group_info.group_id。都兼容。

    文本来源按优先级依次尝试（实测 MaiBot 回复时常常只填后面几个）：
        1) raw_message          标准消息段列表
        2) message              另一种消息段字段名
        3) processed_plain_text 已处理的纯文本
        4) display_message / plain_text / text
    """
    info = message.get('message_info') if isinstance(message, Mapping) else None
    info = info if isinstance(info, Mapping) else {}
    additional = info.get('additional_config')
    additional = additional if isinstance(additional, Mapping) else {}
    group_info = info.get('group_info')
    group_info = group_info if isinstance(group_info, Mapping) else {}
    route = route if isinstance(route, Mapping) else {}
    user_info = info.get('user_info')
    user_info = user_info if isinstance(user_info, Mapping) else {}

    def pick(*candidates: Any) -> str:
        for c in candidates:
            if c not in (None, '', 0):
                return str(c)
        return ''

    target = pick(
        additional.get('platform_io_target_group_id'),
        route.get('group_id'),
        group_info.get('group_id'),
        additional.get('platform_io_target_user_id'),
        route.get('user_id'),
        user_info.get('user_id'),
    )

    chat_type = str(additional.get('wemai_chat_type') or '').strip()
    if not chat_type:
        chat_type = CHAT_TYPE_GROUP if str(target).endswith('@chatroom') else CHAT_TYPE_PRIVATE

    # 文本：段列表优先，再退到纯文本字段
    text = ''
    for key in ('raw_message', 'message', 'messages'):
        candidate = _segments_to_text(message.get(key))
        if candidate.strip():
            text = candidate
            break
    if not text.strip():
        # 注意用 _as_text 而不是 str()：这些字段有时是 {'text': '...'} 形态，
        # 直接 str() 会把字典字面量发到微信里。
        text = _as_text(
            message.get('processed_plain_text')
            or message.get('display_message')
            or message.get('plain_text')
            or message.get('text')
        )

    return target, chat_type, text


def _as_text(value: Any) -> str:
    """把各种形态的「文本」统一成字符串。

    踩过的坑：`processed_plain_text` / `display_message` / `text` 这些字段
    有时不是字符串，而是 `{'text': '...'}` 这样的消息段 dict。早期实现
    直接 `str(value)`，于是微信里会收到 Python 字典的字面量：

        {'text': '@小柚\\u2005出来玩了'} 嗯？叫我干嘛呀

    这里按真实结构逐层取，取不到就返回空串，绝不 str(dict)。
    """
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return ''
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Mapping):
        for key in ('text', 'content', 'plain_text', 'processed_plain_text',
                    'display_message', 'message', 'message_info'):
            if key in value:
                got = _as_text(value.get(key))
                if got:
                    return got
        return ''
    if isinstance(value, (list, tuple)):
        parts = [_as_text(item) for item in value]
        return ''.join(p for p in parts if p)
    return ''


def _segments_to_text(raw: Any) -> str:
    """OneBot 消息段 / 纯字符串 -> 纯文本。hook 的 SendTextMsg 只收文本。

    表情 / 图片 / 语音 / 文件在这里一律返回空 ——
    它们由 sticker.split_segments 单独抽出，走各自的原生接口。
    如果这里也拼成占位符，就会出现「媒体发出去 + 一条 `[图片]` 文本」的重复。

    兼容三种段形态：
        {"type": "text", "data": {"text": "..."}}   标准 OneBot
        {"type": "text", "data": "..."}             data 直接是字符串
        {"text": "..."}                             简化形态（缺 type）
    """
    if isinstance(raw, str):
        return raw
    if isinstance(raw, Mapping):
        # 单个段而非列表
        return _segments_to_text([raw])
    parts: list[str] = []
    for seg in raw or []:
        if isinstance(seg, str):
            parts.append(seg)
            continue
        if not isinstance(seg, Mapping):
            continue
        stype = str(seg.get('type') or '')
        data = seg.get('data')

        if stype == 'text' or (not stype and 'text' in seg):
            if isinstance(data, Mapping):
                parts.append(_as_text(data.get('text')))
            elif isinstance(data, str):
                parts.append(data)
            else:
                parts.append(_as_text(seg.get('text')))
            continue
        if stype in ('emoji', 'sticker', 'image', 'voice', 'file',
                     'record', 'audio'):
            continue          # 交给各自通道，别拼占位符
        if stype == 'custom' and isinstance(data, Mapping):
            inner = str(data.get('custom_type') or data.get('type') or '')
            if inner in ('voice', 'record', 'audio'):
                continue      # 语音 custom 段，同样交给语音通道
        if stype == 'at':
            name = ''
            if isinstance(data, Mapping):
                name = str(data.get('name') or data.get('qq') or '')
            if name:
                parts.append('@' + name)
            continue
        if stype == 'face':
            parts.append('[表情]')
            continue
        if stype == 'reply':
            continue
        # data 可能是 dict / str，统一走 _as_text 避免 str(dict)
        if isinstance(data, Mapping) and 'text' in data:
            parts.append(_as_text(data.get('text')))
        elif isinstance(data, str):
            parts.append(data)
        elif stype:
            parts.append('[%s]' % stype)
    return ''.join(parts)


# MaiBot 把非文本段渲染成的占位文本。真媒体已经由 split_segments 抽走，
# 这些占位符要是再当文本发一遍，微信里就多一条字面量「[语音]」。
MEDIA_PLACEHOLDERS = (
    '[语音]', '[语音消息]', '[voice]', '[音频]', '[audio]', '[record]',
    '[图片]', '[image]', '[表情]', '[动画表情]', '[sticker]',
    '[文件]', '[file]', '[视频]', '[video]',
)


def strip_media_placeholders(text: str) -> str:
    """去掉纯媒体占位符；剩下的才是真要说的话。"""
    if not text:
        return ''
    out = text
    for token in MEDIA_PLACEHOLDERS:
        out = out.replace(token, '')
    return out.strip()


def segment_summary(raw: Any, limit: int = 10) -> str:
    """把出站消息段压成一行摘要，用于日志定位。

    只打类型/形状/长度，不打内容 —— 语音是几十 KB 的 base64，不能进日志。
    """
    if isinstance(raw, str):
        return 'str(len=%d)' % len(raw)
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return type(raw).__name__
    out: list[str] = []
    for seg in list(raw)[:limit]:
        if isinstance(seg, str):
            out.append('str(len=%d)' % len(seg))
            continue
        if not isinstance(seg, Mapping):
            out.append(type(seg).__name__)
            continue
        stype = seg.get('type')
        data = seg.get('data')
        if isinstance(data, str):
            shape = 'str(%d)' % len(data)
        elif isinstance(data, Mapping):
            shape = 'keys=' + ','.join(sorted(str(k) for k in data.keys()))[:80]
        else:
            shape = type(data).__name__
        out.append('%s[%s%s]' % (stype, shape,
                                ',b64' if seg.get('binary_data_base64') else ''))
    return ' | '.join(out) if out else 'empty'
