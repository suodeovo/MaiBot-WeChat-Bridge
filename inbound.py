"""入站管线：微信 SSE 推送 -> MaiBot 聊天管线。

从 plugin.py 拆出（T1）。WxSupervisorPlugin 在 on_load 时把自身传进来，
这里只做三件事：

    1. 过滤（白名单 / 唤醒模式）
    2. 组装（wx_message 标准组件 + message_dict）
    3. 投递（route_message，带 ready 重报重试）

出站、节流、TTS、生命周期都还在 plugin.py / capabilities.py。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Mapping

from . import wx_image, wx_message
from .gateway import (CHAT_TYPE_GROUP, GATEWAY_NAME, PLATFORM,
                      build_message_dict)
from .identity import (NormalizedMessage, PolicyDecision, SessionPolicy,
                       normalize_payload)  # noqa: F401  (NormalizedMessage/PolicyDecision 供类型引用)

if TYPE_CHECKING:                     # 仅类型标注用，避免循环 import
    from .plugin import WxSupervisorPlugin

log = logging.getLogger('wxsupervisor.inbound')


class InboundPipeline:
    """一次构建、长期使用；持主插件引用拿配置与网关能力。"""

    def __init__(self, plugin: 'WxSupervisorPlugin') -> None:
        self.plugin = plugin
        self.policy = SessionPolicy(plugin)

    # ------------------------------------------------ 过滤（WX-001 统一策略）

    def policy_allows(self, payload: Mapping[str, Any],
                      session_id: str) -> bool:
        """兼容测试与旧调用：payload+session -> 统一规范化 -> 策略判定。"""
        account = ''
        reader = getattr(self.plugin, 'reader', None)
        if reader is not None:
            try:
                account = str(reader.status().get('ownWxid') or '')
            except Exception:
                account = ''
        msg = normalize_payload(dict(payload), account=account)
        if not msg.session_id:
            msg = normalize_payload({**payload, 'sessionId': session_id},
                                    account=account)
        decision = self.policy.allows_inbound(msg)
        if not decision.allowed and decision.reason:
            log.debug('策略拒绝: %s', decision.reason)
        return decision.allowed

    def _wake_allows(self, session_id: str, source_name: str, text: str) -> bool:
        # 兼容旧签名：转发到统一策略（文本判定保留）。
        settings = self.plugin._settings()
        if not session_id.endswith('@chatroom'):
            return True
        policy = self.policy
        overrides = policy.mode_overrides()
        mode = overrides.get(session_id) or settings.wechat.wake_mode
        if mode != 'mention':
            return True
        words = settings.wake_word_list()
        if any(w and w in text for w in words):
            return True
        log.debug('mention 模式且未命中唤醒词，跳过: %s', text[:30])
        return False

    # ------------------------------------------------ 投递

    async def _ensure_gateway_ready(self, self_wxid: str) -> None:
        gateway = getattr(getattr(self.plugin, 'ctx', None), 'gateway', None)
        if gateway is None or self.plugin._gateway_ready:
            return
        try:
            accepted = await gateway.update_state(
                GATEWAY_NAME,
                ready=True,
                platform=PLATFORM,
                account_id=self_wxid,
                scope='*',
            )
            if accepted:
                self.plugin._gateway_ready = True
                log.info('已上报网关 ready: gateway=%s account=%s',
                         GATEWAY_NAME, self_wxid)
            else:
                log.warning('网关 ready 上报未被 Host 接受，稍后重试')
        except Exception as exc:
            log.warning('上报网关 ready 失败: %s', exc)

    # ------------------------------------------------ 主入口

    async def submit(self, payload: dict[str, Any]) -> None:
        plugin = self.plugin
        gateway = getattr(getattr(plugin, 'ctx', None), 'gateway', None)
        if gateway is None:
            log.debug('gateway 能力未注入，跳过入站投递')
            return

        try:
            st = await plugin._loop.run_in_executor(None, plugin.reader.status)
        except Exception:
            st = {}
        self_wxid = str(st.get('ownWxid') or '')
        if not self_wxid:
            log.debug('拿不到 ownWxid，跳过入站投递')
            return

        # WX-001：入站边界统一规范化身份
        msg = normalize_payload(payload, account=self_wxid)
        session_id = msg.session_id
        text = msg.text
        if not session_id or not text:
            return

        # 用 reader 会话表刷新别名目录（失败保留旧表）
        try:
            sessions = await plugin._loop.run_in_executor(None, plugin.reader.sessions)
            self.policy.directory.update(sessions or [])
        except Exception as exc:
            log.debug('会话目录刷新失败: %s', exc)

        # R3/R6：接收去重 —— 重复 rawid 直接丢弃，不投递不暂存
        if plugin.recovery is not None:
            try:
                kind = await plugin.recovery.classify_inbound(payload)
                if kind == 'duplicate':
                    log.debug('重复消息丢弃: %s', msg.rawid[:12])
                    return
            except Exception as exc:
                log.debug('接收去重检查失败（放行）: %s', exc)

        decision = self.policy.allows_inbound(msg)
        if not decision.allowed:
            log.debug('策略未放行: %s (%s) 原因: %s',
                      msg.session_display or session_id, session_id, decision.reason)
            return
        if not self.policy.wake_allows(msg):
            return

        # 结构化段：引用/转发/文件/拍一拍 → 核心标准组件 dict。
        try:
            _plain, segments = wx_message.build_raw_segments(payload)
        except Exception as exc:
            log.warning('结构化段构建失败，退回纯文本: %s', exc)
            segments = None

        # W4/W5: 图片与表情推送 → 解码 → 标准 image/emoji 段。
        # WX-009：慢媒体经 SessionMediaScheduler —— 同会话保序、跨会话并行，
        # 一个群的慢图片不再堵住其他群的文本。
        if isinstance(payload.get('image'), Mapping):
            resolver = plugin._image_resolver()
            account_dir = wx_image.account_dir_of(plugin.cfg or {})

            async def decode_media():
                return await plugin._loop.run_in_executor(
                    plugin.workers._executor, wx_image.enrich_image_payload,
                    payload, resolver, self_wxid, account_dir)

            try:
                seg = await plugin.media_scheduler.run_ordered(
                    session_id, decode_media)
                if seg is not None and segments:
                    # 表情段：type=47 推送用 emoji 组件（核心走表情库），图片用 image 组件
                    want = 'emoji' if str(payload.get('type')) == '47' or payload.get('type') == 47 else 'image'
                    if want == 'emoji':
                        seg = dict(seg)
                        seg['type'] = 'emoji'
                    placeholder = '[表情包：附件为静态画面或动画采样画面]' if want == 'emoji' else '[图片]'
                    replaced = False
                    for idx, s in enumerate(segments):
                        if s.get('type') == 'text' and placeholder in str(s.get('data') or ''):
                            segments[idx] = seg
                            replaced = True
                            break
                    if not replaced and not any(s.get('type') in ('image', 'emoji') for s in segments):
                        segments.append(seg)
            except Exception as exc:
                log.warning('图片解码失败，保持占位: %s', exc)

        msg_dict = build_message_dict(
            self_wxid=self_wxid,
            session_id=session_id,
            source_name=msg.group_name if msg.is_group else msg.session_display,
            sender=msg.sender_id,
            sender_name=msg.sender_name,
            text=text,
            timestamp=payload.get('timestamp'),
            message_id=str(payload.get('rawid') or ''),
            raw_segments=segments,
        )

        # 语音请求判定（on_request 模式）按会话记录（WX-008 隔离）
        plugin.note_inbound_text(session_id, text)

        route_metadata: dict[str, Any] = {'self_id': self_wxid}
        mid = msg_dict['message_id']

        await self._ensure_gateway_ready(self_wxid)

        # V2-4：确认事务 —— pending 先持久化，投递成功才 confirmed。
        # 去重标记不在投递前写（CancelledError/崩溃窗口会造成假 received）；
        # 重复拦截由入口 classify_inbound（confirmed 表）承担。
        if plugin.recovery is not None:
            plugin.recovery.stage_unconfirmed(mid, payload,
                                              origin_account=self_wxid)

        try:
            accepted = await gateway.route_message(
                gateway_name=GATEWAY_NAME,
                message=msg_dict,
                route_metadata=route_metadata,
                external_message_id=mid,
                dedupe_key=mid,
            )
            if not accepted:
                log.warning('MaiBot 未接受消息，重报网关状态后重试: %s', mid)
                plugin._gateway_ready = False
                await self._ensure_gateway_ready(self_wxid)
                accepted = await gateway.route_message(
                    gateway_name=GATEWAY_NAME,
                    message=msg_dict,
                    route_metadata=route_metadata,
                    external_message_id=mid,
                    dedupe_key=mid,
                )
            if accepted:
                plugin.usage.count_recv(session_id, len(text))
                # V2-4：确认事务 —— dedup + 水位（最早未确认边界）+ 持久化
                if plugin.recovery is not None:
                    try:
                        plugin.recovery.on_message_processed(self_wxid, payload)
                    except Exception as exc:
                        log.warning('确认事务失败: %s', exc)
            else:
                log.warning('重试后仍未被接收: %s', mid)
                # 已是 pending（入口暂存）—— 保持，等待重放
                if plugin.recovery is None:
                    plugin.replay.stage(mid, payload)
        except asyncio.CancelledError:
            # V2-4：取消不是失败 —— 事件保持 pending，去重无残留
            raise
        except Exception as exc:
            log.warning('投递到 MaiBot 失败: %s', exc)
            if plugin.recovery is None:
                plugin.replay.stage(mid, payload)
        finally:
            plugin._recent_sessions.add(session_id)
