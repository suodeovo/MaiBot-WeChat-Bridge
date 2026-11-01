"""微信链路统一托管插件。

把原来散落的进程收拢到一处：

    reader-api.py  python :5031   微信数据库读取 + SSE 推送
    微信 hook 客户端 Weixin :30001 原生发送（/SendTextMsg），需人工扫码
    OneBot 服务端    本插件 :7999  MaiBot 连这里

设计取向：

  * 发送走 hook 原生 HTTP，**不模拟键盘、不抢前台、窗口可最小化** ——
    这是 WeChatAgent 自带的 adapter 一直在用的路径，比 UIA 稳。
  * 接收走 reader 的 SSE，转成 OneBot v11 事件推给 MaiBot。
  * reader 由插件托管（启停 + 自愈），hook 只做健康检查，不代登录。
  * 进程停止前校验归属（pid + exe + 命令行 marker），pid 复用不会误杀。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, ClassVar, Mapping

from .config import WxSupervisorSettings
from .gateway import (CHAT_TYPE_GROUP, GATEWAY_NAME, PLATFORM, PROTOCOL,
                      resolve_outbound)
from . import gateway, launcher, media, sticker, tts
from .onebot_server import HookNative, OneBotServer, ReaderClient, conversation_id, onebot_id
from .sse_consumer import SseConsumer, to_onebot_event
from .supervisor import ManagedProcess, ProcessSupervisor, _port_open
from . import capabilities, wx_image, wx_message
from . import admin, stats
from .inbound import InboundPipeline
from .send_queue import SendScheduler, SendRequest, SendStatus
from .config_service import PluginConfigService
from .admin import AdminCommandsMixin
from . import perf
from . import send_queue
from .runtime_check import check_managed_reader

log = logging.getLogger('wxsupervisor')


class _NativeTimeoutError(RuntimeError):
    """V2-7：原生调用已提交但响应丢失（超时/连接中断）。


    由 capabilities 层在 run_hook 捕获 (TimeoutError, ConnectionError)
    时抛出，发送入口据此把请求记为 UNKNOWN 而不是 FAILED。
    """


def _classify_native_error(exc: BaseException) -> RuntimeError:
    """把 hook 调用异常分类：响应丢失（UNKNOWN）vs 明确失败。"""
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, ConnectionError,
                        ConnectionResetError, ConnectionAbortedError)):
        return _NativeTimeoutError(f'native response lost: {exc}')
    return RuntimeError(str(exc))

try:                                    # MaiBot SDK 在宿主环境里一定存在
    from maibot_sdk import MessageGateway, PluginConfigBase
    from maibot_sdk.plugin import MaiBotPlugin
except ModuleNotFoundError:             # 便于脱离宿主单测
    class PluginConfigBase:             # type: ignore[no-redef]
        """脱离宿主时的占位基类。"""

    def MessageGateway(**_kwargs: Any):  # type: ignore[no-redef]
        def _decorator(fn: Any) -> Any:
            return fn
        return _decorator

    class MaiBotPlugin:                 # type: ignore[no-redef]
        """脱离 MaiBot 宿主时的占位基类。"""

        async def on_load(self) -> None: ...
        async def on_unload(self) -> None: ...
        async def on_config_update(self, scope: str, config_data: dict[str, Any],
                                   version: str) -> None: ...

        def get_config_reload_subscriptions(self) -> list[str]:
            return []

log = logging.getLogger('wxsupervisor')

PLUGIN_DIR = Path(__file__).resolve().parent


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib
    except ModuleNotFoundError:      # py<3.11
        try:
            import tomli as tomllib  # type: ignore
        except ModuleNotFoundError:
            return {}
    try:
        with path.open('rb') as fh:
            data = tomllib.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class WxSupervisorPlugin(AdminCommandsMixin, MaiBotPlugin):
    """MaiBot 插件入口。"""

    # 声明给 MaiBot：WebUI 会用这个模型渲染配置界面
    config_model: ClassVar[type[PluginConfigBase] | None] = WxSupervisorSettings

    # 动态 API 按实例注册（WX-002）：类标志会造成第二实例 API 表为空。
    # Host 侧表跨重载保留，因此同一进程内对同名 API 只 replace（sync 契约）。

    def __init__(self) -> None:
        # 必须先调基类：MaiBotPlugin.__init__ 会初始化 _ctx / _dynamic_api_components
        # / _plugin_config_data 等内部状态，漏掉会在注册阶段抛 AttributeError。
        super().__init__()

        # 构造时就载入配置：on_load 可能因宿主未启用而跳过，
        # 但 status()/reader_spec() 这类只读检查随时可能被调用。
        self.cfg: dict[str, Any] = self._load_config()
        self.supervisor: ProcessSupervisor | None = None
        self.server: OneBotServer | None = None
        self.sse: SseConsumer | None = None
        self.hook: HookNative | None = None
        self.reader: ReaderClient | None = None
        self._watchdog: asyncio.Task[Any] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._gateway_ready = False
        self._last_target = ''
        self._turn_start = 0.0
        # 近期见过的会话（白名单为空时历史回填只扫这些）
        self._recent_sessions: set[str] = set()
        # 入站管线（T1）：过滤/组装/投递
        self.inbound = InboundPipeline(self)
        # 图片解析器（W4）：懒建
        self._img_resolver: Any = None
        # 用量统计与事件重放（N5）
        self.usage = stats.UsageStats()
        self.replay = stats.EventReplay()
        # WX-004：统一发送调度（额度原子预留 + 单调时钟）
        self._clock = SendScheduler.default_clock()
        self._send_scheduler = SendScheduler(self, clock=self._clock)
        # 账号目录短缓存 (value, monotonic_at)
        self._account_cache = ('', 0.0)
        # WX-009：受限工作池 + 慢媒体按会话调度 + 媒体清理器
        self.workers = perf.WorkerPool(self._cfg_int('media_workers', 4))
        self.media_scheduler = perf.SessionMediaScheduler(
            max(1, self._cfg_int('media_workers', 4)))
        self._janitor_task: asyncio.Task[Any] | None = None
        # WX-002：配置服务（验证 -> 应用 -> 持久化）
        self.config_service = PluginConfigService(self)
        # WX-008：按会话隔离的语音请求状态
        self._voice_requests: dict[str, tuple[str, float]] = {}
        # WX-006：恢复管理器（断线补收水位）在 on_load 构建
        self.recovery: Any = None

    # ------------------------------------------------------------ 生命周期

    async def on_load(self) -> None:
        self._loop = asyncio.get_running_loop()

        settings = self._settings()
        if not settings.validate_runtime(log):
            raise RuntimeError('WX_CONFIG_INVALID: 配置校验未通过，详见前述错误与 INSTALL.md')

        self.cfg = self._load_config()
        if not self.cfg.get('enabled', True):
            log.info('wxsupervisor 已在配置中禁用，跳过加载')
            return
        if not self.cfg.get('auto_start', True):
            log.info('auto_start 关闭，仅注册网关不启动服务')
            return

        root = str(self.cfg.get('project_root') or '')
        if not root or not Path(root).is_dir():
            raise RuntimeError('PROJECT_ROOT_MISSING: 请先安装 WeChatAgent 并正确填写 project.root；见 runtime/README.md')

        if self.cfg.get('autostart_reader', True):
            errors = await asyncio.to_thread(check_managed_reader, root, self.cfg['python'])
            if errors:
                raise RuntimeError('\n'.join(errors))

        self.supervisor = ProcessSupervisor(root)
        self.hook = HookNative(self.cfg['hook_base_url'], self._hook_token())

        # 语音总闸：hook 在它缺失时直接拒绝且错误码很难猜，这里主动确保存在
        try:
            media.ensure_voice_gate(root)
        except Exception as exc:
            log.warning('准备语音总闸失败: %s', exc)
        self.reader = ReaderClient(self.cfg['reader_base_url'], self._reader_token())

        # 1) 托管 reader
        if self.cfg.get('autostart_reader', True):
            if not await self._ensure_reader():
                raise RuntimeError('READER_START_FAILED: reader 启动失败，请检查 reader 日志与 runtime/README.md')

        # 2) hook 健康检查（不代登录，只提示）
        await self._check_hook(warn_only=True)

        # 3) 起 OneBot 服务端
        await self._start_server()

        # 4) 起 SSE 消费
        await self._start_sse()

        # 5) 守护循环
        if self.cfg.get('watchdog', True):
            self._watchdog = asyncio.create_task(self._watch(), name='wx-watchdog')

        # 6) 注册可调用的动作（sync_dynamic_apis 是协程，必须 await）
        await self._register_apis()

        # 7) 失败事件重放与断线补收（WX-006）
        try:
            from .recovery import RecoveryManager
            self.recovery = RecoveryManager(self)
            await self.recovery.start()
        except Exception as exc:
            log.warning('恢复管理器启动失败（不影响主链）: %s', exc)

        # 7.5) 依赖路径配置化 + 媒体清理（WX-009）
        self._apply_dependency_paths()
        try:
            await self._start_janitor()
        except Exception as exc:
            log.warning('媒体清理任务启动失败: %s', exc)

        # 8) 历史回填（N4）：is_notify 消息只入上下文不触发回复
        try:
            from .history_backfill import HistoryBackfill
            n = await HistoryBackfill(self).run()
            if n:
                log.info('历史回填: %d 条', n)
        except Exception as exc:
            log.warning('历史回填失败（不影响主链）: %s', exc)

        log.info('wxsupervisor 已加载：reader=%s hook=%s ob11=:%s',
                 self.cfg['reader_base_url'], self.cfg['hook_base_url'],
                 self.cfg.get('ob_port'))

    def cfg_source_dir(self) -> str:
        return str(PLUGIN_DIR)

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int((self.cfg or {}).get(key) or default)
        except (TypeError, ValueError):
            return default

    def _apply_dependency_paths(self) -> None:
        """WX-009/复核：依赖搜索路径配置驱动，注入全部真实消费点
        （perf 辅助、tts.load_pilk、wx_image 本地降级）。"""
        def split(raw: Any) -> list[str]:
            return [x.strip() for x in str(raw or '').replace('；', ';').split(';')
                    if x.strip()]
        cfg = self.cfg or {}
        pilk = split(cfg.get('pilk_paths'))
        crypto = split(cfg.get('crypto_paths'))
        scripts = str(cfg.get('media_scripts_dir') or '')
        perf._configured_paths({'dependency_paths': {
            'pilk': pilk, 'crypto': crypto, 'media_scripts': scripts}})
        # 真实消费点（复核：perf 辅助未接入旧消费点的问题修正）
        tts.PILK_SEARCH_PATHS = tuple(pilk)
        wx_image.CRYPTO_SEARCH_PATHS = tuple(crypto)
        wx_image.MEDIA_SCRIPTS_DIR = scripts

    async def _start_janitor(self) -> None:
        """媒体暂存周期清理（6 小时一次；先候选验证后删除）。"""
        if self._janitor_task is not None and not self._janitor_task.done():
            return

        async def loop() -> None:
            while True:
                try:
                    await asyncio.sleep(6 * 3600)
                    janitor = perf.MediaJanitor(
                        self.cfg['project_root'],
                        ttl_hours=float(self.cfg.get('media_ttl_hours') or 48.0),
                        max_total_mb=float(self.cfg.get('media_max_mb') or 512.0))
                    deleted = await asyncio.get_running_loop().run_in_executor(
                        self.workers._executor, janitor.enforce)
                    if deleted:
                        log.info('媒体清理: 删除 %d 个过期文件', len(deleted))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning('媒体清理异常: %s', exc)

        self._janitor_task = asyncio.create_task(loop(), name='wx-media-janitor')

    # ------------------------------------------------------------ 管理命令支撑

    def _is_admin(self, user_id: str) -> bool:
        """WX-003：稳定 wxid 精确匹配；空名单一律拒绝（行为变更）。"""
        allowed = self._settings().admin_set()
        if not allowed:
            return False
        return str(user_id or '').strip() in allowed

    async def _admin_reply(self, stream_id: str, text: str) -> tuple[bool, str, bool]:
        """管理命令回复：必须 await，并检查结果（WX-003）。"""
        send = getattr(getattr(self, 'ctx', None), 'send', None)
        if send is None or getattr(send, 'text', None) is None:
            log.warning('管理命令回信失败: send 能力未注入')
            return False, text, False
        try:
            result = await send.text(text, stream_id)
            ok = result is not False
            if not ok:
                log.warning('管理命令回信未被接受: %.40s', text)
            return ok, text, True
        except Exception as exc:
            log.warning('管理命令回信异常: %s', exc)
            return False, text, False

    def _resolve_admin_chat(self, chat: str) -> tuple[str, str]:
        """管理命令的会话参数解析：别名 -> 稳定 wxid。

        目录采用**合并更新**：reader 会话表刷新已有条目，同时保留
        此前注入/学到的条目 —— 全量替换会让同一进程里其他路径
        注入的会话（测试、回填）丢失。
        """
        try:
            reader = self.reader
            if reader is not None and hasattr(reader, 'sessions'):
                sessions = reader.sessions(50)
                if sessions:
                    self.inbound.policy.directory.merge(sessions)
        except Exception as exc:
            log.debug('管理命令目录刷新失败: %s', exc)
        return self.inbound.policy.directory.resolve_alias(chat)

    async def _persist_wechat(self, wechat_changes: dict[str, Any]) -> dict[str, Any]:
        return await self.config_service.apply({'wechat': wechat_changes},
                                               persist=True)

    async def _persist_wechat_tts(self, tts_changes: dict[str, Any]) -> dict[str, Any]:
        return await self.config_service.apply({'tts': tts_changes},
                                               persist=True)

    async def _register_apis(self) -> None:
        """按实例注册动态 API，经 SDK 的 replace/sync 契约同步到 Host。

        WX-002：旧实现用类标志 `_apis_registered` 跳过重复注册，但动态
        组件表是实例属性 —— 第二实例 register 被跳过后 sync 空列表，
        把第一个实例的 API 也顶掉。现在每个实例都声明自己的 API；
        Host 侧同名 API 由 replace_dynamic_apis 原子替换。
        """
        try:
            self.register_dynamic_api(
                'open_wechat',
                self._api_open_wechat,
                description='按配置的版本启动微信 hook 客户端并等待就绪',
                version='1',
                public=False,
            )
            synced = await self.sync_dynamic_apis()
            if synced:
                log.debug('动态 API 已同步: open_wechat')
            else:
                log.warning('动态 API 同步未被 Host 接受')
        except Exception as exc:
            log.warning('注册动态 API 失败: %s', exc)

    async def _api_open_wechat(self, **kwargs: Any) -> dict[str, Any]:
        """动态 API 处理器。"""
        del kwargs
        return await self._open_wechat()

    async def on_unload(self) -> None:
        if self._janitor_task is not None:
            self._janitor_task.cancel()
            try:
                await self._janitor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._janitor_task = None
        if self.recovery is not None:
            try:
                await self.recovery.stop()
            except Exception:
                pass
            self.recovery = None
        if self.workers is not None:
            self.workers.shutdown()

        if self._watchdog is not None:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog = None

        if self.sse is not None:
            try:
                await self.sse.stop()
            except Exception as exc:
                log.warning('停止 SSE 失败: %s', exc)
            self.sse = None

        if self.server is not None:
            try:
                await self.server.stop()
            except Exception as exc:
                log.warning('停止 OneBot 服务端失败: %s', exc)
            self.server = None

            # 等端口真正释放：热重载时新实例立刻 bind 会撞上旧 listen socket，
            # 表现为「重载后 7999 还是老 pid」，代码却没更新。
            port = int(self.cfg.get('ob_port') or 7999)
            for _ in range(20):
                if not _port_open(port):
                    break
                await asyncio.sleep(0.1)
            else:
                log.warning('OneBot 端口 %s 仍未释放，新实例可能绑定失败', port)

        # hook 客户端不代关：那是用户的微信，关掉会掉登录
        if self.supervisor is not None and self.cfg.get('stop_reader_on_unload', True):
            spec = self._reader_spec()
            if spec is not None:
                ok, msg = self.supervisor.stop(spec)
                log.info('停止 reader: %s %s', ok, msg)

        log.info('wxsupervisor 已卸载')

    async def on_config_update(self, scope: str, config_data: dict[str, Any],
                               version: str) -> None:
        log.info('配置更新 scope=%s version=%s keys=%s',
                 scope, version, list((config_data or {}).keys()))
        if scope not in ('self', 'bot'):
            return
        # WX-002：区分插件自身配置（self）与全局 bot 配置广播。
        # bot 范围不是本插件配置：只记录，不覆盖插件配置节。
        if scope == 'bot':
            log.debug('bot 全局配置广播已收到（非插件配置，忽略合并）')
            return

        result = await self.config_service.apply(config_data, persist=False,
                                                 merge=False)
        if not result['ok']:
            log.error('新配置校验/应用失败，保持原状: %s', result['error'])
            return

        # 配置里的「执行动作 = open」：拉起对应版本微信。
        settings = self._settings()
        if str(settings.launcher.action or '').lower() == 'open':
            result_open = await self._open_wechat()
            log.info('配置动作已执行: %s', result_open.get('reason'))
            await self.config_service.apply(
                {'launcher': {'action': 'idle',
                              'last_result': str(result_open.get('reason', ''))[:200]}},
                persist=True)

    # 各配置节影响哪些组件。改成 None 表示本节不影响任何运行组件。
    _SECTION_COMPONENTS: ClassVar[dict[str, tuple[str, ...]]] = {
        'plugin':    (),                          # enabled/auto_start 只在 on_load 读
        'project':   ('reader', 'hook', 'server', 'sse'),
        'reader':    ('reader', 'sse'),
        'hook':      ('hook', 'server'),
        'launcher':  (),
        'server':    ('server',),
        'wechat':    (),                          # 白名单/唤醒词每次入站现读
        'limits':    (),                          # 节流参数每次出站现读
        'tts':       (),                          # TTS 参数每次出站现读
    }

    def _rebuild_consumers(self, old: dict[str, Any],
                           new: dict[str, Any]) -> None:
        """兼容占位：R10 后 config_service 直接 await `_apply_config_diff`，
        不再经此后台任务路径。保留空实现防旧调用点报错。"""
        log.debug('_rebuild_consumers 已由配置事务取代（R10）')

    async def _apply_config_diff(self, old: dict[str, Any],
                                 new: dict[str, Any]) -> list[str]:
        """对比新旧扁平配置，按依赖原子重建受影响的组件；无关变更零动作。

        WX-002/R10：由 config_service 事务串行 await 调用。
        reader 更新时同步重建 OneBot server（其持有 reader/hook 引用）、
        SSE、图片解析器、恢复管理器账号缓存 —— 全部引用一并切换。
        """
        changed_sections: set[str] = set()
        for key in set(old) | set(new):
            if old.get(key) != new.get(key):
                changed_sections.add(self._section_of_key(key))

        components: set[str] = set()
        for section in changed_sections:
            components.update(self._SECTION_COMPONENTS.get(section, ()))
        if not components:
            return []

        rebuilt: list[str] = []
        token_changed = (old.get('reader_base_url') != new.get('reader_base_url'))

        # 1) reader 客户端
        if 'reader' in components or token_changed:
            self.reader = ReaderClient(self.cfg['reader_base_url'],
                                       self._reader_token())
            rebuilt.append('reader')
            # 账号相关缓存失效（WX-002/R10：账号切换无旧账号缓存）
            self._img_resolver = None
            self._account_cache = ('', 0.0)
            # R10：恢复管理器持 reader 状态引用 —— 同步失效账号缓存
            if self.recovery is not None:
                self.recovery._account_override = ''

        # 2) SSE 消费
        if 'reader' in components or 'sse' in components:
            if self.sse is not None:
                try:
                    await self.sse.stop()
                except Exception:
                    pass
                self.sse = None
            await self._start_sse()
            rebuilt.append('sse')

        if 'hook' in components:
            self.hook = HookNative(self.cfg['hook_base_url'], self._hook_token())
            rebuilt.append('hook')

        # 3) OneBot 服务端（reader 更新也重建：其持有 reader/hook 引用，R10.3）
        if 'server' in components or token_changed:
            if self.server is not None:
                try:
                    await self.server.stop()
                except Exception:
                    pass
                self.server = None
            await self._start_server()
            rebuilt.append('server')

        return rebuilt

    @staticmethod
    def _section_of_key(flat_key: str) -> str:
        """扁平配置 key -> 配置节名。覆盖 _load_config 产出的全部 key。"""
        if flat_key.startswith('tts_'):
            return 'tts'
        if flat_key.startswith(('ob_',)):
            return 'server'
        if flat_key.startswith(('reader_',)):
            return 'reader'
        if flat_key.startswith(('hook_', 'expected_version', 'max_concurrent')):
            return 'hook'
        if flat_key.startswith('launcher_'):
            return 'launcher'
        if flat_key.startswith(('wake_', 'max_reply', 'whitelist')):
            return 'wechat'
        if flat_key in ('project_root', 'python'):
            return 'project'
        if flat_key in ('enabled', 'auto_start'):
            return 'plugin'
        # 其余（min_interval_ms / round_window_ms / max_per_minute / batch_ms 等）
        # 都属于 limits；limits 的消费点现读，映射到空节即可
        return 'limits'

    def get_config_reload_subscriptions(self) -> list[str]:
        return ['bot']

    # ------------------------------------------------------ MessageGateway

    @MessageGateway(
        route_type='duplex',
        name=GATEWAY_NAME,
        platform=PLATFORM,
        protocol=PROTOCOL,
        description='微信 hook 原生发送 + reader SSE 接收',
    )
    async def handle_wxsupervisor_gateway(
        self,
        message: Mapping[str, Any],
        route: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """MaiBot 出站：把消息发到微信（R9：统一出站入口）。"""
        return await self.send_outbound(
            message, route=route, metadata=metadata, source='gateway')

    async def send_outbound(self, message: Mapping[str, Any],
                            route: Mapping[str, Any] | None = None,
                            metadata: Mapping[str, Any] | None = None,
                            source: str = 'gateway') -> dict[str, Any]:
        """统一出站入口（R9）：MaiBot 网关、外部 OneBot、TTS 共用。

        流程：内容解析/清洗 → 按最终发送计划计额度（R11）→ 统一调度
        acquire（幂等 R7 + 白名单/停用 R8/R9 + 限流）→ 执行发送 →
        permit 恰好收尾一次（R2，含异常/取消）。
        """
        if self.hook is None:
            return {'success': False, 'error': 'hook not ready'}

        target, chat_type, text = resolve_outbound(message, route)
        if not target:
            return {'success': False, 'error': 'missing target id'}
        raw_message = message.get('raw_message') if isinstance(message, Mapping) else None
        parts = sticker.split_segments(raw_message)
        stickers = parts['stickers']
        images = parts['images']
        voices = parts['voices']
        files = parts['files']
        if not text.strip() and parts['text'].strip():
            text = parts['text']

        if voices or images or stickers or files:
            log.info('出站媒体段: 语音=%d 图片=%d 表情=%d 文件=%d 原始=%s',
                     len(voices), len(images), len(stickers), len(files),
                     gateway.segment_summary(raw_message))

        # 兜底清洗：无论 text 从哪条路径来，都不允许出现 Python 字面量。
        # 历史事故：某些字段是 {'text': '...'} 形态，被 str() 后直接发到微信：
        #   {'text': '@小柚\u2005出来玩了'} 嗯？叫我干嘛呀
        # 这里再走一次 _as_text 并把残留的字典/列表字面量剔掉。
        text = gateway._as_text(text)
        if text and text.lstrip().startswith(("['", "{'", '["', '{"')):
            log.warning('出站文本疑似 Python 字面量，已丢弃前段: %.60s', text)
            marker = text.find('}') if text.lstrip().startswith(("{'", '{"')) else text.find(']')
            if marker > 0:
                text = text[marker + 1:].lstrip()
            else:
                text = ''

        # 媒体占位符清洗：MaiBot 的 processed_plain_text 会把语音/图片段渲染成
        # 「[语音]」这样的占位文本。真媒体已经抽到 voices/images/stickers/files
        # 各自走原生接口，占位符再当文本发一遍，微信里就只剩一条字面量「[语音]」。
        if (voices or images or stickers or files) and text.strip():
            scrubbed = gateway.strip_media_placeholders(text)
            if not scrubbed:
                log.info('出站文本只有媒体占位符(%s)，已丢弃；段: %s',
                         text.strip()[:32], gateway.segment_summary(raw_message))
                text = ''
            else:
                text = scrubbed

        if (not text.strip() and not stickers and not images
                and not voices and not files):
            # 把入参关键字段打进日志，便于下次直接定位（勿打全文，只打长度与键名）
            try:
                keys = sorted(k for k in (message or {}).keys())
                raw = message.get('raw_message') if isinstance(message, Mapping) else None
                log.warning('出站内容为空: keys=%s raw_len=%s route=%s',
                            keys, len(raw) if isinstance(raw, list) else 'n/a',
                            sorted((route or {}).keys()))
            except Exception:
                pass
            return {'success': False, 'error': 'empty message'}

        # R2/R7/R11：先 resolve 出策略用的稳定 wxid（幂等键需要 target），
        # 再按最终发送计划计额度。resolve 在 permit 之前做 —— 失败不占额度。
        try:
            wxid, display = await asyncio.get_running_loop().run_in_executor(
                None, self.reader.resolve, target)
        except Exception as exc:
            return {'success': False, 'error': f'resolve failed: {exc}'}
        if not wxid:
            return {'success': False, 'error': f'cannot resolve: {target}'}

        # R8/R9：策略检查（白名单 + 停用）在调度之前 —— 明确拒绝，不占额度。
        decision = self.inbound.policy.allows_outbound(wxid)
        if not decision:
            log.warning('出站被策略拒绝: %s -> %s', source, decision.reason)
            return {'success': False, 'error': f'policy rejected: {decision.reason}'}

        # V2-3：发送计划一次构建（peek 语义，无副作用）—— 计数与执行共用
        # 同一计划；语音请求只在拿到 permit 进入执行时消费（commit 边界）。
        has_text = bool(text.strip())
        media_count = len(voices) + len(images) + len(stickers) + len(files)
        plan_voice, plan_text = (False, True)
        if has_text and not voices:
            plan_voice, plan_text = self._tts_plan(text, session=wxid,
                                                   consume=False)
        plan = send_queue.SendPlan(
            has_text=has_text, want_voice=plan_voice, want_text=plan_text,
            voice_fallback_enabled=bool(self.cfg.get('tts_voice_fallback_text', True)),
            media_count=media_count, voice_request_session=wxid)
        if plan.native_count <= 0:
            return {'success': False, 'error': 'empty message'}

        account_hint = str((route or {}).get('account') or '')
        request = SendRequest(
            target=wxid, chat_type=chat_type, account=account_hint,
            native_count=plan.native_count,
            request_id=str((metadata or {}).get('request_id')
                           or (metadata or {}).get('external_message_id') or ''),
        )

        # R7/R2：acquire 可能返回 Permit（可发）、DedupHit（幂等命中）、
        # None（拒绝）。DedupHit 直接返回首个请求的结果，不占用并发。
        try:
            outcome, wait_reason = await self._send_scheduler.acquire(
                request, max_wait_s=int(self.cfg.get('max_wait_ms') or 8000) / 1000.0)
        except Exception as exc:
            return {'success': False, 'error': f'scheduler error: {exc}'}

        if isinstance(outcome, send_queue.DedupHit):
            hit = outcome
            log.info('出站幂等命中(%s) request_id=%s 等待在途=%s',
                     hit.status.value, hit.request_id, hit.waited_for_inflight)
            result = dict(hit.result or {'success': True})
            result.setdefault('metadata', {})['dedup'] = True
            return result

        permit = outcome
        if permit is None:
            log.warning('出站被调度拒绝: %s', wait_reason)
            # 无 permit：幂等占位未建立、无并发名额 —— 记 REJECTED（不覆盖 SUBMITTED）
            self._send_scheduler.finish(request, SendStatus.REJECTED,
                                        reason=str(wait_reason), permit=None)
            return {'success': False, 'error': f'throttled: {wait_reason}'}

        # R2：permit 从这里开始必须恰好收尾一次 —— 统一 try/except/finally。
        try:
            try:
                return await self._execute_outbound(
                    wxid, display, chat_type, text, message,
                    voices, images, stickers, files, request, permit, plan)
            except asyncio.CancelledError:
                # V2-7：在途取消 —— 原生调用可能已发生，结果未知
                self._send_scheduler.mark_unknown(request, 'cancelled mid-flight')
                self._send_scheduler.finish(request, SendStatus.FAILED,
                                            reason='cancelled (result unknown)',
                                            permit=permit)
                raise
            except _NativeTimeoutError as exc:
                # V2-7：原生已提交但响应丢失 —— UNKNOWN，不自动重试
                self._send_scheduler.mark_unknown(request, str(exc))
                self._send_scheduler.finish(request, SendStatus.FAILED,
                                            reason=f'unknown: {exc}',
                                            permit=permit)
                return {'success': False,
                        'error': f'result unknown: {exc}',
                        'metadata': {'delivery': 'unknown'}}
            except Exception as exc:
                self._send_scheduler.finish(request, SendStatus.FAILED,
                                            reason=str(exc), permit=permit)
                return {'success': False, 'error': f'send failed: {exc}'}
        finally:
            # R2 兜底：任何路径未收尾则强制归还并发名额（幂等收尾有 once 保护）
            self._send_scheduler.release_permit(permit)

    async def _execute_outbound(self, wxid, display, chat_type, text, message,
                                voices, images, stickers, files,
                                request, permit, plan=None):
        """permit 持有期间的实际发送。返回前必须 finish（各分支显式）。

        V2-3：使用调用方传入的发送计划（peek 构建），本函数入口即
        commit 边界 —— 消费语音请求状态；限流拒绝/未提交不消费。
        """
        if plan is None:
            # 兼容旧直调：构建并消费
            plan_voice, plan_text = (False, True)
            if text.strip() and not voices:
                plan_voice, plan_text = self._tts_plan(text, session=wxid,
                                                       consume=True)
            plan = send_queue.SendPlan(
                has_text=bool(text.strip()), want_voice=plan_voice,
                want_text=plan_text,
                voice_fallback_enabled=bool(self.cfg.get('tts_voice_fallback_text', True)),
                media_count=len(voices) + len(images) + len(stickers) + len(files),
                voice_request_session=wxid)
        elif plan.want_voice and plan.voice_request_session:
            # commit：消费该会话的语音请求（peek 已判定 want_voice）
            self._voice_requests.pop(plan.voice_request_session, None)

        account = await self._account_async()
        loop = asyncio.get_running_loop()
        ctx = capabilities.SendContext(
            wxid=wxid, display=display or wxid, account=account,
            hook=self.hook, cfg={**self.cfg, '_plugin_ref': self},
            loop=loop)

        # V2-3：按既有计划执行（不重新判定）
        want_voice, want_text = plan.want_voice, plan.want_text

        if text.strip() and want_text:
            await capabilities._handle_text(ctx, {'type': 'text', 'data': text})

        if text.strip() and want_voice:
            synth = await self._synthesize_voice(text)
            voice_ok = False
            if synth is not None:
                silk_bytes, duration = synth
                try:
                    silk_path = media.stage_silk(self.cfg['project_root'], silk_bytes)
                    result = await self._send_voice_with_backoff(
                        wxid, str(silk_path), duration, account)
                    if media.voice_confirmed(result):
                        voice_ok = True
                        ctx.mark('voice')
                        log.info('语音已提交(TTS) -> %s %d ms', display or wxid, duration)
                    else:
                        msg = result.get('error') or f'ret={result.get("ret")}'
                        log.warning('TTS 语音被拒: %s', msg)
                        ctx.fail('tts voice', msg)
                except Exception as exc:
                    log.warning('TTS 语音发送失败: %s', exc)
                    ctx.fail('tts voice', str(exc))

            # 语音没成功（合成失败 / 被 hook 拒 / 429 重试用尽）时补发文字。
            # 不补的话整条回复就凭空消失了 —— 用户看到的就是「怎么不发消息」。
            # 想彻底静默就把 tts.voice_fallback_text 关掉。
            need_fallback = (not voice_ok and not ctx.sent['text']
                             and bool(self.cfg.get('tts_voice_fallback_text', True)))
            if need_fallback:
                log.info('语音未成功，补发文字兜底 -> %s', display or wxid)
                await capabilities._handle_text(ctx, {'type': 'text', 'data': text})

        # ---- 媒体段：全部走能力注册表（image/voice/emoji/file + 别名与 custom 解包）
        # voices 从 split_segments 抽出时 type 仍是 'voice'，直接可分发。
        media_segments = [*voices, *files, *images, *stickers]
        if media_segments:
            await capabilities.dispatch_segments(ctx, media_segments)

        sent = ctx.sent
        errors = ctx.errors

        # R2/R7：发送状态收尾（恰好一次；含部分成功 PARTIAL）
        total_planned = request.native_count
        total_sent = sum(sent.values())
        if total_sent == 0:
            self._send_scheduler.finish(request, SendStatus.FAILED,
                                        reason='; '.join(errors) or 'nothing sent',
                                        permit=permit)
            return {'success': False, 'error': '; '.join(errors) or 'nothing sent'}

        # ret=0 只代表提交：状态记录为 submitted，不升级为已送达。
        status = SendStatus.SUBMITTED
        if total_sent < total_planned and errors:
            status = SendStatus.PARTIAL
        self._send_scheduler.finish(request, status,
                                    reason='; '.join(errors),
                                    permit=permit,
                                    result=None)   # result 由本函数返回值承担
        self._send_scheduler.finish_turn(wxid, chat_type,
                                         native_count=total_sent)
        self.usage.count_sent(wxid)
        log.info('出站已发送 -> %s(%s) 文本=%d 图片=%d 表情=%d 语音=%d 文件=%d',
                 display or wxid, chat_type, sent['text'], sent['image'],
                 sent['sticker'], sent['voice'], sent['file'])
        payload_meta = {
            'target': wxid,
            'target_name': display,
            'chat_type': chat_type,
            'transport': 'wechat-hook-native',
            'delivery': 'submitted',
            'sent_text': sent['text'],
            'sent_image': sent['image'],
            'sent_sticker': sent['sticker'],
            'sent_voice': sent['voice'],
            'sent_file': sent['file'],
        }
        result_payload: dict[str, Any] = {'success': True, 'metadata': payload_meta}
        if errors:
            result_payload['warning'] = '; '.join(errors)
        return result_payload

    async def _submit_inbound(self, payload: dict[str, Any]) -> None:
        """委托给 InboundPipeline（T1 拆分）。"""
        await self.inbound.submit(payload)

    # ------------------------------------------------------ 出站节流
    #
    # WX-004：节流逻辑移入 send_queue.SendScheduler（额度原子预留、
    # 单调时钟、按账号全局额度 + 按规范化目标的会话间隔/轮次）。
    # 这里保留 _throttle_wait/_in_turn 为兼容入口，内部直接委托。

    def _throttle_wait(self, chat_type: str, target: str = '') -> tuple[float, str]:
        """兼容入口：查询「下一个发送窗口还需多少秒」。不预留额度。"""
        return self._send_scheduler.probe(chat_type, target)

    def _in_turn(self, now: float) -> bool:
        return self._send_scheduler.in_turn(now)

    async def _send_voice_with_backoff(self, wxid: str, silk_path: str,
                                       duration: int, account: str) -> dict[str, Any]:
        """发语音，遇到 hook 的 429 退避重试。

        hook 的 /WeChatAgent/voice-send 有内部速率限制，短时间内连发会回
        `HTTP Error 429: Too Many Requests`（不是 SILK 格式问题，重试就能过）。
        日志里那条 `TTS 语音发送失败: HTTP Error 429` 就是它。

        重试间隔取 1s / 2s / 4s，总预算受 tts_retry_budget_ms 约束。
        """
        cfg = self.cfg or {}
        delays = [1.0, 2.0, 4.0]
        budget = int(cfg.get('tts_retry_budget_ms') or 12000) / 1000.0
        deadline = time.monotonic() + budget
        loop = asyncio.get_running_loop()
        last: dict[str, Any] = {}

        for attempt in range(len(delays) + 1):
            try:
                last = await loop.run_in_executor(
                    None, self.hook.send_voice, wxid, silk_path, duration, account)
            except Exception as exc:
                last = {'ret': -1, 'error': str(exc)}
                if '429' not in str(exc):
                    return last
            else:
                if '429' not in str(last.get('error') or ''):
                    return last

            if attempt >= len(delays):
                break
            pause = delays[attempt]
            if time.monotonic() + pause > deadline:
                log.warning('语音重试预算用尽，放弃（最后一次: %s）', last.get('error'))
                break
            log.info('语音被限流(429)，%.1fs 后重试（第 %d 次）', pause, attempt + 1)
            await asyncio.sleep(pause)

        return last

    def _mark_sent(self, chat_type: str, target: str = '') -> None:
        """兼容入口：登记一次原生发送（WX-004 后实际状态在 scheduler）。"""
        self._send_scheduler.record_native(target, chat_type)

    # ------------------------------------------------------------ 语音合成

    def note_inbound_text(self, session_id: str, text: str) -> None:
        """WX-008：按会话记录最近入站正文（语音请求判定用）。"""
        self._voice_requests[session_id] = (text, time.time())
        # 防膨胀：只保留最近 64 个会话的请求状态
        if len(self._voice_requests) > 64:
            oldest = sorted(self._voice_requests.items(),
                            key=lambda kv: kv[1][1])[:len(self._voice_requests) - 64]
            for key, _ in oldest:
                self._voice_requests.pop(key, None)

    def _tts_plan(self, text: str, session: str = '',
                  consume: bool = True) -> tuple[bool, bool]:
        """决定这次回复发不发语音、发不发文字。

        返回 (send_voice, send_text)。

        V2-3：consume=False 时为 peek 语义（不消费请求状态）——
        发送计划计数阶段用 peek，执行阶段才消费（commit 边界）。

        ## 模式语义（与旧版一致）

            text_only      只发文字（默认）。语音完全交给模型按需触发。
            on_request     用户在这条消息里明确要求「发语音」才转语音。
            both           文字 + 语音都发
            voice_only     只发语音（文字只做兜底）
            voice_if_short 短文本发语音，长文本发文字

        WX-008：on_request 的判定按**会话**隔离 —— session 为空时兼容
        旧行为（无请求状态）。A 群请求不影响 B 群；B 群新消息也不会
        覆盖 A 群的请求状态（各会话独立键）。

        长度判定用**原始清洗后的长度**，不是截断后的——否则
        clean_for_speech 会先截到 max_chars，导致「超长」永远判不出来。
        """
        cfg = self.cfg or {}
        if not cfg.get('tts_enabled', False):
            return False, True

        mode = str(cfg.get('tts_mode') or 'text_only').lower()

        # text_only：插件不主动合成，语音只来自模型调 volc_tts_speak
        if mode == 'text_only':
            return False, True

        # on_request：本回合该会话的用户点名要语音才合成
        if mode == 'on_request':
            if self._voice_requested(session, consume=consume):
                return True, bool(cfg.get('tts_keep_text', False))
            return False, True

        max_chars = int(cfg.get('tts_max_chars') or 120)
        raw = tts.clean_for_speech(text, limit=0)      # limit=0 表示不截断
        if not raw:
            return False, True
        if len(raw) > max_chars:
            # 太长不适合朗读，保持文字
            return False, True

        if mode == 'voice_only':
            return True, False
        if mode == 'voice_if_short':
            short = int(cfg.get('tts_short_chars') or 60)
            if len(raw) <= short:
                # 短文本优先语音，但同样保留文字兜底
                return True, False
            return False, True
        # both：文字和语音都发
        return True, True

    def _voice_requested(self, session: str = '',
                         consume: bool = True) -> bool:
        """该会话本回合用户是否明确要了语音（WX-008：按会话隔离）。

        依据该会话最近一条入站消息的原文，窗口由 tts_request_window_s 控制
        （默认 120 秒），避免十分钟前那句「发语音」还一直生效。

        V2-3：consume=False 为 peek（不删除请求状态）——发送计划计数
        与执行共用同一判定；消费只发生在执行 commit 边界。

        关键词分两类：
            请求类  发语音 / 语音说 / 念出来 / 读一下 / 说给我听 / 用声音
            否定类  别发语音 / 不要语音 / 别念 / 打字就行

        否定类优先级更高：句子里出现「别发语音」不算请求。
        """
        cfg = self.cfg or {}
        key = str(session or '')
        entry = self._voice_requests.get(key)
        if not entry:
            return False
        text, at = entry
        if not text:
            return False
        window = float(cfg.get('tts_request_window_s') or 120)
        if time.time() - at > window:
            if consume:
                self._voice_requests.pop(key, None)
            return False

        negatives = cfg.get('tts_request_negative') or []
        if any(w and w in text for w in negatives):
            if consume:
                self._voice_requests.pop(key, None)
            return False

        triggers = cfg.get('tts_request_triggers') or []
        hit = [w for w in triggers if w and w in text]
        if hit:
            log.info('用户要求语音（命中 %s），本条转语音 -> %s',
                     '/'.join(hit[:3]), key)
            if consume:
                self._voice_requests.pop(key, None)
            return True
        return False

    async def _synthesize_voice(self, text: str) -> tuple[bytes, int] | None:
        """文本 -> (SILK 字节, 时长毫秒)。失败返回 None。"""
        cfg = self.cfg or {}
        try:
            api = getattr(getattr(self, 'ctx', None), 'api', None)
            audio, mime = await tts.synthesize_via_plugin(
                api,
                tts.clean_for_speech(text, int(cfg.get('tts_max_chars') or 120)),
                str(cfg.get('tts_speaker') or ''),
            )
            silk, duration = tts.audio_to_silk(
                audio, mime, int(cfg.get('tts_sample_rate') or 24000))
            log.info('TTS 合成完成 %d 字节 / %d ms', len(silk), duration)
            return silk, duration
        except Exception as exc:
            log.warning('TTS 合成失败，退回文字: %s', exc)
            return None

    # ------------------------------------------------------ 打开微信

    async def _open_wechat(self) -> dict[str, Any]:
        """按配置的版本拉起微信。供配置动作与动态 API 共用。"""
        cfg = self.cfg or {}
        exe = str(cfg.get('launcher_exe') or 'state/hook/wechat-4.1.10.27/Weixin.exe')
        wait = float(cfg.get('launcher_wait') or 40.0)
        root = str(cfg.get('project_root') or '')
        expected = str(cfg.get('expected_version') or '')
        base = str(cfg.get('hook_base_url') or '')

        result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: launcher.open_hook_wechat(
                project_root=root, exe_raw=exe, expected_version=expected,
                hook_base_url=base, hook_token=self._hook_token(),
                wait_seconds=wait),
        )
        log.info('打开微信: ok=%s action=%s pid=%s %s',
                 result.get('ok'), result.get('action'),
                 result.get('pid'), result.get('reason'))

        # hook 就绪后重建客户端，让发送立刻可用
        if result.get('ok'):
            try:
                self.hook = HookNative(base, self._hook_token())
            except Exception:
                pass
        return result

    async def open_wechat_now(self) -> dict[str, Any]:
        """对外可调：打开对应版本微信。"""
        return await self._open_wechat()

    # ------------------------------------------------------------ 配置

    def _load_config(self) -> dict[str, Any]:
        """把配置模型摊平成内部用的扁平 dict。

        字段来源：MaiBot WebUI 保存的插件配置（config_model）；
        没有时回退到插件目录的 config.toml；再没有就用模型默认值。
        """
        settings = self._settings()
        return {
            'enabled': settings.enabled(),
            'auto_start': bool(settings.plugin.auto_start),
            'project_root': settings.project.root,
            'python': settings.project.python,
            'reader_base_url': settings.reader.base_url,
            'hook_base_url': settings.hook.base_url,
            'ob_host': settings.server.host,
            'ob_port': int(settings.server.port),
            'ob_token': settings.server.token,
            'ob_enable': bool(settings.server.enable),
            'autostart_reader': bool(settings.reader.autostart),
            'stop_reader_on_unload': bool(settings.reader.stop_on_unload),
            'watchdog': bool(settings.reader.watchdog),
            'watchdog_seconds': float(settings.reader.watchdog_seconds),
            'expected_version': settings.hook.expected_version,
            'hook_timeout_ms': int(settings.hook.request_timeout_ms),
            'receipt_timeout_ms': int(settings.hook.receipt_timeout_ms),
            'max_concurrent_sends': int(settings.hook.max_concurrent_sends),
            'launcher_action': settings.launcher.action,
            'launcher_exe': settings.launcher.exe,
            'launcher_wait': float(settings.launcher.wait_seconds),
            'launcher_allow_other': bool(settings.launcher.allow_other_versions),
            'wake_mode': settings.wechat.wake_mode,
            'wake_words': settings.wake_word_list(),
            'max_reply_messages': int(settings.wechat.max_reply_messages),
            'batch_ms': int(settings.limits.batch_ms),
            'min_interval_ms': int(settings.limits.min_interval_ms),
            'private_min_interval_ms': int(settings.limits.private_min_interval_ms),
            'max_per_minute': int(settings.limits.max_per_minute),
            'max_per_ten_minutes': int(settings.limits.max_per_ten_minutes),
            'round_window_ms': int(settings.limits.round_window_ms),
            'turn_quiet_ms': int(settings.limits.turn_quiet_ms),
            'round_min_gap_ms': int(settings.limits.round_min_gap_ms),
            'max_wait_ms': int(settings.limits.max_wait_ms),
            'tts_enabled': bool(settings.tts.enabled),
            'tts_mode': str(settings.tts.mode or 'text_only'),
            'tts_voice_fallback_text': bool(settings.tts.voice_fallback_text),
            'tts_request_triggers': settings.tts_triggers(),
            'tts_request_negative': settings.tts_negatives(),
            'tts_request_window_s': float(settings.tts.request_window_s),
            'tts_keep_text': bool(settings.tts.keep_text),
            'tts_max_chars': int(settings.tts.max_chars),
            'tts_short_chars': int(settings.tts.short_chars),
            'tts_speaker': str(settings.tts.speaker or ''),
            'tts_sample_rate': int(settings.tts.sample_rate),
            'tts_retry_budget_ms': int(settings.tts.retry_budget_ms),
            # WX-009 消费点（复核：字段必须进入扁平 cfg 才有真实消费者）
            'media_ttl_hours': float(settings.project.media_ttl_hours),
            'media_max_mb': float(settings.project.media_max_mb),
            'media_workers': int(settings.project.media_workers),
            'pilk_paths': settings.project.pilk_paths,
            'crypto_paths': settings.project.crypto_paths,
            'media_scripts_dir': settings.project.media_scripts_dir,
        }

    def _settings(self) -> WxSupervisorSettings:
        """取当前生效的配置：优先宿主注入的，其次 config.toml，最后默认值。"""
        inst = getattr(self, '_plugin_config_instance', None)
        if isinstance(inst, WxSupervisorSettings):
            return inst

        data = getattr(self, '_plugin_config_data', None)
        if isinstance(data, dict) and data:
            try:
                return WxSupervisorSettings(**data)
            except Exception as exc:
                log.warning('宿主配置解析失败，回退 config.toml: %s', exc)

        raw = _load_toml(PLUGIN_DIR / 'config.toml')
        if raw:
            try:
                return WxSupervisorSettings(**raw)
            except Exception as exc:
                log.warning('config.toml 解析失败，使用默认值: %s', exc)
        return WxSupervisorSettings()

    def _state_dir(self) -> Path:
        return Path(self.cfg['project_root']) / 'state'

    def _reader_token(self) -> str:
        try:
            data = json.loads((self._state_dir() / 'wechat-io-config.json')
                              .read_text(encoding='utf-8-sig'))
            return str(data.get('reader_token') or '')
        except Exception:
            return ''

    def _hook_token(self) -> str:
        try:
            return (self._state_dir() / 'hook' / 'native-token.txt').read_text(
                encoding='utf-8-sig').strip()
        except Exception:
            return ''

    def _account(self) -> str:
        """兼容入口：同步读取（WX-009 起优先用 _account_async 的短缓存）。"""
        try:
            return str(self.reader.status().get('accountDirectory') or '') if self.reader else ''
        except Exception:
            return ''

    async def _account_async(self) -> str:
        """账号目录，带 3 秒缓存；reader 缺失时返回空串（WX-009）。"""
        now = time.monotonic()
        cached, at = getattr(self, '_account_cache', ('', 0.0))
        if cached and now - at < 3.0:
            return cached
        loop = asyncio.get_running_loop()
        try:
            st = await loop.run_in_executor(None, self.reader.status)
            account = str(st.get('accountDirectory') or '')
        except Exception:
            account = ''
        self._account_cache = (account, now)
        return account

    # ------------------------------------------------------------ reader

    def _reader_spec(self) -> ManagedProcess | None:
        root = Path(self.cfg['project_root'])
        script = root / 'scripts' / 'reader-api.py'
        if not script.is_file():
            return None
        return ManagedProcess(
            name='reader',
            exe=self.cfg['python'],
            args=[str(script)],
            cwd=str(root),
            marker=str(script),
            port=urlsplit(self.cfg['reader_base_url']).port or 80,
            env={'PYTHONUTF8': '1', 'PYTHONUNBUFFERED': '1'},
        )

    def _image_resolver(self) -> 'wx_image.ImageResolver':
        """图片解析器（W4）：reader 主路径 + 本地降级。懒建，配置变化时重建。"""
        if self._img_resolver is None:
            self._img_resolver = wx_image.ImageResolver(
                self.cfg['reader_base_url'], self._reader_token())
        return self._img_resolver

    async def _ensure_reader(self) -> bool:
        spec = self._reader_spec()
        if spec is None or self.supervisor is None:
            log.warning('reader 规格无效，跳过托管')
            return False
        if _port_open(spec.port):
            log.info('reader 已在运行，复用')
            return True
        loop = asyncio.get_running_loop()
        ok, msg = await loop.run_in_executor(None, self.supervisor.ensure_running, spec)
        log.info('启动 reader: %s %s', ok, msg)
        return ok

    # ------------------------------------------------------------ hook

    async def _check_hook(self, *, warn_only: bool = False) -> bool:
        if self.hook is None:
            return False
        loop = asyncio.get_running_loop()
        try:
            health = await loop.run_in_executor(None, self.hook.health)
            ok = (health.get('integration') == 'WeChatAgent-1'
                  and health.get('backend') == 'wechat-hook'
                  and health.get('startupState') == 'ready')
            if not ok:
                log.warning('hook 未就绪: %s', health.get('startupState') or health)
            return ok
        except Exception as exc:
            if warn_only:
                log.warning('hook 不可达（微信可能未启动或未登录）: %s', exc)
            return False

    # ------------------------------------------------------------ server

    async def _start_server(self) -> None:
        # WX-002：server.enable 在启动/热更/守护三处统一生效
        if self.server is not None or self.reader is None or self.hook is None:
            return
        if not self.cfg.get('ob_enable', True):
            log.debug('OneBot 服务端已在配置中关闭，跳过启动')
            return
        self.server = OneBotServer(
            self.cfg['ob_host'], self.cfg['ob_port'], self.cfg['ob_token'],
            self.hook, self.reader, self._account,
        )
        # R9：外部 OneBot 与 MaiBot 网关共用统一出站入口
        self.server.send_outbound = self.send_outbound
        await self.server.start()

    async def _start_sse(self) -> None:
        if self.sse is not None or self.reader is None:
            return
        # WX-009：self_id 短缓存 —— 没有 OneBot 客户端时避免逐消息查询 reader
        self._self_id_cache = (0, 0.0)

        async def on_event(payload: dict[str, Any]) -> None:
            # 1) 投递给 MaiBot 聊天管线
            await self._submit_inbound(payload)
            # 2) 同时保留 OneBot 推送，便于外部工具直连调试
            if self.server is not None:
                event = to_onebot_event(payload, self.server._self_id(), conversation_id)
                if event is not None:
                    await self.server.push_message(event)

        self.sse = SseConsumer(self.cfg['reader_base_url'], self._reader_token(), on_event)
        self.sse.on_reconnected = getattr(self, '_on_sse_reconnected', None)
        await self.sse.start()

    async def _on_sse_reconnected(self) -> None:
        """SSE 重连回调（WX-005→006）：按水位补收断线窗口。"""
        if self.recovery is None:
            return
        try:
            n = await self.recovery.backfill_gap(reason='sse-reconnect')
            if n:
                log.info('SSE 重连补收: %d 条', n)
        except Exception as exc:
            log.warning('重连补收失败: %s', exc)

    # ------------------------------------------------------------ 守护

    async def _watch(self) -> None:
        interval = max(5.0, self.cfg.get('watchdog_seconds', 15))
        while True:
            try:
                await asyncio.sleep(interval)
                if self.supervisor is not None:
                    self.supervisor.reap_dead()

                # reader 掉了就拉起来
                if self.cfg.get('autostart_reader', True) and not _port_open(5031):
                    log.warning('reader 端口不通，尝试重启')
                    await self._ensure_reader()

                # OneBot 服务端掉了才重启；被配置关闭的绝不拉起（WX-002）
                if self.cfg.get('ob_enable', True):
                    if self.server is None:
                        await self._start_server()
                    elif not _port_open(self.cfg['ob_port']):
                        log.warning('OneBot 服务端不通，重启')
                        try:
                            await self.server.stop()
                        except Exception:
                            pass
                        self.server = None
                        await self._start_server()
                elif self.server is not None:
                    # 配置已关闭但实例还在：收敛到关闭态
                    try:
                        await self.server.stop()
                    except Exception:
                        pass
                    self.server = None

                # SSE 消费者内部有统一重连退避（WX-005）：
                # 只有实例丢失时才重建，避免看门狗反复取消仍在连接中的任务。
                if self.sse is None:
                    await self._start_sse()
                elif (not self.sse.connected and self.sse.gave_up
                      and not self.sse.running):
                    log.info('SSE 重连已放弃且任务退出，重建消费者')
                    await self.sse.stop()
                    self.sse = None
                    await self._start_sse()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('守护循环异常: %s', exc)

    # ------------------------------------------------------------ 状态

    def status(self) -> dict[str, Any]:
        reader_ok = _port_open(5031)
        hook_ok = False
        try:
            health = self.hook.health() if self.hook else {}
            hook_ok = health.get('startupState') == 'ready'
        except Exception:
            pass
        return {
            'component': 'wxsupervisor',
            'reader': {'port': 5031, 'listening': reader_ok},
            'hook': {'port': 30001, 'ready': hook_ok},
            'onebot': {
                'port': self.cfg.get('ob_port'),
                'listening': _port_open(int(self.cfg.get('ob_port') or 7999)),
                'clients': len(self.server._clients) if self.server else 0,
            },
            'sse': {
                'connected': bool(self.sse and self.sse.connected),
                'received': int(self.sse.received) if self.sse else 0,
                'last_error': self.sse.last_error if self.sse else '',
            },
            'usage': self.usage.snapshot(),
            'replay_pending': self.replay.pending(),
        }


def create_plugin() -> WxSupervisorPlugin:
    """MaiBot 插件工厂函数。

    加载器读的是 `<插件目录>/plugin.py`，并要求该模块级导出 create_plugin。
    放在这里而不是 __init__.py —— 加载器不会去 import __init__。
    """
    return WxSupervisorPlugin()
