"""历史回填（WX-007 重写）。

## 宿主契约（1.3.16 已核实）

`is_notify=True` 的消息会入库，但被宿主所有模型上下文恢复路径过滤
（maisaka/runtime.py 304/335/429、replyer 历史、planner 注入），
且不会触发回复 —— 与旧 docstring 声称的「进上下文」相反。

因此本模块交付两层：

1. **is_notify=True 回填**（默认）：安全 —— 消息入库、绝不触发回复；
   「进入模型上下文」依赖宿主未来提供安全历史导入契约
   （候选接口见 docs/host-history-import-proposal.md，未实现，标记待验收）。
2. **from_time 增量查询**：reader 隔离副本已扩展（旧客户端兼容：
   不传 from_time 时行为与旧版一致）。

## WX-001/WX-007 修复点

* 白名单别名先经 SessionDirectory 解析到**原始会话 wxid** 再查询
  （旧实现把「群名/数字 ID」直接当数据库 talker）。
* 查询参数 urllib.parse.urlencode 编码。
* 只累计 Host 实际接受（accepted=True）的投递；被拒绝计入 rejected。
* 只回填允许且未停用的会话（停用会话不回填）。
* 时间正序投递（reader 倒序拉、reversed 投）；保留成员身份与 rawid。
* 去重：同 message_id（rawid）不重复投递。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Any, Mapping

from .gateway import GATEWAY_NAME, build_message_dict
from .identity import is_group_session

if TYPE_CHECKING:
    from .plugin import WxSupervisorPlugin

log = logging.getLogger('wxsupervisor.backfill')


def _fetch_messages(base: str, token: str, talker: str, limit: int,
                    from_time: int | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {'talker': talker, 'limit': int(limit)}
    if from_time is not None:
        params['from_time'] = int(from_time)
    url = f'{base.rstrip("/")}/api/v1/messages?{urllib.parse.urlencode(params)}'
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    return list(data.get('messages') or [])


def payload_from_reader_row(talker: str, row: Mapping[str, Any]) -> dict[str, Any] | None:
    """reader 的 messages 行 → 与 SSE 推送同构的 payload（R13：完整契约）。

    * 与 reader push_payload 相同的过滤：isSend（自身消息）跳过；
      支持类型之外的行跳过（补收不得引入 reader 不推送的类型）。
    * 身份字段完整：groupName=群名（会话级，不是发言人昵称）、
      talkerId/sender=发言人、senderName/sourceName=发言人显示名。
    * 结构化描述保留：file/quoteReply/forwardedRecord/image 从行字段
      恢复（reader messages 行带 fullText，可解析出引用/转发/文件）。
    * 语音（34）：带 voice 描述，转换层生成 voice 段（见 wx_message）。
    """
    sender = str(row.get('senderUsername') or '')
    # reader push_payload 契约：isSend（自己发的）不推送
    if row.get('isSend'):
        return None
    kind = row.get('localType')
    supported = (1, 3, 34, 47, 1048625, 922746929, 822083633,
                 244813135921, 25769803825, 49)
    if kind not in supported:
        return None
    is_group = is_group_session(talker)
    display = str(row.get('senderDisplay') or sender)
    content = str(row.get('parsedContent') or row.get('content') or '')
    full_text = str(row.get('fullText') or '')
    if not content.strip() and not full_text.strip():
        return None
    payload: dict[str, Any] = {
        'sessionId': talker,
        'rawid': hashlib_rawid(talker, row),
        'sessionType': 'group' if is_group else 'private',
        'groupName': display if False else '',   # 占位：下方按会话名填充
        'sourceName': display,
        'talkerId': sender,
        'sender': sender,
        'senderName': display,
        'content': content,
        'fullText': full_text,
        'type': kind,
        'timestamp': row.get('createTime'),
    }
    # 结构化描述：reader messages 行可能带解析结果字段
    if isinstance(row.get('quoteReply'), Mapping):
        payload['quoteReply'] = row['quoteReply']
    if isinstance(row.get('forwardedRecord'), Mapping):
        payload['forwardedRecord'] = row['forwardedRecord']
    if isinstance(row.get('file'), Mapping):
        payload['file'] = row['file']
    if kind == 3 or kind == 47:
        payload['image'] = {'localId': row.get('localId'),
                            'serverId': row.get('serverId'),
                            'createTime': row.get('createTime'),
                            'localType': kind}
    if kind == 34:
        payload['voice'] = {'localId': row.get('localId'),
                            'serverId': row.get('serverId'),
                            'createTime': row.get('createTime'),
                            'localType': 34}
    # 群名由调用方（补收/回填）按会话目录填充 —— 行内没有会话级群名
    payload['groupName'] = str(row.get('groupName') or '') if is_group else ''
    return payload


def hashlib_rawid(talker: str, row: Mapping[str, Any]) -> str:
    """与 reader push_payload 相同的 rawid 算法（保持 message_id 一致）。"""
    digest = json.dumps([talker, row.get('serverId'), row.get('localId'),
                         row.get('createTime')], separators=(',', ':'))
    return hashlib.sha256(digest.encode()).hexdigest()


class HistoryBackfill:
    """一次构建；on_load 后跑一轮，失败不影响主链。"""

    def __init__(self, plugin: 'WxSupervisorPlugin') -> None:
        self.plugin = plugin

    async def run(self) -> int:
        """回填所有白名单会话。返回 Host 实际接受的条数。"""
        plugin = self.plugin
        settings = plugin._settings()
        count = int(getattr(settings.wechat, 'backfill_count', 0) or 0)
        if count <= 0:
            return 0
        gateway = getattr(getattr(plugin, 'ctx', None), 'gateway', None)
        if gateway is None:
            return 0

        try:
            st = await plugin._loop.run_in_executor(None, plugin.reader.status)
        except Exception:
            st = {}
        self_wxid = str(st.get('ownWxid') or '')
        if not self_wxid:
            log.debug('拿不到 ownWxid，跳过历史回填')
            return 0

        # 会话目录刷新：别名解析需要 reader 会话表
        policy = plugin.inbound.policy
        try:
            sessions = await plugin._loop.run_in_executor(None, plugin.reader.sessions)
            policy.directory.update(sessions or [])
        except Exception as exc:
            log.debug('回填前目录刷新失败（沿用旧表）: %s', exc)

        groups_raw = str(settings.wechat.whitelist_groups or '')
        privates_raw = str(settings.wechat.whitelist_private or '')
        talkers: list[str] = []
        unresolved: list[str] = []
        for raw in (groups_raw, privates_raw):
            for entry in (x.strip() for x in raw.replace('，', ',').split(',') if x.strip()):
                session, status = policy.directory.resolve_alias(entry)
                if status == 'ok':
                    talkers.append(session)
                elif is_group_session(entry) or entry.startswith('wxid_'):
                    talkers.append(entry)
                else:
                    unresolved.append(entry)
        if unresolved:
            log.warning('回填白名单中无法解析的条目（跳过）: %s', unresolved)
        # 白名单为空 = 全部允许：只回填 SSE 最近见过的会话，避免全库扫描
        if not talkers:
            talkers = sorted(plugin._recent_sessions)

        accepted_total = 0
        rejected_total = 0
        base = plugin.cfg['reader_base_url']
        token = plugin._reader_token()
        loop = plugin._loop
        for talker in dict.fromkeys(talkers):
            # 停用会话不回填（WX-007）
            decision = policy.allows_outbound(talker)
            if not decision:
                log.info('会话 %s 已停用/不在白名单，跳过回填', talker)
                continue
            try:
                rows = await loop.run_in_executor(
                    None, _fetch_messages, base, token, talker, count)
            except Exception as exc:
                log.warning('回填写取失败 %s: %s', talker, exc)
                continue
            # reader 返回倒序（新→旧），投递要正序
            for row in reversed(rows):
                payload = payload_from_reader_row(talker, row)
                if payload is None:
                    continue
                text = str(payload.get('content') or '').strip()
                if not text:
                    continue
                accepted = await self._deliver_one(
                    gateway, self_wxid, talker, payload, text)
                if accepted:
                    accepted_total += 1
                else:
                    rejected_total += 1
        if accepted_total or rejected_total:
            log.info('历史回填完成: 接受 %d 条 / 拒绝 %d 条',
                     accepted_total, rejected_total)
        return accepted_total

    async def _deliver_one(self, gateway: Any, self_wxid: str, talker: str,
                           payload: Mapping[str, Any], text: str) -> bool:
        """单条投递。返回 Host 是否实际接受。"""
        from . import wx_message
        try:
            _plain, segments = wx_message.build_raw_segments(dict(payload))
        except Exception:
            segments = None
        msg = build_message_dict(
            self_wxid=self_wxid,
            session_id=talker,
            source_name=str(payload.get('groupName') or payload.get('sourceName') or ''),
            sender=str(payload.get('talkerId') or payload.get('sender') or ''),
            sender_name=str(payload.get('senderName') or ''),
            text=text,
            timestamp=payload.get('timestamp'),
            message_id=str(payload.get('rawid') or ''),
            raw_segments=segments,
            is_notify=True,               # 只入历史，不触发回复（安全层）
        )
        try:
            accepted = await gateway.route_message(
                gateway_name=GATEWAY_NAME,
                message=msg,
                route_metadata={'self_id': self_wxid, 'backfill': True},
                external_message_id=msg['message_id'],
                dedupe_key=msg['message_id'],
            )
        except Exception as exc:
            log.warning('回填投递异常 %s: %s', msg['message_id'][:12], exc)
            return False
        return bool(accepted)
