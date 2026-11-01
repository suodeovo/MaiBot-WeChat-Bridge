"""微信推送 -> MaiBot 标准消息组件 dict。

## 为什么需要这一层

核心（`plugin_runtime/host/message_utils._component_from_dict`）按固定契约
消费 `raw_message` 段：

    {"type": "text",  "data": "..."}
    {"type": "image", "data": "<描述可空>", "hash": "<sha256可空>",
     "binary_data_base64": "<b64>"}
    {"type": "emoji", ...} 同 image
    {"type": "voice", ...} 同 image（核心自动走 ASR，enable_asr 开时）
    {"type": "file",  "data": {"name","size","url","file_id"}}
    {"type": "reply", "data": {"target_message_id",
     "target_message_content","target_message_sender_id",
     "target_message_sender_nickname"}}
    {"type": "at",    "data": {"target_user_id","target_user_nickname"}}

带 `binary_data_base64` 的段进核心后由对应子系统自动处理：
图片 -> VLM 描述；语音 -> ASR 转录；表情 -> 表情库注册/描述。
插件侧只负责「解码出字节 + 组装 dict」。

reader 的推送（push_payload）已经解析好结构化字段：
    quoteReply / forwardedRecord / file / image / pat
本模块把它们映射成上面的段。图片/语音的字节级解码在 W4/W6 落地前，
先投递带占位描述的段（不塞 binary），核心按 content 文本渲染。
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any, Mapping


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


def text_seg(text: str) -> dict[str, Any]:
    return {'type': 'text', 'data': str(text or '')}


def media_seg(kind: str, data: bytes, content: str = '') -> dict[str, Any]:
    """图片/表情/语音通用段。kind ∈ image|emoji|voice。"""
    return {'type': kind, 'data': str(content or ''), 'hash': _sha256(data),
            'binary_data_base64': _b64(data)}


def file_seg(name: str, size: int, md5: str = '') -> dict[str, Any]:
    """文件段。核心 FileComponent.from_payload 认 name/size/url/file_id。"""
    return {'type': 'file', 'data': {'name': str(name or 'file'),
                                     'size': int(size or 0),
                                     'md5': str(md5 or '')}}


def reply_seg(*, message_id: str, content: str, sender_id: str = '',
              sender_name: str = '') -> dict[str, Any]:
    """引用段：content 带上就能被核心直接渲染，无需查库。"""
    return {'type': 'reply', 'data': {
        'target_message_id': str(message_id or ''),
        'target_message_content': str(content or ''),
        'target_message_sender_id': str(sender_id or ''),
        'target_message_sender_nickname': str(sender_name or ''),
    }}


def at_seg(user_id: str, nickname: str = '') -> dict[str, Any]:
    return {'type': 'at', 'data': {'target_user_id': str(user_id or ''),
                                   'target_user_nickname': str(nickname or '')}}


def forward_seg(title: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """合并转发段。节点结构与核心 ForwardComponent 对齐。"""
    nodes = []
    for item in (items or [])[:50]:
        nodes.append({
            'sender_name': str(item.get('name') or '未知发言人'),
            'time': str(item.get('time') or ''),
            'content': str(item.get('text') or ''),
        })
    return {'type': 'forward', 'data': nodes, 'title': str(title or '聊天记录')}


def build_raw_segments(payload: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """reader 推送 -> (主文本, raw_message 段列表)。

    段顺序 = 模型看到的顺序：引用上下文在前，正文在后。
    字节级媒体（W4/W6 解码）落地后由 enrichment 钩子替换对应段。
    """
    segments: list[dict[str, Any]] = []
    mtype = payload.get('type')

    # ---- 引用回复：reader 已解析 quoteReply.reference
    quote = payload.get('quoteReply')
    if isinstance(quote, Mapping):
        ref = quote.get('reference')
        if isinstance(ref, Mapping):
            ref_text = str(ref.get('text') or '')
            if ref.get('unavailable'):
                ref_text = ref_text or '（引用内容不可见）'
            segments.append(reply_seg(
                message_id=str(ref.get('messageId') or ''),
                content=ref_text[:800],
                sender_id=str(ref.get('senderId') or ''),
                sender_name=str(ref.get('senderName') or ''),
            ))

    # ---- 合并转发：摘要进正文，段里带结构
    fwd = payload.get('forwardedRecord')
    if isinstance(fwd, Mapping):
        items = fwd.get('items')
        if isinstance(items, list) and items:
            segments.append(forward_seg(str(fwd.get('title') or '聊天记录'), items))

    # ---- 拍一拍：纯文本即可
    if payload.get('pat'):
        pat = payload['pat']
        who = ''
        if isinstance(pat, Mapping):
            who = str(pat.get('targetName') or pat.get('actor') or '')
        segments.append(text_seg('[拍一拍]' + (f'（{who}）' if who else '')))

    # ---- 文件：元信息段（核心 to_plain_text 会渲染名字+大小）
    finfo = payload.get('file')
    if isinstance(finfo, Mapping):
        segments.append(file_seg(str(finfo.get('name') or ''),
                                 int(finfo.get('size') or 0),
                                 str(finfo.get('md5') or '')))

    # ---- 语音（R13a）：voice 段进核心走 ASR 转写。
    # reader 推送 voice 描述（localId/serverId/createTime）；音频字节
    # 解码依赖 reader 侧语音文件接口（docs/reader-cursor-patch.md 附录，
    # 未实现）。当前投递 voice 段（无 binary），核心按文本渲染降级；
    # reader 补丁就绪后此处自动携带 binary_data_base64。
    if payload.get('type') == 34 or payload.get('voice'):
        voice_info = payload.get('voice')
        descriptor = ''
        if isinstance(voice_info, Mapping):
            descriptor = ' '.join(
                f'{k}={voice_info[k]}' for k in ('localId', 'serverId', 'createTime')
                if voice_info.get(k) is not None)
        segments.append({
            'type': 'voice',
            'data': descriptor,
            'hash': '',
            # 字节可用时填充；空串表示核心走文本降级（不是丢弃）
            'binary_data_base64': '',
        })
        note = '[收到一条语音' + ('，待音频解码' if not descriptor else '') + ']'
        segments.append(text_seg(note))

    # ---- 正文文本
    # reader 的 content 已经把结构化内容渲染过一遍（[文件] x / [合并转发聊天记录] x /
    # 引用正文 / [语音] 等）。段里已有对应结构时，再拼一遍就是重复。
    body = str(payload.get('content') or '')
    has_struct = bool(segments)
    if has_struct:
        quote = payload.get('quoteReply')
        if isinstance(quote, Mapping) and quote.get('reply'):
            body = str(quote['reply'])
        elif payload.get('pat') or payload.get('voice') or payload.get('type') == 34:
            body = ''          # 拍一拍/语音：占位段已表达，正文丢弃
        elif isinstance(fwd, Mapping):
            body = ''
        elif isinstance(finfo, Mapping):
            body = ''
    if body.strip():
        segments.append(text_seg(body[:4000]))

    plain = ''.join(
        str(s['data']) if s['type'] == 'text'
        else str((s.get('data') or {}).get('target_message_content') or '') if s['type'] == 'reply'
        else f"[转发: {s.get('title', '')}]" if s['type'] == 'forward'
        else f"[文件: {(s.get('data') or {}).get('name', '')}]" if s['type'] == 'file'
        else ''
        for s in segments
    )
    return plain, segments


def is_media_payload(payload: Mapping[str, Any]) -> bool:
    """这条推送是否携带可解码的媒体（W4/W6 的解码钩子据此触发）。"""
    mtype = payload.get('type')
    return mtype == 3 or isinstance(payload.get('image'), Mapping)
