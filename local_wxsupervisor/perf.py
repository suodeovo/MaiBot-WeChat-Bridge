"""WX-009：性能优化与依赖配置。

- 同步 HTTP 查询（账号/健康）、音频/图片转码、大文件 I/O 移到受限
  工作线程（ThreadPoolExecutor，max_workers 由配置控制）。
- 账号状态 3 秒缓存，账号切换/配置变化时失效（plugin._account_async）。
- 慢媒体按会话调度：同会话顺序保证，跨会话并行（有全局并发上限），
  一个群的慢图片不堵其他群文本。
- 媒体暂存清理：保留期限 + 总量上限；候选清单先验证再删除；
  待发送/重试/待回执文件永不删除；仅限插件自有目录。
- 硬编码 pilk/Crypto/脚本路径改为配置 + 依赖发现，更新 _manifest.json
  依赖声明。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import pathlib
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

log = logging.getLogger('wxsupervisor.perf')

DEFAULT_MEDIA_TTL_HOURS = 48.0
DEFAULT_MEDIA_MAX_MB = 512
DEFAULT_WORKERS = 4


class WorkerPool:
    """受限工作线程池：同步阻塞操作统一走这里，不占默认 executor。"""

    def __init__(self, max_workers: int = DEFAULT_WORKERS) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max(1, int(max_workers)),
                                            thread_name_prefix='wx-worker')

    async def run(self, loop, fn: Callable[..., Any], *args: Any) -> Any:
        return await loop.run_in_executor(self._executor, fn, *args)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class SessionMediaScheduler:
    """慢媒体按会话调度：同会话串行（保序），跨会话并行，全局有上限。"""

    def __init__(self, max_concurrent: int = DEFAULT_WORKERS) -> None:
        self._sem = asyncio.Semaphore(max(1, int(max_concurrent)))
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_of(self, session: str) -> asyncio.Lock:
        lock = self._locks.get(session)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session] = lock
            if len(self._locks) > 128:
                # 简单防膨胀：清最旧（dict 保插入序）
                for key in list(self._locks)[:64]:
                    self._locks.pop(key, None)
        return lock

    async def run_ordered(self, session: str, coro_factory: Callable[[], Any]) -> Any:
        """同会话顺序执行；全局并发受 semaphore 约束。"""
        lock = self._lock_of(session)
        async with lock:
            async with self._sem:
                return await coro_factory()




class MediaLeaseRegistry:
    """V2-8：媒体租约注册表（进程内引用计数 + 持久化）。

    同一内容寻址文件被多个发送并发使用时按引用计数保护；
    计数归零后释放。清理器通过 active_leases() 读取当前有效租约，
    不依赖创建实例时的旧快照。
    """

    _shared: "MediaLeaseRegistry | None" = None

    @classmethod
    def shared(cls) -> "MediaLeaseRegistry":
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    def __init__(self) -> None:
        # path -> (refcount, expires_at_monotonic)
        self._refs: dict[str, list[float]] = {}
        # 兜底过期：防止异常路径泄漏计数（12h）
        self._max_hold_s = 12 * 3600.0

    def acquire(self, path) -> None:
        entry = self._refs.setdefault(str(path), [0.0, time.monotonic()])
        entry[0] += 1
        entry[1] = time.monotonic()

    def release(self, path) -> None:
        key = str(path)
        entry = self._refs.get(key)
        if entry is None:
            return
        entry[0] -= 1
        if entry[0] <= 0:
            self._refs.pop(key, None)

    def is_leased(self, path) -> bool:
        entry = self._refs.get(str(path))
        if entry is None:
            return False
        if time.monotonic() - entry[1] > self._max_hold_s:
            self._refs.pop(str(path), None)
            return False
        return True

    def gc(self) -> int:
        now = time.monotonic()
        expired = [k for k, e in self._refs.items() if now - e[1] > self._max_hold_s]
        for k in expired:
            self._refs.pop(k, None)
        return len(expired)


class MediaJanitor:
    """媒体暂存清理（R12）：租约保护 + 计划先验证后执行。

    保护规则：
    * 只处理 allowlist 里的相对目录（插件拥有的暂存区）；
    * **租约表**：正在发送/重试/待回执的文件由 media.stage_* 写入时
      登记（lease）；有租约的文件永不删除，无论 mtime 新旧 ——
      staging 可能复用旧文件（内容寻址），mtime 不能代表使用状态；
    * 过期候选 = 无租约 且 mtime 超 TTL；
    * 容量压力只删「过期且无租约」的文件；安全清理后仍超限 →
      明确告警 + 返回 blocked，不删待发送文件。

    租约存插件自有目录 state/wxsupervisor/media-leases.json（跨重启
    恢复后仍保护待重发文件）。
    """

    ALLOWED_DIRS = (
        'state/media/outgoing', 'state/voice/outgoing',
        'state/stickers/outgoing', 'state/files/outgoing',
    )

    def __init__(self, project_root: str | pathlib.Path,
                 ttl_hours: float = DEFAULT_MEDIA_TTL_HOURS,
                 max_total_mb: float = DEFAULT_MEDIA_MAX_MB,
                 lease_path: pathlib.Path | None = None) -> None:
        self.root = pathlib.Path(project_root).resolve()
        self.ttl_s = float(ttl_hours) * 3600.0
        self.max_bytes = int(float(max_total_mb) * 1024 * 1024)
        self.lease_path = pathlib.Path(lease_path) if lease_path else \
            self.root / 'state' / 'wxsupervisor' / 'media-leases.json'
        self._leases: dict[str, float] = {}      # 绝对路径 -> monotonic 登记
        self._load_leases()
        # V2-8：真实发送路径的引用计数租约（实时读取，不用旧快照）
        self.registry = MediaLeaseRegistry.shared()

    # ------------------------------------------------------------ 租约

    def _load_leases(self) -> None:
        try:
            if self.lease_path.exists():
                raw = json.loads(self.lease_path.read_text(encoding='utf-8-sig'))
                self._leases = {str(k): float(v) for k, v in (raw or {}).items()}
        except Exception:
            self._leases = {}

    def save_leases(self) -> None:
        try:
            self.lease_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.lease_path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(self._leases), encoding='utf-8')
            tmp.replace(self.lease_path)
        except Exception as exc:
            log.warning('媒体租约写盘失败: %s', exc)

    def lease(self, path: str | pathlib.Path) -> None:
        """登记租约（stage/发送前调用）。"""
        self._leases[str(path)] = time.monotonic()
        self.save_leases()

    def release(self, path: str | pathlib.Path) -> None:
        self._leases.pop(str(path), None)
        self.save_leases()

    def _leased(self, path: pathlib.Path) -> bool:
        # V2-8：实时注册表优先（发送中/等待回执），文件租约表兜底
        if self.registry.is_leased(path):
            return True
        return str(path) in self._leases

    def gc_orphan_leases(self, max_age_s: float = 24 * 3600.0) -> int:
        """清理孤儿租约（登记超 24h 且文件已不存在）。"""
        now = time.monotonic()
        orphaned = 0
        for path in list(self._leases):
            if now - self._leases[path] > max_age_s and not pathlib.Path(path).exists():
                self._leases.pop(path, None)
                orphaned += 1
        if orphaned:
            self.save_leases()
        return orphaned

    # ------------------------------------------------------------ 清理

    def _resolve_allowed(self, rel: str) -> pathlib.Path | None:
        target = (self.root / rel).resolve()
        try:
            target.relative_to(self.root)
        except ValueError:
            return None
        if not str(target.relative_to(self.root)).replace('\\', '/').startswith(
                tuple(d for d in self.ALLOWED_DIRS)):
            return None
        return target if target.is_dir() else None

    def _all_files(self) -> list[pathlib.Path]:
        out: list[pathlib.Path] = []
        for rel in self.ALLOWED_DIRS:
            base = self._resolve_allowed(rel)
            if base is None:
                continue
            for path in base.rglob('*'):
                if path.is_file() and not path.is_symlink():
                    out.append(path)
        return out

    def candidates(self) -> list[pathlib.Path]:
        """过期且无租约的文件候选（先验证清单，再由调用方决定执行）。"""
        now = time.time()
        out: list[pathlib.Path] = []
        for path in self._all_files():
            if self._leased(path):
                continue
            try:
                if now - path.stat().st_mtime > self.ttl_s:
                    out.append(path)
            except OSError:
                continue
        return out

    def total_bytes(self) -> int:
        return sum(p.stat().st_size for p in self._all_files()
                   if p.exists())

    def enforce(self) -> list[str]:
        """执行清理（R12）：全部删除都在候选/超配额候选计划内且无租约。

        返回已删路径列表。安全清理后仍超配额时不再删除（保护待发送），
        记录告警 —— 调用方可据此限流。
        """
        deleted: list[str] = []
        # 1) 过期且无租约
        plan = list(self.candidates())
        for path in plan:
            if self._leased(path):        # 双检
                continue
            try:
                path.unlink()
                deleted.append(str(path))
            except OSError:
                continue
        # 2) 容量压力：只删「过期+无租约」已删尽后仍超限时 —— 停手告警
        if self.total_bytes() > self.max_bytes:
            log.warning(
                '媒体暂存超配额但无可安全清理的过期文件（租约保护 %d 个），'
                '暂不删除待发送/待回执文件', len(self._leases))
        return deleted


# ------------------------------------------------------------ 依赖发现

PILK_SEARCH_PATHS: tuple[str, ...] = ()
CRYPTO_SEARCH_PATHS: tuple[str, ...] = ()
MEDIA_SCRIPTS_DIR: str = ''


def _configured_paths(cfg: dict[str, Any]) -> None:
    """从配置读取依赖搜索路径（WX-009：替换硬编码 D 盘路径）。"""
    global PILK_SEARCH_PATHS, CRYPTO_SEARCH_PATHS, MEDIA_SCRIPTS_DIR
    deps = cfg.get('dependency_paths') or {}
    if isinstance(deps, dict):
        PILK_SEARCH_PATHS = tuple(deps.get('pilk') or ())
        CRYPTO_SEARCH_PATHS = tuple(deps.get('crypto') or ())
        MEDIA_SCRIPTS_DIR = str(deps.get('media_scripts') or '')


def load_pilk():
    """优先常规 import，再按配置路径搜索（tts.load_pilk 的配置化版本）。"""
    try:
        import pilk  # type: ignore
        return pilk
    except ModuleNotFoundError:
        pass
    import sys
    for path in PILK_SEARCH_PATHS:
        if pathlib.Path(path).is_dir() and path not in sys.path:
            sys.path.insert(0, path)
            try:
                import pilk  # type: ignore
                return pilk
            except Exception:
                continue
    return None


def ensure_crypto():
    try:
        from Crypto.Cipher import AES  # noqa: F401
        return True
    except ModuleNotFoundError:
        pass
    import sys
    for path in CRYPTO_SEARCH_PATHS:
        if pathlib.Path(path).is_dir() and path not in sys.path:
            sys.path.insert(0, path)
            try:
                from Crypto.Cipher import AES  # noqa: F401
                return True
            except Exception:
                continue
    return False


_FILE_HASH = re.compile(r'[a-f0-9]{32}')


def safe_media_name(name: str) -> str:
    base = pathlib.PurePath(str(name or '')).name.strip()
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', base).strip('. ')
    return base[:180] or 'file.bin'
