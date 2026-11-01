"""WX-009 消费点修正回归（复核）：配置字段到真实消费、SSE 慢媒体并行。"""
from __future__ import annotations

import asyncio
import unittest

from helpers import TALKER, fresh_plugin, reader_payload
from local_wxsupervisor import perf, tts, wx_image


def _tmp():
    import tempfile
    return _TmpCtx(tempfile.TemporaryDirectory(prefix="wx9fix-"))


class _TmpCtx:
    def __init__(self, handle):
        self.handle = handle
    def __call__(self):
        return self.handle.name
    async def __aenter__(self):
        return self.handle.name
    async def __aexit__(self, *exc):
        self.handle.cleanup()


class ConfigConsumers(unittest.IsolatedAsyncioTestCase):
    async def test_media_fields_in_flat_config(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        for key in ('media_ttl_hours', 'media_max_mb', 'media_workers',
                    'pilk_paths', 'crypto_paths', 'media_scripts_dir'):
            self.assertIn(key, obj.cfg, f"{key} 必须进入扁平 cfg（复核）")

    async def test_dependency_paths_reach_real_consumers(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        data = obj.get_plugin_config_data()
        data["project"]["pilk_paths"] = r"C:\synthetic\pilk-a;C:\synthetic\pilk-b"
        data["project"]["crypto_paths"] = r"C:\synthetic\crypto-a"
        data["project"]["media_scripts_dir"] = r"C:\synthetic\scripts"
        obj.set_plugin_config(data)
        obj.cfg = obj._load_config()
        obj._apply_dependency_paths()
        self.assertIn(r"C:\synthetic\pilk-a", tts.PILK_SEARCH_PATHS,
                      "tts.load_pilk 的真实消费点必须收到配置路径")
        self.assertIn(r"C:\synthetic\crypto-a", wx_image.CRYPTO_SEARCH_PATHS,
                      "wx_image 降级解码必须收到配置路径")
        self.assertEqual(wx_image.MEDIA_SCRIPTS_DIR, r"C:\synthetic\scripts")

    async def test_janitor_uses_configured_ttl(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        janitor = perf.MediaJanitor(
            obj.cfg['project_root'],
            ttl_hours=float(obj.cfg.get('media_ttl_hours') or 48),
            max_total_mb=float(obj.cfg.get('media_max_mb') or 512))
        self.assertEqual(janitor.ttl_s, 48 * 3600.0)


class SseSlowMediaParallel(unittest.IsolatedAsyncioTestCase):
    """复核：从真实 SSE 入口验证慢媒体不堵其他会话。"""

    async def test_slow_media_does_not_block_other_sessions(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.recovery = None
        completion_order = []
        release = asyncio.Event()

        async def fake_enrich(payload, *a, **k):
            await release.wait()
            completion_order.append(('media-decoded', payload['sessionId']))
            return None

        from unittest.mock import patch
        # 入站投递快速完成（gateway 已是替身）；媒体解码挂起
        text_payload = reader_payload("wxid_audit_alice", "plain text", 501,
                                      talker="text_group@chatroom")
        img_payload = dict(reader_payload("wxid_audit_alice", "[图片]", 502,
                                          talker=TALKER))
        img_payload['image'] = {'localId': 1, 'serverId': 1, 'createTime': 1}
        img_payload['type'] = 3

        with patch('local_wxsupervisor.wx_image.enrich_image_payload',
                   fake_enrich):
            img_task = asyncio.create_task(obj.inbound.submit(img_payload))
            await asyncio.sleep(0.1)          # 让图片先进入解码（阻塞）
            # 文本会话入站：慢图片阻塞期间必须能完成
            await asyncio.wait_for(obj.inbound.submit(text_payload), timeout=2)
            completion_order.append(('text-done', 'text_group@chatroom'))
            release.set()
            await asyncio.wait_for(img_task, timeout=5)
        self.assertIn(('text-done', 'text_group@chatroom'), completion_order,
                      "文本会话必须在慢图片阻塞期间完成入站")


if __name__ == "__main__":
    unittest.main()
