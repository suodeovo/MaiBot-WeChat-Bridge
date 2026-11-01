"""WX-003 管理命令专项：权限矩阵、回复 await、配置保存。"""
from __future__ import annotations

import unittest

from helpers import ListenerGuardMixin, assert_no_production_listeners, GROUP_NAME, TALKER, fresh_plugin, reader_conversation_id


def _tmp():
    import tempfile
    return _TmpCtx(tempfile.TemporaryDirectory(prefix="wx003-"))


class _TmpCtx:
    def __init__(self, handle):
        self.handle = handle
    def __call__(self):
        return self.handle.name
    async def __aenter__(self):
        return self.handle.name
    async def __aexit__(self, *exc):
        self.handle.cleanup()


class _RecordingSend:
    """记录 send.text 调用与 await 行为。"""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    async def text(self, text: str, stream_id: str):
        self.calls.append((text, stream_id))
        if self.fail:
            return False
        return True


class Wx003AdminTests(ListenerGuardMixin,
                             unittest.IsolatedAsyncioTestCase):
    async def _make(self, admins: str = "wxid_boss"):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()),
                                    wechat__administrators=admins)
        obj._ctx.send = _RecordingSend()
        return obj

    async def test_empty_admin_list_rejects_everything(self):
        obj = await self._make(admins="")
        ok, text, sent = await obj.wx_status(stream_id="s", user_id="wxid_anyone")
        self.assertTrue(sent, "拒绝回复必须发出且被 await")
        self.assertIn("管理员", text)
        ok, text, sent = await obj.wx_tts_mode(stream_id="s", user_id="wxid_anyone",
                                               mode="both")
        self.assertTrue(sent)
        self.assertIn("管理员", text)
        # 关键：写操作未生效
        self.assertNotEqual(obj.cfg["tts_mode"], "both")

    async def test_admin_exact_match_allows(self):
        obj = await self._make(admins="wxid_boss, wxid_second")
        ok, text, sent = await obj.wx_status(stream_id="s", user_id="wxid_boss")
        self.assertTrue(ok)
        self.assertTrue(sent)
        self.assertIn("reader", text)

    async def test_similar_wxid_rejected(self):
        obj = await self._make(admins="wxid_boss")
        # 子串/后缀都不算：wxid_boss_admin、boss、xxwxid_boss 均拒绝
        for uid in ("wxid_boss_admin", "boss", "xxwxid_boss"):
            ok, text, sent = await obj.wx_tts_mode(stream_id="s", user_id=uid,
                                                   mode="both")
            self.assertTrue(sent, f"{uid!r} 的拒绝回复必须发出")
            self.assertIn("管理员", text)
        # 关键：写操作全部未生效
        self.assertNotEqual(obj.cfg["tts_mode"], "both")
        # 精确匹配成功
        ok, text, _ = await obj.wx_tts_mode(stream_id="s", user_id="wxid_boss",
                                            mode="both")
        self.assertTrue(ok)
        self.assertIn("TTS 模式 -> both", text)
        self.assertEqual(obj.cfg["tts_mode"], "both")

    async def test_reply_is_awaited_and_result_checked(self):
        obj = await self._make(admins="wxid_boss")
        obj._ctx.send = _RecordingSend(fail=True)
        ok, text, sent = await obj.wx_status(stream_id="s", user_id="wxid_boss")
        self.assertFalse(ok, "send 返回 False 时命令结果必须失败")
        self.assertEqual(len(obj._ctx.send.calls), 1, "回复确实发出并被 await")

    async def test_write_command_persists_and_survives_rebuild(self):
        import tempfile
        from pathlib import Path
        tmpdir = Path(tempfile.mkdtemp(prefix="wx003p-"))
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()),
                                    wechat__administrators="wxid_boss")
        obj._ctx.send = _RecordingSend()
        obj.config_service.config_path = tmpdir / "config.toml"
        ok, text, _ = await obj.wx_disable_chat(stream_id="s", user_id="wxid_boss",
                                                chat=GROUP_NAME)
        self.assertTrue(ok)
        self.assertIn("已停用", text)
        # 重建实例从磁盘读
        obj2, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj2.config_service.config_path = tmpdir / "config.toml"
        disk = obj2.config_service._read_disk()
        self.assertEqual(disk["wechat"]["disabled_chats"], TALKER)

    async def test_save_failure_reports_failure_not_success(self):
        obj = await self._make(admins="wxid_boss")

        async def broken_apply(*a, **k):
            return {"ok": False, "persisted": False, "error": "disk-full"}
        obj.config_service.apply = broken_apply
        ok, text, _ = await obj.wx_tts_mode(stream_id="s", user_id="wxid_boss",
                                            mode="both")
        self.assertTrue(ok)          # 命令本身执行了
        self.assertIn("保存失败", text)
        self.assertIn("disk-full", text)
        self.assertNotIn("->", text.replace("TTS 模式 ->", ""), "不得回复成功样式")

    async def test_disable_by_numeric_alias_resolves(self):
        obj = await self._make(admins="wxid_boss")
        numeric = str(reader_conversation_id(TALKER))
        ok, text, _ = await obj.wx_disable_chat(stream_id="s", user_id="wxid_boss",
                                                chat=numeric)
        self.assertTrue(ok)
        settings = obj._settings()
        self.assertIn(TALKER, settings.disabled_chat_set(),
                      "数字 ID 必须解析到原始 wxid 再入名单")

    async def test_ambiguous_alias_rejected(self):
        obj = await self._make(admins="wxid_boss")
        # reader 会话表（FakeReader）里没有这两个群；目录注入后由
        # _resolve_admin_chat 的刷新保留（刷新只在成功获取会话时覆盖，
        # FakeReader 不含这两个会话名，display 索引仍在）。
        obj.inbound.policy.directory.update([
            {"username": "g1@chatroom", "displayName": "同名群X", "conversationId": "1"},
            {"username": "g2@chatroom", "displayName": "同名群X", "conversationId": "2"},
        ])
        ok, text, sent = await obj.wx_disable_chat(stream_id="s", user_id="wxid_boss",
                                                   chat="同名群X")
        self.assertTrue(sent)
        self.assertIn("ambiguous", text)
        self.assertNotIn("同名群X", obj._settings().disabled_chat_set(),
                         "歧义别名不得写入停用名单")


if __name__ == "__main__":
    unittest.main()
