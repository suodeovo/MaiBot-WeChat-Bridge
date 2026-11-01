"""群内管理命令（WX-003 重写）。

## 注册方式

命令是**插件实例方法**上的 `@Command` 装饰器（maibot_sdk.components.
collect_components 只扫实例属性），SDK 在 Runner 收集组件时即可发现。
旧实现把命令定义成 register_admin_commands() 内的局部函数——装饰器
附着在局部变量上，collect_components 扫不到，收集结果恒为 0。

## 权限（WX-003 行为变更，需在部署说明中写明）

管理员身份 = `wechat.administrators` 配置的**稳定 wxid 精确匹配**
（trim 后全等；不做子串/后缀/去前缀的模糊匹配）。
**名单为空时所有写命令直接拒绝**——旧语义「空=任何人可用」是子串
匹配的产物且不安全。查看命令 /wx状态 在空名单下也要求管理员
（磁盘当前管理员名单为空，部署后需先在 WebUI 配置管理员才能用）。

## 配置写入

写命令经 config_service.apply()：完整配置 + plugin.config_version +
模型验证 + 运行态应用 + 持久化。保存失败明确回复失败，不回「已修改」。
"""
from __future__ import annotations

import logging
from typing import Any

from maibot_sdk import Command

log = logging.getLogger('wxsupervisor.admin')

TTS_MODES = ('text_only', 'on_request', 'both', 'voice_only', 'voice_if_short')


class AdminCommandsMixin:
    """命令声明 mixin：由 WxSupervisorPlugin 继承，收集时即可发现。"""

    @Command('wx_status', description='查看微信桥接状态',
             pattern=r'^.*?/wx(?:状态|status)\s*$')
    async def wx_status(self, stream_id: str = '', user_id: str = '',
                        **kwargs: Any):
        if not self._is_admin(user_id):
            return await self._admin_reply(stream_id, '只有管理员能查看状态')
        st = self.status()
        settings = self._settings()
        usage = st.get('usage', {})
        scheduler = self._send_scheduler.snapshot()
        recovery = (self.recovery.snapshot() if self.recovery
                    else {'replay': {'pending': self.replay.pending()}})
        lines = [
            f"reader : {'在线' if st['reader']['listening'] else '离线'}",
            f"hook   : {'就绪' if st['hook']['ready'] else '未就绪'}",
            f"OneBot : {'在线' if st['onebot']['listening'] else '离线'} :{st['onebot']['port']}",
            f"SSE    : {'已连接' if st['sse']['connected'] else '断开'} (收到 {st['sse']['received']})",
            f"用量   : 收 {usage.get('total_recv', 0)} / 发 {usage.get('total_sent', 0)}"
            f" / 待重放 {replay_pending(recovery)}",
            (f"调度   : 原生/分 {scheduler.get('native_per_minute', 0)}"
             f" /10分 {scheduler.get('native_per_ten_minutes', 0)}"
             f" 并发 {scheduler.get('in_flight', 0)}/{scheduler.get('max_concurrent', 0)}"),
            f"停用   : {', '.join(sorted(disabled_display(settings))) or '无'}",
            f"分群模式: {settings.wechat.chat_modes or '无（用全局 ' + settings.wechat.wake_mode + '）'}",
            f"TTS    : {settings.tts.mode}",
        ]
        top = usage.get('top') or []
        if top:
            lines.append('活跃Top5:')
            for row in top[:5]:
                lines.append(f"  {row['session'][:32]}  收{row['recv']} 发{row['sent']}")
        return await self._admin_reply(stream_id, '\n'.join(lines))

    @Command('wx_disable_chat', description='停用某个会话（不收不回）',
             pattern=r'^.*?/wx停用\s+(?P<chat>\S+)\s*$')
    async def wx_disable_chat(self, stream_id: str = '', user_id: str = '',
                              chat: str = '', **kwargs: Any):
        if not self._is_admin(user_id):
            return await self._admin_reply(stream_id, '只有管理员能用这条命令')
        session, status = self._resolve_admin_chat(chat)
        if status != 'ok':
            return await self._admin_reply(stream_id, f'无法解析会话 {chat}: {status}')
        settings = self._settings()
        current = settings.disabled_chat_set()
        if session in current:
            return await self._admin_reply(stream_id, f'{chat} 已在停用名单')
        current.add(session)
        result = await self._persist_wechat({'disabled_chats': ', '.join(sorted(current))})
        if not result['ok']:
            return await self._admin_reply(stream_id,
                                           f'保存失败: {result["error"]}（配置未生效）')
        note = '' if result['persisted'] else '（注意：写盘失败，重启后丢失）'
        return await self._admin_reply(stream_id, f'已停用 {chat}（不收不回）{note}')

    @Command('wx_enable_chat', description='启用某个会话',
             pattern=r'^.*?/wx启用\s+(?P<chat>\S+)\s*$')
    async def wx_enable_chat(self, stream_id: str = '', user_id: str = '',
                             chat: str = '', **kwargs: Any):
        if not self._is_admin(user_id):
            return await self._admin_reply(stream_id, '只有管理员能用这条命令')
        session, status = self._resolve_admin_chat(chat)
        if status != 'ok':
            return await self._admin_reply(stream_id, f'无法解析会话 {chat}: {status}')
        settings = self._settings()
        current = settings.disabled_chat_set()
        if session not in current:
            return await self._admin_reply(stream_id, f'{chat} 不在停用名单')
        current.discard(session)
        result = await self._persist_wechat({'disabled_chats': ', '.join(sorted(current))})
        if not result['ok']:
            return await self._admin_reply(stream_id,
                                           f'保存失败: {result["error"]}（配置未生效）')
        note = '' if result['persisted'] else '（注意：写盘失败，重启后丢失）'
        return await self._admin_reply(stream_id, f'已启用 {chat}{note}')

    @Command('wx_chat_mode', description='设置分群唤醒模式 hybrid|mention',
             pattern=r'^.*?/wx模式\s+(?P<chat>\S+)\s+(?P<mode>hybrid|mention)\s*$')
    async def wx_chat_mode(self, stream_id: str = '', user_id: str = '',
                           chat: str = '', mode: str = '', **kwargs: Any):
        if not self._is_admin(user_id):
            return await self._admin_reply(stream_id, '只有管理员能用这条命令')
        session, status = self._resolve_admin_chat(chat)
        if status != 'ok':
            return await self._admin_reply(stream_id, f'无法解析会话 {chat}: {status}')
        settings = self._settings()
        overrides = settings.chat_mode_overrides()
        overrides[session] = mode.lower()
        text = ', '.join(f'{k}={v}' for k, v in sorted(overrides.items()))
        result = await self._persist_wechat({'chat_modes': text})
        if not result['ok']:
            return await self._admin_reply(stream_id,
                                           f'保存失败: {result["error"]}（配置未生效）')
        note = '' if result['persisted'] else '（注意：写盘失败，重启后丢失）'
        return await self._admin_reply(stream_id, f'{chat} 唤醒模式 -> {mode}{note}')

    @Command('wx_tts_mode', description='切换 TTS 模式',
             pattern=r'^.*?/wx语音\s+(?P<mode>\S+)\s*$')
    async def wx_tts_mode(self, stream_id: str = '', user_id: str = '',
                          mode: str = '', **kwargs: Any):
        if not self._is_admin(user_id):
            return await self._admin_reply(stream_id, '只有管理员能用这条命令')
        if mode not in TTS_MODES:
            return await self._admin_reply(stream_id,
                                           '模式必须是: ' + '/'.join(TTS_MODES))
        result = await self._persist_wechat_tts({'mode': mode})
        if not result['ok']:
            return await self._admin_reply(stream_id,
                                           f'保存失败: {result["error"]}（配置未生效）')
        note = '' if result['persisted'] else '（注意：写盘失败，重启后丢失）'
        return await self._admin_reply(stream_id, f'TTS 模式 -> {mode}{note}')


def replay_pending(recovery_snapshot: dict[str, Any]) -> int:
    data = recovery_snapshot.get('replay') or {}
    return int(data.get('pending', 0))


def disabled_display(settings: Any) -> set[str]:
    return settings.disabled_chat_set()


def register_admin_commands(plugin: Any) -> None:
    """兼容保留：新实现命令在类上声明，无需运行时注册。

    保留函数只打日志，避免旧调用点报错。
    """
    log.debug('管理命令为类级声明（WX-003），无需运行时注册')
