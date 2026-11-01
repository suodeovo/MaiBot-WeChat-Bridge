"""插件配置服务（WX-002 / 返修 R10）：配置事务。

事务顺序（R10）：

1. **版本校验前置**：变更里显式带了 `plugin.config_version` 且不等于
   支持版本 → 直接拒绝，不改写；只有「未提供版本」时才补缺省值。
2. **保存真回滚状态**：应用前深拷贝旧的实例配置数据 + 扁平 cfg +
   组件引用（reader/hook/server/sse/img_resolver/recovery/账号缓存）。
3. **应用 + await 重建**：串行 await `_apply_config_diff`（不再
   create_task 后台跑）；重建异常视为应用失败。
4. **失败回滚**：从「应用前保存的快照」恢复实例配置、扁平 cfg 和
   全部组件引用，回滚值绝不从已修改的内存读取。
5. **持久化**：回滚成功后才写盘；写盘失败明确暴露。
"""
from __future__ import annotations

import asyncio
import copy
import logging
import tomllib
from pathlib import Path
from typing import Any, Mapping

from .config import SUPPORTED_CONFIG_VERSION, WxSupervisorSettings
from .toml_writer import dumps_toml

log = logging.getLogger('wxsupervisor.config_service')


class ConfigValidationError(ValueError):
    """配置验证失败。"""


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(dict(out[key]), value)
        else:
            out[key] = value
    return out


class _ComponentSnapshot:
    """R10/V2-6：组件生命周期快照。

    回滚时：stop 全部事务期间新建的组件 → 恢复引用 → **按旧配置真正
    重建已 stop 的旧组件**（引用恢复不等于服务恢复）。
    active 判定通过组件自身的 active 属性（缺省视为 True）。
    """

    __slots__ = ('reader', 'hook', 'server', 'sse', 'img_resolver',
                 'recovery', 'account_cache', 'new_components',
                 'orig_start_server', 'orig_start_sse')

    def __init__(self, plugin: Any) -> None:
        self.reader = getattr(plugin, 'reader', None)
        self.hook = getattr(plugin, 'hook', None)
        self.server = getattr(plugin, 'server', None)
        self.sse = getattr(plugin, 'sse', None)
        self.img_resolver = getattr(plugin, '_img_resolver', None)
        self.recovery = getattr(plugin, 'recovery', None)
        self.account_cache = getattr(plugin, '_account_cache', ('', 0.0))
        self.new_components: list[Any] = []
        # 事务前的组件工厂（回滚重建必须用它，而不是事务期包装/测试 patch）
        self.orig_start_server = getattr(plugin, '_start_server', None)
        self.orig_start_sse = getattr(plugin, '_start_sse', None)

    @staticmethod
    def _is_active(component: Any) -> bool:
        """G3：组件健康判定。

        优先级：
        1. 组件显式 `is_active()` 方法（真实组件接口）；
        2. `active` 属性（测试替身）；
        3. 无任何接口的组件（ReaderClient 等无生命周期对象）→ 视为可用。
        真实 SseConsumer/OneBotServer 实现 is_active()，stop 后返回 False。
        """
        if component is None:
            return False
        is_active = getattr(component, 'is_active', None)
        if callable(is_active):
            try:
                return bool(is_active())
            except Exception:
                return False
        active = getattr(component, 'active', None)
        if active is not None:
            return bool(active)
        return True          # 无生命周期接口的对象视为可用

    def register_new(self, component: Any) -> None:
        if component is not None and component not in (
                self.reader, self.hook, self.server, self.sse):
            self.new_components.append(component)

    async def stop_new_components(self) -> None:
        for component in self.new_components:
            stop = getattr(component, 'stop', None)
            if callable(stop):
                try:
                    await stop()
                except Exception as exc:
                    log.warning('回滚停止新组件失败: %s', exc)
        self.new_components.clear()

    def restore_refs(self, plugin: Any) -> None:
        plugin.reader = self.reader
        plugin.hook = self.hook
        plugin.server = self.server
        plugin.sse = self.sse
        plugin._img_resolver = self.img_resolver
        plugin.recovery = self.recovery
        plugin._account_cache = self.account_cache

    async def rebuild_stopped(self, plugin: Any) -> None:
        """按旧配置重建已停止的旧组件（V2-6 核心）。

        使用事务前保存的组件工厂（绕开事务期包装），完成后还原。
        """
        current_start_server = getattr(plugin, '_start_server', None)
        current_start_sse = getattr(plugin, '_start_sse', None)
        if self.orig_start_sse is not None:
            plugin._start_sse = self.orig_start_sse
        if self.orig_start_server is not None:
            plugin._start_server = self.orig_start_server
        try:
            if not self._is_active(self.sse):
                plugin.sse = None
                await plugin._start_sse()
            if not self._is_active(self.server):
                plugin.server = None
                await plugin._start_server()
        finally:
            plugin._start_sse = current_start_sse
            plugin._start_server = current_start_server


class PluginConfigService:
    """插件配置的唯一写入路径（WX-003 管理命令也走这里）。"""

    def __init__(self, plugin: Any, config_path: Path | None = None) -> None:
        self.plugin = plugin
        self.config_path = config_path or (Path(plugin.cfg_source_dir())
                                           / 'config.toml')

    # ---------------------------------------------------- 读取

    def load_full(self) -> dict[str, Any]:
        data = self.plugin.get_plugin_config_data()
        if data:
            return data
        return self._read_disk()

    def _read_disk(self) -> dict[str, Any]:
        try:
            with self.config_path.open('rb') as fh:
                return tomllib.load(fh)
        except FileNotFoundError:
            return {}
        except Exception as exc:
            log.warning('config.toml 读取失败，使用空配置: %s', exc)
            return {}

    # ---------------------------------------------------- 校验（R10.4）

    def validate(self, full: Mapping[str, Any]) -> WxSupervisorSettings:
        plugin_section = full.get('plugin')
        if not isinstance(plugin_section, Mapping):
            raise ConfigValidationError('缺少 [plugin] 节')
        version = str(plugin_section.get('config_version') or '').strip()
        if not version:
            raise ConfigValidationError('缺少 plugin.config_version')
        if version != SUPPORTED_CONFIG_VERSION:
            # R10：显式传入的不兼容版本直接拒绝，绝不改写
            raise ConfigValidationError(
                f'配置版本不兼容: {version} != {SUPPORTED_CONFIG_VERSION}')
        try:
            settings = WxSupervisorSettings.model_validate(dict(full))
        except Exception as exc:
            raise ConfigValidationError(f'模型校验失败: {exc}') from exc
        import logging as _logging
        if not settings.validate_runtime(_logging.getLogger('wxsupervisor.config')):
            raise ConfigValidationError('runtime 校验未通过')
        return settings

    # ---------------------------------------------------- 事务（R10）

    async def apply(self, changes: Mapping[str, Any], *,
                    persist: bool = True,
                    merge: bool = True) -> dict[str, Any]:
        """应用一次配置修改（完整事务，await 重建）。"""
        # ---- 1) 合并与版本校验（未提供版本才补缺省）
        current = self.load_full()
        # merge=False：changes 是新的完整配置意图，但缺失节仍回退 current
        # —— 整体替换不允许把 server/reader 等节悄悄掉回出厂默认（R10：
        # 部分缺失导致的默认值漂移本身就是一种静默配置丢失）。
        full = _deep_merge(changes, {}) if not merge else _deep_merge(current, changes)
        if not merge:
            # changes 未提供的节从 current 补齐（顶层节级）
            for section_key, section_value in current.items():
                if section_key not in full:
                    full[section_key] = copy.deepcopy(section_value)
        if 'plugin' not in full or not isinstance(full.get('plugin'), Mapping):
            full.setdefault('plugin', {})
        provided_version = ''
        if merge and isinstance(changes.get('plugin'), Mapping):
            provided_version = str(changes['plugin'].get('config_version') or '').strip()
        elif not merge:
            provided_version = str((changes.get('plugin') or {}).get(
                'config_version') if isinstance(changes.get('plugin'), Mapping)
                else '')
        if not str(full['plugin'].get('config_version') or '').strip():
            full['plugin']['config_version'] = SUPPORTED_CONFIG_VERSION
        # merge 场景：changes 只改其他节、未动版本时保留 current 的版本；
        # changes 显式带了不同版本 → validate 会拒绝
        if not provided_version and merge:
            full['plugin']['config_version'] = str(
                current.get('plugin', {}).get('config_version')
                or SUPPORTED_CONFIG_VERSION)
        try:
            settings = self.validate(full)
        except ConfigValidationError as exc:
            return {'ok': False, 'persisted': False, 'error': str(exc)}

        # ---- 2) 真回滚快照（R10.2）：应用前保存
        old_config_data = copy.deepcopy(self.plugin.get_plugin_config_data())
        old_flat = copy.deepcopy(self.plugin.cfg or {})
        old_components = _ComponentSnapshot(self.plugin)

        # ---- 3) 应用 + await 重建（R10.1 串行；V2-6 新建组件登记）
        self.plugin.set_plugin_config(dict(full))
        new_flat = self.plugin._load_config()
        self.plugin.cfg = new_flat
        # 包装组件工厂：事务期间新建的 server/sse 都登记进快照
        original_start_server = self.plugin._start_server
        original_start_sse = self.plugin._start_sse

        async def tracked_start_server():
            await original_start_server()
            old_components.register_new(self.plugin.server)

        async def tracked_start_sse():
            await original_start_sse()
            old_components.register_new(self.plugin.sse)

        self.plugin._start_server = tracked_start_server
        self.plugin._start_sse = tracked_start_sse
        try:
            await self.plugin._apply_config_diff(old_flat, new_flat)
        except asyncio.CancelledError:
            await self._rollback(old_config_data, old_flat, old_components)
            raise
        except Exception as exc:
            log.exception('配置应用失败，回滚到旧状态')
            restored = await self._rollback(old_config_data, old_flat,
                                            old_components)
            error = f'apply failed: {exc}'
            if not restored:
                error += '; ROLLBACK FAILED - manual intervention required'
            return {'ok': False, 'persisted': False, 'error': error}
        finally:
            # 还原包装的组件工厂
            self.plugin._start_server = original_start_server
            self.plugin._start_sse = original_start_sse

        # ---- 4) 持久化
        persisted = True
        persist_error = ''
        if persist:
            try:
                self.persist(full)
            except Exception as exc:
                persisted = False
                persist_error = str(exc)
                log.error('配置写盘失败（运行态已应用）: %s', exc)
        return {'ok': True, 'persisted': persisted, 'error': persist_error}

    async def _rollback(self, old_config_data: dict[str, Any],
                        old_flat: dict[str, Any],
                        old_components: _ComponentSnapshot) -> bool:
        """V2-6：回滚 = stop 新建组件 + 恢复旧引用 + **按旧配置重建**

        旧组件在事务中可能已被 stop —— 只恢复引用不会让服务复活。
        这里在恢复引用后，对「已 stop 的旧组件」按旧配置真正重建；
        成功后服务恢复可收发。
        """
        try:
            # 1) 停止并清理事务期间新建的组件
            await old_components.stop_new_components()
            # 2) 恢复配置与引用
            if old_config_data:
                self.plugin.set_plugin_config(copy.deepcopy(old_config_data))
            self.plugin.cfg = copy.deepcopy(old_flat)
            old_components.restore_refs(self.plugin)
            # 3) 按旧配置重建已停止的旧组件（active 检测）
            await old_components.rebuild_stopped(self.plugin)
            return True
        except Exception:
            log.exception('配置回滚失败')
            return False

    def persist(self, full: Mapping[str, Any]) -> None:
        """原子写盘。"""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix('.toml.tmp')
        payload = dumps_toml(dict(full))
        tmp.write_text(payload, encoding='utf-8')
        tmp.replace(self.config_path)
