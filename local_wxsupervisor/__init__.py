"""微信链路统一托管插件入口。"""

from __future__ import annotations

from .plugin import WxSupervisorPlugin


def create_plugin() -> WxSupervisorPlugin:
    """创建插件实例。"""
    return WxSupervisorPlugin()


__all__ = ["WxSupervisorPlugin", "create_plugin"]
