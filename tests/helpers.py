"""合成测试基座：替身网关/reader/hook + 合成 reader payload 生产者。

所有测试使用合成账号、消息与替身收发端，绝不触达真实微信发送、
登录、进程重启或付费语音接口。push_payload 生产者从隔离副本
source/reader/reader-api.py 用 AST 提取，与审计脚本同一手法，
保证测试的入站结构与真实 reader 完全一致。
"""
from __future__ import annotations

import asyncio
import ast
import hashlib
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

SOURCE_DIR = Path(__file__).resolve().parent.parent / "source"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

READER_API = SOURCE_DIR / "reader" / "reader-api.py"

TALKER = "audit_group@chatroom"
GROUP_NAME = "AuditGroup"
NAMES = {TALKER: GROUP_NAME, "wxid_audit_alice": "Alice",
         "wxid_audit_bob": "Bob", "wxid_audit_friend": "Friend"}
OWN_WXID = "wxid_audit_bot"


class FakeGateway:
    """route_message 替身：记录调用，可编程接受与否。"""

    def __init__(self, accept: bool = True):
        self.accept = accept
        self.calls: list[dict[str, Any]] = []
        self.update_state_calls: list[dict[str, Any]] = []

    async def route_message(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        return self.accept

    async def update_state(self, *args: Any, **kwargs: Any) -> bool:
        self.update_state_calls.append({"args": args, "kwargs": kwargs})
        return True


class FakeApi:
    def __init__(self):
        self.replaced: list[list[dict[str, Any]]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def replace_dynamic_apis(self, components, offline_reason=""):
        self.replaced.append(list(components))
        return True

    async def call(self, name: str, **kwargs: Any):
        # V2-3 测试支撑：模拟 TTS 插件 synthesize 返回空音频
        self.calls.append((name, kwargs))
        return {'success': True, 'audio_base64': '', 'mime_type': 'pcm'}


class FakeReader:
    """ReaderClient 替身：合成会话表与解析。"""

    def __init__(self, own_wxid: str = OWN_WXID, account: str = "audit-account"):
        self.own_wxid = own_wxid
        self.account = account
        self.status_calls = 0
        self.base_url = "http://127.0.0.1:5031"

    def status(self) -> dict[str, Any]:
        self.status_calls += 1
        return {"ownWxid": self.own_wxid, "accountDirectory": self.account}

    def sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        out = []
        for talker, name in NAMES.items():
            is_group = talker.endswith("@chatroom")
            out.append({
                "username": talker,
                "displayName": name,
                "conversationId": _conversation_id(talker, name),
                "type": 1 if is_group else 0,
            })
        return out

    def resolve(self, target: str) -> tuple[str, str]:
        raw = str(target).strip()
        for talker, name in NAMES.items():
            if talker == raw:
                return talker, name
        for talker, name in NAMES.items():
            if name == raw:
                return talker, name
        if raw.isdigit():
            for talker, name in NAMES.items():
                if str(_conversation_id(talker, name)) == raw:
                    return talker, name
        return raw, ""


class FakeHook:
    """HookNative 替身：记录发送，可编程结果/延迟/429。"""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []
        self.result = {"ret": 0}
        self.delay = 0.0

    def _record(self, name: str, args: tuple) -> dict[str, Any]:
        self.calls.append((name, args))
        if self.delay:
            time.sleep(self.delay)
        if callable(self.result):
            return dict(self.result(*args))
        return dict(self.result)

    def send_text(self, target, text, account=""):
        return self._record("send_text", (target, text, account))

    def send_image(self, target, path, account=""):
        return self._record("send_image", (target, path, account))

    def send_file(self, target, path, md5, size, account=""):
        return self._record("send_file", (target, path, md5, size, account))

    def send_voice(self, target, path, duration, account=""):
        return self._record("send_voice", (target, path, duration, account))

    def send_sticker(self, target, path, md5, length, account=""):
        return self._record("send_sticker", (target, path, md5, length, account))

    def health(self):
        return {"integration": "WeChatAgent-1", "backend": "wechat-hook",
                "startupState": "ready"}


def _conversation_id(talker: str, name: str) -> int:
    """与 reader conversation_id 相同的稳定映射（群=群名哈希，私聊=wxid 哈希）。"""
    basis = name.strip() if talker.endswith("@chatroom") else talker
    digest = hashlib.sha256(basis.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 2147483647 + 1


def reader_conversation_id(talker: str) -> int:
    return _conversation_id(talker, NAMES.get(talker, talker))


def _load_push_payload():
    """AST 提取 push_payload，避免引入 reader 的运行时依赖。"""
    tree = ast.parse(READER_API.read_text(encoding="utf-8-sig"))
    producer = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "push_payload")
    scope: dict[str, Any] = dict(
        hashlib=hashlib, json=json, APPMSG_STICKER_TYPE=1048625,
        PAT_TYPE=922746929, FORWARD_TYPE=822083633,
        QUOTE_TYPE=244813135921, FILE_TYPE=25769803825,
        clean_account_wxid=lambda s: s, parse_file_message=lambda *a: None,
        parse_pat=lambda *a, **k: None, parse_forwarded_record=lambda *a: None,
        parse_quote_reply=lambda *a: None, sticker_image_xml=lambda *a: None)
    exec(compile(ast.Module(body=[producer], type_ignores=[]),
                 str(READER_API), "exec"), scope)
    return scope["push_payload"]


PUSH = _load_push_payload()


def reader_payload(sender: str = "wxid_audit_alice", text: str = "hello",
                   number: int = 1, talker: str = TALKER) -> dict[str, Any]:
    """真实 push_payload 逻辑产出的合成入站推送。"""
    row = dict(senderUsername=sender, localType=1, fullText=text,
               serverId=number, localId=number,
               createTime=1793462400 + number)
    payload = PUSH(talker, row, NAMES, OWN_WXID, reader_conversation_id(talker))
    assert payload is not None, "synthetic row must produce a payload"
    return payload


def private_payload(sender: str = "wxid_audit_friend", text: str = "hi",
                    number: int = 100) -> dict[str, Any]:
    return reader_payload(sender=sender, text=text, number=number,
                          talker=sender)


async def fresh_plugin(tmp_root: str, **overrides: Any):
    """构造带合成上下文的插件实例（等价审计脚本的 fresh()）。"""
    import socket

    from local_wxsupervisor.config import WxSupervisorSettings
    from local_wxsupervisor.plugin import WxSupervisorPlugin

    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    obj = WxSupervisorPlugin()
    data = WxSupervisorSettings().model_dump(mode="python")
    data["project"].update(root=str(tmp_root), python=sys.executable)
    data["reader"].update(autostart=False, watchdog=False, stop_on_unload=False)
    data["server"]["port"] = _free_port()     # 复核：测试绝不绑生产端口
    for key, value in overrides.items():
        section, _, field = key.partition("__")
        if field:
            data[section][field] = value
        else:
            data["plugin"][section] = value
    obj.set_plugin_config(data)
    obj.cfg = obj._load_config()
    obj._loop = asyncio.get_running_loop()
    gateway = FakeGateway()
    obj._ctx = SimpleNamespace(
        logger=__import__("logging").getLogger("test"),
        gateway=gateway,
        api=FakeApi(),
        send=SimpleNamespace(text=None),
    )
    obj.reader = FakeReader()
    obj.hook = FakeHook()
    obj.supervisor = None
    # 预填会话目录：别名解析测试的数据源
    obj.inbound.policy.directory.update(obj.reader.sessions())
    return obj, gateway


class ControlledClock:
    """可推进的单调时钟：注入限流器，验证上限时不真睡。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

def _find_7999_owner_pid() -> int:
    """原生 TCP 表查询：返回监听 7999 的进程 PID（无则 0）。毫秒级。"""
    import ctypes
    import ctypes.wintypes as wt

    class MIB_TCPROW_LISTENER(ctypes.Structure):
        _fields_ = [('dwState', wt.DWORD), ('dwLocalAddr', wt.DWORD),
                    ('dwLocalPort', wt.DWORD), ('dwRemoteAddr', wt.DWORD),
                    ('dwRemotePort', wt.DWORD), ('dwOwningPid', wt.DWORD)]

    class MIB_TCPTABLE_OWNER(ctypes.Structure):
        _fields_ = [('dwNumEntries', wt.DWORD),
                    ('table', MIB_TCPROW_LISTENER * 4096)]

    TABLE_CLASS_LISTENER = 3
    size = wt.DWORD(0)
    ctypes.windll.iphlpapi.GetExtendedTcpTable(
        None, ctypes.byref(size), False, 2, TABLE_CLASS_LISTENER, 0)
    buf = ctypes.create_string_buffer(size.value)
    if ctypes.windll.iphlpapi.GetExtendedTcpTable(
            buf, ctypes.byref(size), False, 2, TABLE_CLASS_LISTENER, 0) != 0:
        return 0
    table = ctypes.cast(buf, ctypes.POINTER(MIB_TCPTABLE_OWNER)).contents
    port_net = (7999 & 0xFF) << 8 | (7999 >> 8)
    for i in range(table.dwNumEntries):
        row = table.table[i]
        if row.dwLocalPort & 0xFFFF == port_net:
            return int(row.dwOwningPid)
    return 0


def assert_no_production_listeners():
    """复核要求：测试不得泄漏 OneBot 默认端口 7999 的监听。

    只把「7999 监听者是当前测试进程」判为失败（fresh_plugin 已把插件
    端口随机化，任何本进程对 7999 的绑定都来自泄漏的 server 重建）；
    生产 reader/hook 的既有监听与本测试无关。
    """
    import os as _os
    pid = _find_7999_owner_pid()
    if pid == _os.getpid():
        raise AssertionError("test leaked a OneBot listener on port 7999")


class ListenerGuardMixin:
    """每用例结束检查 7999 未被本进程绑定（后台泄漏 → 测试失败）。"""

    def _guard_ports(self):
        assert_no_production_listeners()

    def tearDown(self):
        self._guard_ports()

    async def asyncTearDown(self):
        await asyncio.sleep(0)
        self._guard_ports()