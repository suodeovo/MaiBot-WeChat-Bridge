"""OneBot v11 WS 服务端：直接对接微信 hook 的原生 HTTP 接口。

## 为什么是服务端

WeChatAgent 自带的 `hook-onebot.mjs`（adapter）是 OneBot **客户端**——
它 `new WebSocket(ws://127.0.0.1:11229/ws)` 主动连 social-bridge。
MaiBot 的适配器插件也是客户端。两个客户端无法直接对接，
所以这里在插件内起一个 OneBot 服务端，让 MaiBot 连进来。

## 为什么不用 UIA

adapter 的发送走 hook 原生 HTTP：

    POST http://127.0.0.1:30001/SendTextMsg
    Authorization: Bearer <native-token>
    X-WeChatAgent-Account: <accountDirectory>
    {"wxidorgid": "<群ID或wxid>", "msg": "<文本>"}
    -> {"ret": 0}   0 = 已接受

这是注入在微信进程里的 C++ hook 直接调内部发送，**不模拟键盘、不抢前台、
窗口最小化也能发**。UIA 那套（ChatInputField + WM_CHAR + sw_minimize）
在本链路完全不需要。

## 消息接收

reader-api（5031）暴露 SSE：

    GET /api/v1/push/messages?access_token=<token>   (也可用 Authorization 头)
    data: {"timestamp":..,"sessionId":..,"sessionType":"group|private",
           "sourceName":..,"content":..,"sender":..,"senderName":..,...}

拿到后转成 OneBot v11 `post_type=message` 事件推给所有连入的 MaiBot 客户端。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import ssl
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable

log = logging.getLogger('wxsupervisor.onebot')


def onebot_id(value: str) -> int:
    """把 wxid / 群 id 稳定映射成 OneBot 的 int id。

    与 WeChatAgent 的 `onebotId()` 保持同一算法，避免两边 id 对不上：
        sha256(value) 前 8 字节大端 % 2147483647 + 1
    """
    digest = hashlib.sha256(str(value).encode('utf-8')).digest()
    return int.from_bytes(digest[:8], 'big') % 2147483647 + 1


def conversation_id(wxid: str, display_name: str = '') -> int:
    """会话的 OneBot id。

    群聊用「群名」算（微信群的 wxid 形如 xxx@chatroom，改群名后 wxid 不变
    但用户看到的群名会变，MaiBot 侧按群名更直观）；私聊用 wxid。
    """
    if str(wxid).endswith('@chatroom'):
        basis = (display_name or wxid).strip() or wxid
        return onebot_id(basis)
    return onebot_id(wxid)


def _http_json(url: str, *, headers: dict[str, str] | None = None,
               body: dict[str, Any] | None = None, timeout: float = 6.0) -> Any:
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        hdrs['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=hdrs,
                                 method='POST' if data is not None else 'GET')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if not raw:
        return None
    return json.loads(raw.decode('utf-8'))


class HookNative:
    """微信 hook 的原生发送接口。"""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip('/')
        self.token = token

    def _headers(self, account: str = '') -> dict[str, str]:
        h = {'Authorization': 'Bearer ' + self.token}
        if account:
            h['X-WeChatAgent-Account'] = account
        return h

    def health(self) -> dict[str, Any]:
        return _http_json(self.base_url + '/WeChatAgent/health',
                          headers=self._headers(), timeout=5.0) or {}

    def send_text(self, target: str, text: str, account: str = '') -> dict[str, Any]:
        """发文本。target 是群 id（xxx@chatroom）或好友 wxid。"""
        return _http_json(self.base_url + '/SendTextMsg',
                          headers=self._headers(account),
                          body={'wxidorgid': target, 'msg': text},
                          timeout=20.0) or {}

    def send_image(self, target: str, image_path: str, account: str = '') -> dict[str, Any]:
        """发图片（普通图片，不是表情）。

        hook 的 /SendImgMsg 只收本机文件路径：
            {"wxidorgid": ..., "path": "..."}  ->  {"ret": 0}
        支持 .jpg/.jpeg/.png/.gif/.webp，扩展名必须对得上实际格式。
        """
        return _http_json(self.base_url + '/SendImgMsg',
                          headers=self._headers(account),
                          body={'wxidorgid': target, 'path': str(image_path)},
                          timeout=45.0) or {}

    def send_file(self, target: str, file_path: str, md5: str, size: int,
                  account: str = '') -> dict[str, Any]:
        """发文件。

        路径必须已在 <project_root>/state/files/outgoing/<sha256>/<name>，
        md5 / size 与真实文件一致，否则 hook 只回 {"ret":1} 不给原因。
        """
        return _http_json(self.base_url + '/SendFileMsg',
                          headers=self._headers(account),
                          body={
                              'wxidorgid': target,
                              'path': str(file_path),
                              'md5': md5,
                              'size': int(size),
                          },
                          timeout=90.0) or {}

    def send_voice(self, target: str, silk_path: str, duration_ms: int,
                   account: str = '') -> dict[str, Any]:
        """发语音。

        端点是对外的 /WeChatAgent/voice-send（不是内部的 /SendVoiceMsg）。
        文件要是 SILK v3，path 用正斜杠，总闸 state/hook/voice-live.enable 要存在。
        返回 accepted_unconfirmed 属正常：语音异步投递，以 reader 回执为准。
        """
        return _http_json(self.base_url + '/WeChatAgent/voice-send',
                          headers=self._headers(account),
                          body={
                              'wxidorgid': target,
                              'path': str(silk_path).replace('\\', '/'),
                              'durationMs': int(duration_ms),
                          },
                          timeout=60.0) or {}

    def send_sticker(self, target: str, gif_path: str, md5: str,
                     length: int, account: str = '') -> dict[str, Any]:
        """发微信原生表情。

        hook 对文件位置与字段有严格校验（详见 sticker.py 模块注释）：
        gif 必须已在 <project_root>/state/stickers/outgoing/<md5>.gif，
        md5 是文件真实 md5，length 是精确字节数，且这些字段要传 int。
        """
        return _http_json(self.base_url + '/SendStickerMsg',
                          headers=self._headers(account),
                          body={
                              'wxidorgid': target,
                              'sticker': {
                                  'md5': md5,
                                  'productid': '',
                                  'len': int(length),
                                  'type': 2,
                                  'width': 200,
                                  'height': 200,
                              },
                              'path': str(gif_path),
                          },
                          timeout=30.0) or {}


class ReaderClient:
    """reader-api 的读取接口。"""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip('/')
        self.token = token

    def _get(self, path: str, timeout: float = 10.0) -> Any:
        return _http_json(self.base_url + path,
                          headers={'Authorization': 'Bearer ' + self.token},
                          timeout=timeout)

    def status(self) -> dict[str, Any]:
        return self._get('/api/v1/reader/status') or {}

    def sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        data = self._get(f'/api/v1/sessions?limit={int(limit)}') or {}
        return data.get('sessions') or []

    def resolve(self, target: str) -> tuple[str, str]:
        """把 OneBot 的群 id / 用户名解析成 (wxid, 显示名)。

        MaiBot 发消息时可能给：
          - 群名（例如 "洛克王国摸鱼群"）
          - 数字 group_id（conversation_id 算出来的）
          - 原始 xxx@chatroom
          - 好友昵称 / wxid
        这里按会话列表逐一比对，命中即返回。
        """
        raw = str(target).strip()
        if not raw:
            return '', ''

        sessions = self.sessions()
        # 1) 直接就是 wxid
        for s in sessions:
            if str(s.get('username') or '') == raw:
                return raw, str(s.get('displayName') or '')
        # 2) 群名精确匹配
        for s in sessions:
            if str(s.get('displayName') or '').strip() == raw:
                return str(s.get('username') or ''), raw
        # 3) 数字 id：按 conversation_id 反算
        if raw.isdigit():
            want = int(raw)
            for s in sessions:
                username = str(s.get('username') or '')
                name = str(s.get('displayName') or '')
                if conversation_id(username, name) == want:
                    return username, name
            # 4) 也可能是 WeFlow 给的 conversationId
            for s in sessions:
                if str(s.get('conversationId') or '') == raw:
                    return str(s.get('username') or ''), str(s.get('displayName') or '')
        return raw, ''


class OneBotServer:
    """极简 OneBot v11 WS 服务端，只实现 MaiBot 适配器会用到的接口。"""

    def __init__(self, host: str, port: int, token: str,
                 hook: HookNative, reader: ReaderClient,
                 get_account: Callable[[], str],
                 send_outbound: Callable[..., Any] | None = None) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.hook = hook
        self.reader = reader
        self.get_account = get_account
        # R9：统一出站入口（plugin 构造时传 self.send_outbound）。
        # 未接线时 _send 保留旧直发路径会绕过策略 —— 改为明确报错，
        # 由调用方必须接线（plugin._start_server 传入）。
        self.send_outbound = send_outbound
        self._server: Any = None
        self._clients: set[Any] = set()
        self._send_lock = asyncio.Lock()

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        import websockets
        from websockets.asyncio.server import serve

        async def _handler(ws):
            peer = getattr(ws, 'remote_address', None)
            if not self._token_ok(ws):
                # 401 由底层拒绝，这里直接关
                await ws.close(code=1008, reason='Unauthorized')
                return
            self._clients.add(ws)
            log.info('[OB11] MaiBot 客户端已连接: %s', peer)
            try:
                await ws.send(json.dumps({
                    'time': int(asyncio.get_event_loop().time()),
                    'self_id': self._self_id(),
                    'post_type': 'meta_event',
                    'meta_event_type': 'lifecycle',
                    'sub_type': 'connect',
                }, ensure_ascii=False))
                async for raw in ws:
                    await self._on_request(ws, raw)
            except Exception as exc:
                log.debug('[OB11] 连接异常: %s', exc)
            finally:
                self._clients.discard(ws)
                log.info('[OB11] 剩余客户端: %d', len(self._clients))

        self._server = await serve(_handler, self.host, self.port,
                                   max_size=16 * 1024 * 1024,
                                   process_request=self._process_request)
        log.info('[OB11] OneBot WS 服务端监听 %s:%s', self.host, self.port)

    def _process_request(self, connection: Any, request: Any) -> Any:
        """握手阶段校验 token（Authorization 头或 ?access_token=）。"""
        if not self.token:
            return None
        try:
            auth = request.headers.get('Authorization', '') or ''
        except Exception:
            auth = ''
        supplied = auth.removeprefix('Bearer ').strip()
        if not supplied:
            path = getattr(request, 'path', '') or ''
            if '?' in path:
                from urllib.parse import parse_qs
                supplied = (parse_qs(path.split('?', 1)[1]).get('access_token') or [''])[0]
        if supplied != self.token:
            from websockets.http11 import Response
            from websockets.datastructures import Headers
            return Response(401, 'Unauthorized',
                            Headers([('Connection', 'close')]), b'Unauthorized')
        return None

    def _token_ok(self, ws: Any) -> bool:
        return True  # 已在握手阶段校验

    async def stop(self) -> None:
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

    # ------------------------------------------------------------ 推送

    def is_active(self) -> bool:
        # G3: lifecycle state used by config rollback; False after stop()
        return self._server is not None

    def _self_id(self) -> int:
        """OneBot self_id，带 10s 缓存（WX-009：不再逐消息查询 reader）。"""
        now = asyncio.get_event_loop().time()
        cached, at = getattr(self, '_self_id_cache', (None, 0.0))
        if cached is not None and now - at < 10.0:
            return cached
        try:
            st = self.reader.status()
            value = onebot_id(st.get('ownWxid') or 'wechat-bot')
        except Exception:
            value = cached if cached is not None else onebot_id('wechat-bot')
        self._self_id_cache = (value, now)
        return value

    async def broadcast(self, payload: dict[str, Any]) -> int:
        if not self._clients:
            return 0
        text = json.dumps(payload, ensure_ascii=False)
        async with self._send_lock:
            sent = 0
            for ws in list(self._clients):
                try:
                    await ws.send(text)
                    sent += 1
                except Exception:
                    self._clients.discard(ws)
            return sent

    async def push_message(self, event: dict[str, Any]) -> int:
        """把 reader 的一条消息转成 OneBot v11 message 事件并推送。"""
        return await self.broadcast(event)

    # ------------------------------------------------------------ 请求分发

    async def _on_request(self, ws: Any, raw: Any) -> None:
        try:
            req = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(req, dict):
            return
        action = str(req.get('action') or '')
        params = req.get('params') or {}
        echo = req.get('echo', '')
        try:
            data = await self._dispatch(action, params)
            resp = {'status': 'ok', 'retcode': 0, 'data': data, 'echo': echo}
        except Exception as exc:
            log.warning('[OB11] %s 失败: %s', action, exc)
            resp = {'status': 'failed', 'retcode': 1, 'data': {},
                    'echo': echo, 'message': str(exc)}
        try:
            await ws.send(json.dumps(resp, ensure_ascii=False))
        except Exception:
            pass

    async def _dispatch(self, action: str, params: dict[str, Any]) -> Any:
        loop = asyncio.get_running_loop()

        if action == 'get_login_info':
            st = await loop.run_in_executor(None, self.reader.status)
            wxid = st.get('ownWxid') or ''
            return {'user_id': onebot_id(wxid or 'wechat-bot'),
                    'nickname': self._nickname(st)}

        if action == 'get_status':
            return {'online': True, 'good': True}

        if action in ('get_group_list', 'get_friend_list'):
            return await loop.run_in_executor(None, self._list_sessions, action)

        if action in ('send_msg', 'send_private_msg', 'send_group_msg'):
            return await self._send(action, params)

        if action in ('get_group_info', 'get_group_member_info',
                      'get_stranger_info', 'get_group_member_list'):
            # MaiBot 适配器对缺失信息是容忍的，返回空结构即可
            gid = params.get('group_id') or params.get('user_id') or 0
            return {'group_id': gid, 'group_name': str(gid), 'user_id': gid}

        raise ValueError(f'unsupported action: {action}')

    def _nickname(self, st: dict[str, Any]) -> str:
        name = str(st.get('accountDirectory') or '') or 'wechat-bot'
        return name

    def _list_sessions(self, action: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for s in self.reader.sessions():
            username = str(s.get('username') or '')
            name = str(s.get('displayName') or '')
            is_group = username.endswith('@chatroom')
            if action == 'get_group_list' and not is_group:
                continue
            if action == 'get_friend_list' and is_group:
                continue
            cid = conversation_id(username, name)
            if is_group:
                out.append({'group_id': cid, 'group_name': name or username})
            else:
                out.append({'user_id': cid, 'nickname': name or username})
        return out

    async def _send(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """OneBot 发送（R9）：统一走插件的 send_outbound。

        白名单/停用/限流/幂等与 MaiBot 网关共用同一入口；这里只把
        OneBot 的 action/params 映射成 message_dict。
        """
        outbound = getattr(self, 'send_outbound', None)
        if outbound is None:
            raise ValueError('unified outbound entry not wired')

        if action == 'send_group_msg':
            target = params.get('group_id')
            message_type = 'group'
        elif action == 'send_private_msg':
            target = params.get('user_id')
            message_type = 'private'
        else:
            message_type = str(params.get('message_type') or 'private')
            target = params.get('group_id') if message_type == 'group' else params.get('user_id')

        if target in (None, '', 0):
            raise ValueError('missing target id')

        segments = params.get('message') or params.get('messages') or []
        message: dict[str, Any] = {
            'raw_message': segments if isinstance(segments, list) else
            [{'type': 'text', 'data': {'text': str(segments)}}],
        }
        route = {'group_id' if message_type == 'group' else 'user_id': str(target),
                 'wemai_chat_type': message_type}
        result = await outbound(message, route=route,
                                metadata={'source': 'onebot'}, source='onebot')
        if not result.get('success'):
            raise ValueError(f'send failed: {result.get("error")}')

        wxid = str((result.get('metadata') or {}).get('target') or target)
        return {
            'message_id': onebot_id(f'{wxid}:{asyncio.get_event_loop().time()}'),
            'target': wxid,
            'target_name': (result.get('metadata') or {}).get('target_name'),
            'message_type': message_type,
        }

    @staticmethod
    def _segments_to_text(segments: Iterable[Any]) -> str:
        """OneBot 消息段 -> 纯文本。hook 的 SendTextMsg 只收文本。"""
        if isinstance(segments, str):
            return segments
        parts: list[str] = []
        for seg in segments or []:
            if not isinstance(seg, dict):
                continue
            stype = str(seg.get('type') or '')
            data = seg.get('data') or {}
            if stype == 'text':
                parts.append(str(data.get('text') or ''))
            elif stype == 'at':
                name = str(data.get('name') or data.get('qq') or '')
                parts.append('@' + name if name else '')
            elif stype == 'face':
                parts.append('[表情]')
            elif stype == 'image':
                parts.append('[图片]')
            elif stype == 'reply':
                continue          # 引用段无意义，跳过
            else:
                parts.append('[%s]' % stype)
        return ''.join(parts)
