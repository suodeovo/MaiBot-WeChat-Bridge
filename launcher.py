"""按版本拉起微信 hook 客户端。

微信 hook 是注入在微信进程里的 version.dll，它只对特定版本生效
（本机是 4.1.10.27）。启动时必须带上：

    Weixin.exe StartPort=30001 CallBackURL="http://127.0.0.1:8766/unused"

`StartPort` 决定 hook 的 HTTP 监听端口（对应配置的 hook.base_url），
`CallBackURL` 是 hook 初始化后回调的地址（我们不用它，随便给个回环即可）。

这里不复制 WeChatAgent 的 `launch-hook-wechat.ps1`（那脚本还管
dll 部署/签名校验/pin 客户端），只做「确保这个版本的微信跑起来」：

  1. 已在跑 → 直接返回，不重复启动
  2. 端口被非微信进程占用 → 拒绝启动
  3. 校验 exe 存在、版本号匹配配置
  4. 用 StartPort/CallBackURL 拉起，等 hook health 就绪
  5. 有其他版本的微信在跑 → 提示先退出（两个版本共用登录态会打架）
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger('wxsupervisor.launcher')

# 微信各版本主进程名
WECHAT_PROCESS_NAMES = ('Weixin.exe', 'WeChat.exe')


def _run_ps(command: str, timeout: float = 15.0) -> str:
    """跑一段 PowerShell，返回 stdout。失败返回空串。"""
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', command],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        return (out.stdout or '').strip()
    except Exception as exc:
        log.debug('PowerShell 执行失败: %s', exc)
        return ''


def list_wechat_processes() -> list[dict[str, Any]]:
    """列出所有微信主进程（不含 --type= 的渲染子进程）。"""
    cmd = (
        "Get-CimInstance Win32_Process -Filter \"Name='Weixin.exe' OR Name='WeChat.exe'\" | "
        "Where-Object { $_.CommandLine -notmatch '--type=' } | "
        "Select-Object ProcessId,ExecutablePath | ConvertTo-Json -Compress"
    )
    raw = _run_ps(cmd)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    items = data if isinstance(data, list) else [data]
    return [{'pid': int(x.get('ProcessId') or 0),
             'exe': str(x.get('ExecutablePath') or '')}
            for x in items if isinstance(x, dict)]


def _port_open(port: int, host: str = '127.0.0.1', timeout: float = 0.6) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _hook_health(base_url: str, token: str, timeout: float = 4.0) -> dict[str, Any]:
    """查 hook 健康状态。"""
    try:
        req = urllib.request.Request(
            base_url.rstrip('/') + '/WeChatAgent/health',
            headers={'Authorization': 'Bearer ' + token} if token else {})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception:
        return {}


def resolve_exe(project_root: str, raw: str) -> Path:
    """把配置里的微信路径解析成绝对路径（支持相对于项目根）。"""
    p = Path(raw)
    if p.is_absolute():
        return p
    return Path(project_root) / p


def file_version(exe: Path) -> str:
    """读 exe 的 FileVersion。取不到返回空串。"""
    if not exe.is_file():
        return ''
    quoted_path = "'" + str(exe).replace("'", "''") + "'"
    cmd = f'(Get-Item -LiteralPath {quoted_path}).VersionInfo.FileVersion'
    return _run_ps(cmd, timeout=10)


def open_hook_wechat(project_root: str, exe_raw: str, expected_version: str,
                     hook_base_url: str, hook_token: str,
                     wait_seconds: float = 40.0) -> dict[str, Any]:
    """按版本拉起微信 hook 客户端，并等它就绪。

    返回 {'ok': bool, 'reason': str, 'pid': int, 'version': str, 'action': str}
    """
    exe = resolve_exe(project_root, exe_raw)
    port = 0
    try:
        from urllib.parse import urlsplit
        port = urlsplit(hook_base_url).port or 80
    except Exception:
        port = 30001

    # 1) hook 已经就绪 → 什么都不用做
    health = _hook_health(hook_base_url, hook_token, timeout=2.0)
    if (health.get('integration') == 'WeChatAgent-1'
            and health.get('startupState') == 'ready'):
        return {'ok': True, 'reason': 'hook 已就绪，无需启动',
                'pid': int(health.get('pid') or 0),
                'version': str(health.get('version') or ''), 'action': 'none'}

    # 2) exe 必须存在
    if not exe.is_file():
        return {'ok': False, 'reason': f'找不到微信程序: {exe}',
                'pid': 0, 'version': '', 'action': 'failed'}

    # 3) 版本号必须匹配
    ver = file_version(exe)
    if expected_version and ver and ver != expected_version:
        return {'ok': False,
                'reason': f'版本不匹配：文件是 {ver}，配置要求 {expected_version}',
                'pid': 0, 'version': ver, 'action': 'failed'}

    # 4) 端口被非微信进程占用就拒绝（避免顶掉别人的服务）
    procs = list_wechat_processes()
    same = [p for p in procs if Path(p['exe']).resolve() == exe.resolve()]
    other = [p for p in procs if Path(p['exe']).resolve() != exe.resolve()]
    if _port_open(port) and not same:
        return {'ok': False,
                'reason': f'端口 {port} 已被非该版本的进程占用，请先确认监听者',
                'pid': 0, 'version': ver, 'action': 'failed'}

    # 5) 该版本已在跑，只是 hook 没就绪 → 等一会儿即可，不要重复拉起
    if same:
        pid = same[0]['pid']
        deadline = time.monotonic() + min(wait_seconds, 20.0)
        while time.monotonic() < deadline:
            health = _hook_health(hook_base_url, hook_token, timeout=2.0)
            if health.get('startupState') == 'ready':
                return {'ok': True, 'reason': '该版本已在运行，hook 已就绪',
                        'pid': pid, 'version': ver, 'action': 'reused'}
            time.sleep(1.0)
        return {'ok': False,
                'reason': '该版本已在运行但 hook 未就绪，请确认已扫码登录',
                'pid': pid, 'version': ver, 'action': 'pending'}

    # 6) 有别的版本在跑 → 提示先退出（共用登录态会互相干扰）
    if other:
        names = ', '.join(f"{p['exe']}(pid {p['pid']})" for p in other[:3])
        return {'ok': False,
                'reason': f'检测到其它版本微信在运行：{names}。请先从托盘完全退出再打开',
                'pid': 0, 'version': ver, 'action': 'blocked'}

    # 7) 正式拉起
    #
    # 两个坑，实测踩过：
    #   a) 不能用 `subprocess.Popen(..., close_fds=True)` —— GUI 进程会以 rc=0 秒退
    #   b) 用 ShellExecuteW 时必须显式声明 argtypes，否则 64 位下
    #      lpParameters 会被按 32 位截断，传进去的参数是垃圾，进程起不来
    args = f'StartPort={port} CallBackURL="http://127.0.0.1:8766/unused"'
    try:
        import ctypes
        from ctypes import wintypes
        shell32 = ctypes.WinDLL('shell32', use_last_error=True)
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        shell32.ShellExecuteW.argtypes = [
            wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int,
        ]
        rc = shell32.ShellExecuteW(None, 'open', str(exe), args,
                                   str(exe.parent), 1)
        # 返回值 >32 才算成功（<=32 是错误码）
        if int(rc or 0) <= 32:
            return {'ok': False,
                    'reason': f'启动失败（ShellExecute 返回 {rc}）',
                    'pid': 0, 'version': ver, 'action': 'failed'}
    except Exception as exc:
        return {'ok': False, 'reason': f'启动异常: {exc}',
                'pid': 0, 'version': ver, 'action': 'failed'}

    log.info('已拉起微信 %s (StartPort=%s)', exe, port)

    # 8) 等进程出现并让 hook 就绪（ShellExecute 不给 pid，回头查）
    pid = 0
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        procs = [p for p in list_wechat_processes()
                 if Path(p['exe']).resolve() == exe.resolve()]
        if procs and not pid:
            pid = procs[0]['pid']
            log.info('微信已启动 pid=%s', pid)

        health = _hook_health(hook_base_url, hook_token, timeout=2.0)
        if health.get('startupState') == 'ready':
            return {'ok': True, 'reason': '已拉起并完成 hook 初始化，请扫码登录',
                    'pid': int(health.get('pid') or pid),
                    'version': str(health.get('version') or ver),
                    'action': 'started'}
        time.sleep(1.0)

    if pid:
        return {'ok': True,
                'reason': '已拉起微信，等你在客户端里扫码登录（hook 稍后就绪）',
                'pid': pid, 'version': ver, 'action': 'started-pending'}

    return {'ok': False,
            'reason': '已调用启动但没检测到微信进程，请检查路径与权限',
            'pid': 0, 'version': ver, 'action': 'failed'}
