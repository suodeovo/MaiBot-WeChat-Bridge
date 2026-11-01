"""统一消息身份与会话策略（WX-001）。

reader 的真实字段（reader-api.py push_payload）：

    sessionId       原始会话 wxid（群形如 xxx@chatroom）
    talkerId        发言人 wxid（群聊、私聊都有；私聊=对方 wxid）
    groupName       群名（仅群聊；私聊为空串）
    sourceName      群聊=发言人显示名；私聊=对方显示名
    senderName      发言人显示名
    conversationId  reader 的稳定数字会话 ID（群=群名哈希，私聊=wxid 哈希）
    rawid           消息原始 ID（sha256）

旧实现的错误：读不存在的 `sender`，把 sourceName 当群名，网关再把空
sender 回退成群 ID —— 两个成员发言后 user_id 都成了群 ID。

这里在入站边界把推送规范化成 NormalizedMessage，之后白名单、停用、
唤醒、目标解析、Host 投递、OneBot 事件全部使用同一个身份。

显示名只作别名；同名群不能被合并：policy 解析别名时遇到歧义（多个
会话对应同一个显示名）返回 AMBIGUOUS，不猜测收件人。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

log = logging.getLogger('wxsupervisor.identity')

GROUP_SUFFIX = '@chatroom'


def is_group_session(session_id: str) -> bool:
    return str(session_id or '').endswith(GROUP_SUFFIX)


@dataclass(frozen=True)
class NormalizedMessage:
    """入站边界产出的统一身份结构。"""

    account: str = ''                # 机器人账号 wxid（ownWxid）
    session_id: str = ''             # 原始会话 wxid（稳定）
    conversation_id: str = ''        # reader 稳定数字 ID（兼容 OneBot 旧数字 ID）
    group_name: str = ''             # 群名（仅群聊）
    is_group: bool = False
    sender_id: str = ''              # 发言人 wxid（talkerId）
    sender_name: str = ''            # 发言人显示名
    session_display: str = ''        # 会话显示名（群名或私聊对方显示名）
    rawid: str = ''
    timestamp: float = 0.0
    text: str = ''
    payload: Mapping[str, Any] = field(default_factory=dict)   # 原始推送引用
    aliases: tuple[str, ...] = ()    # 该会话的显示别名（仅诊断/展示）

    @property
    def chat_type(self) -> str:
        return 'group' if self.is_group else 'private'


def normalize_payload(payload: Mapping[str, Any], *,
                      account: str = '') -> NormalizedMessage:
    """reader 推送 -> 统一身份。

    兼容旧字段：`sender`（OneBot 风格）在缺 talkerId 时作发言人回退；
    优先级 talkerId > sender > sessionId。
    """
    session_id = str(payload.get('sessionId') or '').strip()
    talker = str(payload.get('talkerId') or payload.get('sender') or '').strip()
    is_group = is_group_session(session_id)
    group_name = str(payload.get('groupName') or '').strip() if is_group else ''
    # 群聊的 sourceName 是发言人显示名，绝不当群名；私聊时才是对方显示名。
    source_name = str(payload.get('sourceName') or '').strip()
    sender_name = str(payload.get('senderName') or '').strip()
    if is_group:
        session_display = group_name or session_id
        sender_name = sender_name or source_name
    else:
        session_display = source_name or session_id
        if not talker:
            talker = session_id
    conv_id = str(payload.get('conversationId') or '').strip()
    ts = payload.get('timestamp')
    try:
        ts = float(ts or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    return NormalizedMessage(
        account=str(account or ''),
        session_id=session_id,
        conversation_id=conv_id,
        group_name=group_name,
        is_group=is_group,
        sender_id=talker,
        sender_name=sender_name,
        session_display=session_display,
        rawid=str(payload.get('rawid') or ''),
        timestamp=ts,
        text=str(payload.get('content') or ''),
        payload=payload,
    )


class SessionDirectory:
    """会话目录：稳定 wxid <-> 显示名/数字 ID 的映射与别名解析。

    数据来源是 reader 的 /sessions（username + displayName +
    conversationId）。同名歧义直接暴露，不猜测。
    """

    def __init__(self) -> None:
        # session_id -> {'display': str, 'conversation_id': str}
        self._by_session: dict[str, dict[str, str]] = {}
        self._display_index: dict[str, set[str]] = {}
        self._numeric_index: dict[str, str] = {}

    def update(self, sessions: Iterable[Mapping[str, Any]]) -> None:
        """全量替换目录（完整会话表来源用）。"""
        by_session: dict[str, dict[str, str]] = {}
        display_index: dict[str, set[str]] = {}
        numeric_index: dict[str, str] = {}
        for entry in sessions or []:
            username = str(entry.get('username') or '').strip()
            if not username:
                continue
            display = str(entry.get('displayName') or '').strip()
            conv = str(entry.get('conversationId') or '').strip()
            by_session[username] = {'display': display, 'conversation_id': conv}
            if display:
                display_index.setdefault(display, set()).add(username)
            if conv:
                numeric_index[conv] = username
        self._by_session = by_session
        self._display_index = display_index
        self._numeric_index = numeric_index

    def merge(self, sessions: Iterable[Mapping[str, Any]]) -> None:
        """合并更新：新条目覆盖同键旧值，其他条目保留。"""
        for entry in sessions or []:
            username = str(entry.get('username') or '').strip()
            if not username:
                continue
            display = str(entry.get('displayName') or '').strip()
            conv = str(entry.get('conversationId') or '').strip()
            old = self._by_session.get(username, {})
            self._by_session[username] = {'display': display or old.get('display', ''),
                                          'conversation_id': conv or old.get('conversation_id', '')}
            if display:
                self._display_index.setdefault(display, set()).add(username)
            if conv:
                self._numeric_index[conv] = username

    def __len__(self) -> int:
        return len(self._by_session)

    def display_of(self, session_id: str) -> str:
        return self._by_session.get(session_id, {}).get('display', '')

    def conversation_id_of(self, session_id: str) -> str:
        return self._by_session.get(session_id, {}).get('conversation_id', '')

    def resolve_alias(self, alias: str) -> tuple[str, str]:
        """把显示名/数字 ID/wxid 解析到唯一会话。

        返回 (session_id, 状态)；状态 ∈ ok | ambiguous | unknown。
        wxid（含 @chatroom）直接命中；数字 ID 走 reader 稳定映射；
        显示名唯一时命中，歧义时返回状态 ambiguous 且不猜测。
        """
        raw = str(alias or '').strip()
        if not raw:
            return '', 'unknown'
        if raw in self._by_session:
            return raw, 'ok'
        if raw.isdigit() and raw in self._numeric_index:
            return self._numeric_index[raw], 'ok'
        hits = self._display_index.get(raw)
        if not hits:
            return '', 'unknown'
        if len(hits) > 1:
            return '', 'ambiguous'
        return next(iter(hits)), 'ok'


class PolicyDecision:
    """策略判定结果：可诊断，不静默。"""

    __slots__ = ('allowed', 'reason')

    def __init__(self, allowed: bool, reason: str = '') -> None:
        self.allowed = allowed
        self.reason = reason

    def __bool__(self) -> bool:      # 便捷：if decision
        return self.allowed

    def __repr__(self) -> str:
        return f'PolicyDecision(allowed={self.allowed}, reason={self.reason!r})'


ALLOW = PolicyDecision(True)


class SessionPolicy:
    """白名单 / 停用 / 唤醒 / 目标解析共用的一份策略。

    语义（WX-001 契约）：
    * 群白名单与私聊白名单**分别**判空：某类为空表示该类不限制。
      旧实现「两类都空才放行全部」导致填了群白名单就封死私聊。
    * 白名单/停用名单条目在**配置解析边界**就解析到稳定会话 wxid；
      显示名命中歧义时该条目按「未解析」处理并在诊断中暴露。
    * 停用覆盖入站、出站、重放、回填；发送前还会重新检查。

    settings 每次经 plugin._settings() 动态读取 —— 配置热更后
    立即生效，不存在旧实例快照。
    """

    def __init__(self, plugin: Any, directory: SessionDirectory | None = None) -> None:
        self._plugin = plugin
        self.directory = directory or SessionDirectory()

    def _settings(self) -> Any:
        return self._plugin._settings()

    # ---------------------------------------------------- 配置解析

    def _split(self, raw: str) -> list[str]:
        return [x.strip() for x in str(raw or '').replace('，', ',').split(',') if x.strip()]

    def _resolve_entries(self, raw: str) -> tuple[set[str], list[str], bool]:
        """条目 -> (稳定会话 wxid 集合, 无法解析条目, 配置是否原本为空)。

        R8：区分「用户没配置」（raw 空）与「配置了但解析失败」。
        配置非空且存在无法解析条目时，调用方不得把这些条目当作放行。
        """
        resolved: set[str] = set()
        unresolved: list[str] = []
        entries = self._split(raw)
        configured = bool(entries)
        for entry in entries:
            session, status = self.directory.resolve_alias(entry)
            if status == 'ok':
                resolved.add(session)
            else:
                # R8：目录不可用/未知/歧义条目全部按「未解析」保留 ——
                # wxid 形态仍精确匹配，显示名/数字形态在 policy 判定时拒绝。
                if is_group_session(entry) or entry.startswith('wxid_'):
                    resolved.add(entry)
                else:
                    unresolved.append(entry)
        return resolved, unresolved, configured

    def whitelist_sessions(self) -> tuple[set[str], set[str], list[str], bool, bool]:
        """返回 (群解析集, 私聊解析集, 未解析条目, 群名单已配置, 私聊名单已配置)。

        R8：`configured` 区分「留空=不限」与「配置了但解析失败」。
        """
        wechat = self._settings().wechat
        groups, g_unresolved, g_configured = self._resolve_entries(
            getattr(wechat, 'whitelist_groups', '') or '')
        privates, p_unresolved, p_configured = self._resolve_entries(
            getattr(wechat, 'whitelist_private', '') or '')
        return groups, privates, g_unresolved + p_unresolved, g_configured, p_configured

    def disabled_sessions(self) -> tuple[set[str], list[str], bool]:
        """返回 (解析集, 未解析条目, 是否配置)。R8：解析失败的停用条目
        在判定时显式暴露，不悄悄失效。"""
        resolved, unresolved, configured = self._resolve_entries(
            getattr(self._settings().wechat, 'disabled_chats', '') or '')
        return resolved, unresolved, configured

    def mode_overrides(self) -> dict[str, str]:
        """分群唤醒模式，键同样解析到稳定 wxid。"""
        raw = getattr(self._settings().wechat, 'chat_modes', '') or ''
        out: dict[str, str] = {}
        for item in self._split(raw):
            chat, _, mode = item.rpartition('=')
            mode = mode.strip().lower()
            if not chat or mode not in ('hybrid', 'mention'):
                continue
            session, status = self.directory.resolve_alias(chat.strip())
            key = session if status == 'ok' else chat.strip()
            out[key] = mode
        return out

    # ---------------------------------------------------- 判定

    def disabled_for(self, msg: NormalizedMessage) -> PolicyDecision:
        disabled, unresolved, configured = self.disabled_sessions()
        if not disabled and not unresolved:
            return ALLOW
        # R8：停用配置存在但目录不可用时，按 session_id 精确匹配仍然拒绝；
        # 未解析条目不放大到全部会话（停用语义保守：只停确定命中的）。
        if msg.session_id in disabled:
            return PolicyDecision(False, f'disabled: session {msg.session_id}')
        if configured and unresolved and not disabled:
            # 配置了停用名单但一个都没解析出来：策略可能失效 —— 诊断暴露
            log.warning('停用名单条目全部未解析（检查会话目录）: %s', unresolved)
        return ALLOW

    def whitelist_for(self, msg: NormalizedMessage) -> PolicyDecision:
        groups, privates, unresolved, g_configured, p_configured = \
            self.whitelist_sessions()
        if msg.is_group:
            if not g_configured:
                return ALLOW                      # 群白名单未配置 = 群聊不限
            # R8：已配置但存在未解析条目时，未命中解析集合的一律拒绝
            # （不确定的不放行），并在诊断中暴露未解析条目。
            if msg.session_id in groups:
                return ALLOW
            reason = f'group {msg.session_id} not in group whitelist'
            if unresolved:
                reason += f'; unresolved entries: {unresolved}'
            return PolicyDecision(False, reason)
        if not p_configured:
            return ALLOW                          # 私聊白名单未配置 = 私聊不限
        if msg.session_id in privates:
            return ALLOW
        reason = f'private {msg.session_id} not in private whitelist'
        if unresolved:
            reason += f'; unresolved entries: {unresolved}'
        return PolicyDecision(False, reason)

    def allows_inbound(self, msg: NormalizedMessage) -> PolicyDecision:
        disabled = self.disabled_for(msg)
        if not disabled:
            return disabled
        return self.whitelist_for(msg)

    def allows_outbound(self, session_id: str) -> PolicyDecision:
        """出站/重放/回填共用的会话级检查（无需完整消息）。R8 语义。"""
        disabled, _unresolved, disabled_configured = self.disabled_sessions()
        if disabled and session_id in disabled:
            return PolicyDecision(False, f'disabled: session {session_id}')
        groups, privates, _u, g_configured, p_configured = \
            self.whitelist_sessions()
        if is_group_session(session_id):
            if g_configured and session_id not in groups:
                return PolicyDecision(False, 'group not whitelisted')
        else:
            if p_configured and session_id not in privates:
                return PolicyDecision(False, 'private not whitelisted')
        return ALLOW

    def wake_allows(self, msg: NormalizedMessage) -> bool:
        """唤醒判定（mention 模式才拦）。"""
        if not msg.is_group:
            return True
        overrides = self.mode_overrides()
        mode = overrides.get(msg.session_id) or \
            getattr(self._settings().wechat, 'wake_mode', 'hybrid')
        if mode != 'mention':
            return True
        words = self._settings().wake_word_list()
        if any(w and w in msg.text for w in words):
            return True
        return False


def refresh_directory(policy: SessionPolicy, reader_status_sessions) -> None:
    """用 reader 会话表刷新目录（供入站/出站前调用；失败不致命）。"""
    try:
        policy.directory.update(reader_status_sessions)
    except Exception as exc:
        log.debug('会话目录刷新失败（保留旧表）: %s', exc)
