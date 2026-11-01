"""微信链路进程守护：启停编排 + 崩溃自愈 + 归属校验。

设计参照 WeChatAgent 自带的 `scripts/manage.ps1` 与 `scripts/process-recovery.mjs`：

  * 每个被托管的进程都落一条记录到 state/runtime.json
        {pid, exe, marker, startedAt}
  * 停止进程前必须校验归属——pid 复用是真实存在的风险，只凭 pid
    去 kill 有可能杀掉无关进程。校验条件是三选二：
        1) pid 还活着
        2) ExecutablePath 与记录一致
        3) CommandLine 里含记录的 marker（脚本绝对路径）
    任一条对不上就丢弃这条记录，不杀。
  * marker 必须落在项目根目录内，避免误管别的安装。

守护对象（按依赖顺序启动）：

    reader-api.py      python  127.0.0.1:5031   微信数据库读取 + SSE 推送
    bridge(social)     node    127.0.0.1:11229  WeChatAgent 社交引擎（可选）
    adapter            node    127.0.0.1:8766   OneBot 适配层（连 11229）
    hook 客户端        Weixin  127.0.0.1:30001  原生发送，需人工扫码登录

本模块只管 reader（必须）与 hook 健康检查（不代登录）；
adapter/social-bridge 由 WeChatAgent 的 manage.ps1 管，避免双重托管打架。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _port_open(port: int, host: str = '127.0.0.1', timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _process_info(pid: int) -> dict[str, str] | None:
    """读进程的 ExecutablePath / CommandLine。取不到返回 None。

    用 CIM 而不是 psutil：MaiBot 的运行环境不一定装了 psutil，
    CIM 是 Windows 自带能力。
    """
    if not pid:
        return None
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command',
             f'$p=Get-CimInstance Win32_Process -Filter "ProcessId={int(pid)}";'
             'if($p){@{exe=[string]$p.ExecutablePath;cmd=[string]$p.CommandLine}|ConvertTo-Json -Compress}'],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    except Exception:
        return None
    raw = (out.stdout or '').strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return {'exe': data.get('exe') or '', 'cmd': data.get('cmd') or ''}


@dataclass
class ManagedProcess:
    """一个被托管进程的规格。"""
    name: str
    exe: str
    args: list[str]
    cwd: str
    marker: str              # 命令行里必须出现的特征串（通常是脚本绝对路径）
    port: int
    visible: bool = False
    required: bool = True
    env: dict[str, str] = field(default_factory=dict)


class ProcessSupervisor:
    """进程守护器：启动、自愈、归属校验后停止。"""

    def __init__(self, root: str, state_dir: str | None = None) -> None:
        self.root = Path(root).resolve()
        self.state_dir = Path(state_dir).resolve() if state_dir else self.root / 'state'
        self.record_file = self.state_dir / 'supervisor-runtime.json'
        self.log_dir = self.state_dir / 'logs'
        self._lock = threading.RLock()
        self._procs: dict[str, subprocess.Popen[Any]] = {}
        self._records: dict[str, dict[str, Any]] = {}
        self._load_records()

    # ---------------------------------------------------------------- 记录

    def _load_records(self) -> None:
        try:
            if self.record_file.exists():
                data = json.loads(self.record_file.read_text(encoding='utf-8-sig'))
                if isinstance(data, dict):
                    self._records = data
        except Exception:
            self._records = {}

    def _save_records(self) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.record_file.with_suffix('.tmp')
            tmp.write_text(json.dumps(self._records, indent=2, ensure_ascii=False), encoding='utf-8')
            tmp.replace(self.record_file)
        except Exception:
            pass

    def _marker_in_root(self, marker: str) -> bool:
        """marker 必须落在项目根内，否则拒管——防止误操作别的安装。"""
        try:
            resolved = Path(marker).resolve()
            resolved.relative_to(self.root)
            return True
        except (ValueError, OSError):
            return False

    def _owned(self, name: str, *, allow_stale: bool = True) -> bool:
        """校验记录里的进程是否仍归我们管。"""
        rec = self._records.get(name)
        if not isinstance(rec, dict):
            return False
        pid = rec.get('pid')
        marker = rec.get('marker') or ''
        if not isinstance(pid, int) or not marker:
            if allow_stale:
                self._records.pop(name, None)
                self._save_records()
            return False
        if not self._marker_in_root(marker):
            # 记录来自别处，直接丢弃，不碰那个进程
            self._records.pop(name, None)
            self._save_records()
            return False

        info = _process_info(pid)
        if info is None:
            if allow_stale:
                self._records.pop(name, None)
                self._save_records()
            return False

        exe_ok = bool(rec.get('exe')) and info['exe'].lower() == str(rec['exe']).lower()
        marker_ok = marker.lower() in info['cmd'].lower()
        if not (exe_ok and marker_ok):
            # pid 被复用或进程已换，丢弃记录，不杀
            if allow_stale:
                self._records.pop(name, None)
                self._save_records()
            return False
        return True

    # ---------------------------------------------------------------- 状态

    def status(self, spec: ManagedProcess) -> dict[str, Any]:
        alive = self._owned(spec.name)
        return {
            'name': spec.name,
            'port': spec.port,
            'listening': _port_open(spec.port),
            'owned': alive,
            'pid': self._records.get(spec.name, {}).get('pid') if alive else 0,
        }

    # ---------------------------------------------------------------- 启动

    def ensure_running(self, spec: ManagedProcess, *, wait_seconds: float = 25.0) -> tuple[bool, str]:
        """确保进程在跑。已在跑则复用；否则拉起并等端口就绪。"""
        with self._lock:
            if _port_open(spec.port) and self._owned(spec.name):
                return True, 'already-running'

            # 端口被别的进程占用：不抢，直接报错
            if _port_open(spec.port) and not self._owned(spec.name):
                return False, f'PORT_IN_USE port={spec.port}'

            exe = spec.exe
            if not os.path.exists(exe):
                return False, f'EXE_MISSING {exe}'
            if not os.path.exists(spec.marker):
                return False, f'MARKER_MISSING {spec.marker}'

            self.log_dir.mkdir(parents=True, exist_ok=True)
            out_path = self.log_dir / f'{spec.name}.stdout.log'
            err_path = self.log_dir / f'{spec.name}.stderr.log'

            env = os.environ.copy()
            env.update(spec.env)

            creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if not spec.visible else 0
            try:
                out = open(out_path, 'ab')
                err = open(err_path, 'ab')
                proc = subprocess.Popen(
                    [exe, *spec.args],
                    cwd=spec.cwd,
                    env=env,
                    stdout=out,
                    stderr=err,
                    creationflags=creationflags,
                )
            except OSError as exc:
                return False, f'SPAWN_FAILED {exc}'

            self._procs[spec.name] = proc
            self._records[spec.name] = {
                'pid': proc.pid,
                'exe': exe,
                'marker': spec.marker,
                'startedAt': int(time.time() * 1000),
            }
            self._save_records()

            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    tail = self._tail(err_path)
                    return False, f'EXITED rc={proc.returncode} {tail}'
                if _port_open(spec.port):
                    return True, 'started'
                time.sleep(0.25)

            return False, f'START_TIMEOUT port={spec.port}'

    def _tail(self, path: Path, limit: int = 600) -> str:
        try:
            data = path.read_bytes()[-limit:]
            return data.decode('utf-8', 'replace').strip().replace('\n', ' | ')
        except Exception:
            return ''

    # ---------------------------------------------------------------- 停止

    def stop(self, spec: ManagedProcess) -> tuple[bool, str]:
        """校验归属后再停。不属于我们的进程一律不碰。"""
        with self._lock:
            if not self._owned(spec.name):
                return True, 'not-owned'

            rec = self._records.get(spec.name) or {}
            pid = rec.get('pid')
            proc = self._procs.pop(spec.name, None)

            try:
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                elif isinstance(pid, int) and pid > 0:
                    subprocess.run(
                        ['taskkill', '/PID', str(pid), '/T', '/F'],
                        capture_output=True, timeout=15,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                    )
            except Exception as exc:
                return False, f'STOP_FAILED {exc}'

            self._records.pop(spec.name, None)
            self._save_records()
            return True, 'stopped'

    def reap_dead(self) -> list[str]:
        """清理已退出进程的记录，返回被清理的名字。"""
        with self._lock:
            dead = []
            for name in list(self._records.keys()):
                if not self._owned(name):
                    dead.append(name)
            return dead
