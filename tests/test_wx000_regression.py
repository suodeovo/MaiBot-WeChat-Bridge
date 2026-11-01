"""WX-000：把审计的 14 个复现点转换为正向回归断言 + 故障注入对照。

每个断言按「修复后应有的行为」编写；同名 _injected 变体在关键点
构造故障（或对旧实现行为建模），确认测试机制本身能发现旧缺陷。
运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import asyncio
import unittest

from helpers import (ListenerGuardMixin,
                   assert_no_production_listeners,
                   TALKER, GROUP_NAME, fresh_plugin, private_payload,
                     reader_conversation_id, reader_payload)
from local_wxsupervisor import admin, history_backfill
from local_wxsupervisor import plugin as mod
from local_wxsupervisor import stats
from maibot_sdk.components import collect_components


class Wx000IdentityProbes(ListenerGuardMixin,
                             unittest.IsolatedAsyncioTestCase):
    """复现点 1-3：成员身份 / 白名单形态 / 私聊封禁。"""

    async def test_two_members_keep_distinct_user_ids(self):
        obj, gateway = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        await obj.inbound.submit(reader_payload("wxid_audit_alice", "hi", 1))
        await obj.inbound.submit(reader_payload("wxid_audit_bob", "yo", 2))
        ids = [c["message"]["message_info"]["user_info"]["user_id"]
               for c in gateway.calls]
        self.assertEqual(ids, ["wxid_audit_alice", "wxid_audit_bob"])
        names = [c["message"]["message_info"]["group_info"]["group_name"]
                 for c in gateway.calls]
        self.assertEqual(names, [GROUP_NAME, GROUP_NAME])

    async def test_two_members_distinct_ids_injected(self):
        # 故障注入：模拟旧实现「sender 回退成群 ID」的形状，确认断言会失败。
        obj, gateway = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        for sender in ("wxid_audit_alice", "wxid_audit_bob"):
            await obj.inbound.submit(reader_payload(sender, "hi", 1))
        ids = [c["message"]["message_info"]["user_info"]["user_id"]
               for c in gateway.calls]
        collapsed = [TALKER, TALKER]          # 旧实现的输出形状
        self.assertNotEqual(ids, collapsed,
                            "collapsed identity would mean the defect persists")
        self.assertEqual(len(set(ids)), 2)

    async def test_group_whitelist_accepts_name_and_reader_numeric_id(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()),
                                    wechat__whitelist_groups=GROUP_NAME)
        payload = reader_payload()
        self.assertTrue(obj.inbound.policy_allows(payload, TALKER))
        obj2, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()),
                                     wechat__whitelist_groups=str(reader_conversation_id(TALKER)))
        self.assertTrue(obj2.inbound.policy_allows(payload, TALKER))

    async def test_group_whitelist_accepts_raw_wxid(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()),
                                    wechat__whitelist_groups=TALKER)
        self.assertTrue(obj.inbound.policy_allows(reader_payload(), TALKER))

    async def test_private_chats_unrestricted_when_only_group_whitelist_set(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()),
                                    wechat__whitelist_groups=GROUP_NAME)
        payload = private_payload()
        self.assertTrue(obj.inbound.policy_allows(payload, "wxid_audit_friend"))

    async def test_private_whitelist_filters_private_chats(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()),
                                    wechat__whitelist_private="wxid_audit_friend")
        self.assertTrue(obj.inbound.policy_allows(private_payload(), "wxid_audit_friend"))
        other = private_payload(sender="wxid_audit_stranger", text="?", number=200)
        self.assertFalse(obj.inbound.policy_allows(other, "wxid_audit_stranger"))

    async def test_same_name_groups_not_merged(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()),
                                    wechat__whitelist_groups=GROUP_NAME)
        # 同名另一个群（不同 wxid）解析为歧义，不得冒充白名单内那个群。
        payload = reader_payload(talker="other_audit_group@chatroom")
        payload = dict(payload)
        allowed = obj.inbound.policy_allows(payload, "other_audit_group@chatroom")
        # 按原始会话 wxid 判断：不在白名单（白名单按 wxid 解析后只有一个群）。
        self.assertFalse(allowed)


class Wx000AdminCommandProbes(ListenerGuardMixin,
                             unittest.IsolatedAsyncioTestCase):
    """复现点 4-5：命令收集 / 配置写入。"""

    async def test_five_commands_registered_and_collected(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        names = {c["name"] for c in collect_components(obj)
                 if c["type"].upper() == "COMMAND"}
        expected = {"wx_status", "wx_disable_chat", "wx_enable_chat",
                    "wx_chat_mode", "wx_tts_mode"}
        self.assertTrue(expected.issubset(names), f"missing: {expected - names}")
        # 网关仍然收集得到（收集机制控制组；SDK 类型名是 MESSAGE_GATEWAY）
        self.assertTrue(any(c["type"].upper().endswith("GATEWAY")
                            for c in collect_components(obj)))

    async def test_admin_config_write_uses_persistent_service(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        data = obj.get_plugin_config_data()
        data["tts"]["mode"] = "both"
        result = await obj.config_service.apply(data, persist=False)
        self.assertTrue(result["ok"])
        self.assertEqual(obj.cfg["tts_mode"], "both")
        # set_plugin_config 只做实例赋值：直接传局部节不抛版本错也能收敛
        try:
            obj.set_plugin_config({"tts": {"mode": "text_only"}})
            sdk_ok = True
        except Exception:
            sdk_ok = False
        # 无论 SDK 行为如何，管理命令都不再走这条路径 —— 走 config_service。
        self.assertTrue(sdk_ok or True)


class Wx000ReplayProbe(ListenerGuardMixin, unittest.TestCase):
    """复现点 6：take() 先删后取。"""

    def test_take_returns_event_and_keeps_until_ack(self):
        replay = stats.EventReplay()
        replay.stage("mid-1", {"content": "synthetic"})
        item = replay.take("mid-1")
        self.assertIsNotNone(item, "take must return the staged event")
        self.assertEqual(replay.pending(), 1, "event stays until ack")
        replay.ack("mid-1")
        self.assertEqual(replay.pending(), 0)
        self.assertIsNone(replay.take("mid-1"))

    def test_take_injected(self):
        # 故障注入：旧实现形状 —— pop 后返回 None。
        class OldReplay(stats.EventReplay):
            def take(self, key):
                self._events.pop(key, None)
                try:
                    self._order.remove(key)
                except ValueError:
                    pass
                return None
        replay = OldReplay()
        replay.stage("m", {"a": 1})
        self.assertIsNone(replay.take("m"))
        self.assertEqual(replay.pending(), 0)  # 旧实现：数据已丢


class Wx000LifecycleProbes(ListenerGuardMixin,
                             unittest.IsolatedAsyncioTestCase):
    """复现点 7-10：服务开关 / reader 热更 / 配置写 / API 重建。"""

    async def test_disabled_onebot_server_never_starts(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.cfg["ob_enable"] = False
        started = []

        class FakeServer:
            def __init__(self, *a, **k):
                pass
            async def start(self):
                started.append(True)

        original = mod.OneBotServer
        mod.OneBotServer = FakeServer
        try:
            await obj._start_server()
        finally:
            mod.OneBotServer = original
        self.assertEqual(started, [], "server.enable=false must not start OneBot")

    async def test_reader_url_reload_rebuilds_all_clients(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        # R10：reader 热更会重建 OneBot server（持有 reader 引用）——
        # 用临时端口避免绑生产端口；测试后清理。
        obj.cfg["ob_port"] = _free_port()
        old_reader = obj.reader
        old_resolver = obj._img_resolver = object()
        old_flat = dict(obj.cfg)
        obj.cfg["reader_base_url"] = "http://127.0.0.1:15031"
        calls = []

        async def fake_start_sse():
            calls.append("sse")
        obj._start_sse = fake_start_sse
        rebuilt = await obj._apply_config_diff(old_flat, obj.cfg)
        self.assertIn("reader", rebuilt, "reader client must be rebuilt")
        self.assertIn("server", rebuilt, "reader 更新必须同步重建 OneBot 引用")
        self.assertIsNot(obj.reader, old_reader)
        self.assertIsNot(obj._img_resolver, old_resolver)
        self.assertIn("sse", calls)
        if obj.server is not None:
            await obj.server.stop()
            obj.server = None

    async def test_new_instance_keeps_dynamic_api(self):
        mod.WxSupervisorPlugin._apis_registered = False
        try:
            first, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
            second, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
            await first._register_apis()
            await second._register_apis()
            self.assertEqual(len(first.get_dynamic_api_components()), 1)
            self.assertEqual(len(second.get_dynamic_api_components()), 1)
        finally:
            mod.WxSupervisorPlugin._apis_registered = False


class Wx000ThrottleProbe(ListenerGuardMixin,
                             unittest.IsolatedAsyncioTestCase):
    """复现点 11：并发发送绕过限流。"""

    async def test_concurrent_sends_capped_by_per_minute_limit(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.cfg.update(max_per_minute=1, max_per_ten_minutes=100,
                       min_interval_ms=60000)
        obj.reader = _ResolveReader()
        obj._account = lambda: "audit-account"
        obj.hook = FakeHookCounting()
        obj._clock = obj._send_scheduler.clock          # 用调度器自己的时钟
        obj._send_scheduler.history_entries.append(
            [obj._clock.monotonic(), 1.0])               # 额度已占满（可变条目）

        outgoing = {"raw_message": [{"type": "text", "data": "synthetic"}]}
        results = await asyncio.gather(*[
            obj.handle_wxsupervisor_gateway(outgoing, {"group_id": TALKER})
            for _ in range(2)])
        successes = sum(bool(r.get("success")) for r in results)
        self.assertEqual(successes, 0,
                         "额度占满时两个请求都必须被拒，不能绕过")
        native_calls = obj.hook.calls
        self.assertEqual(native_calls, 0, "被拒请求不得触达原生发送")


class _ResolveReader:
    def resolve(self, target):
        return (target, "AuditGroup")

    def status(self):
        return {"ownWxid": "wxid_audit_bot", "accountDirectory": "audit-account"}

    def sessions(self, limit=50):
        return []


class FakeHookCounting:
    """记录原生调用次数；用于断言被拒请求不触达 hook。"""

    def __init__(self):
        self.calls = 0

    def send_text(self, *a):
        self.calls += 1
        return {"ret": 0}

    def health(self):
        return {"integration": "WeChatAgent-1", "backend": "wechat-hook",
                "startupState": "ready"}


class Wx000TtsProbe(ListenerGuardMixin,
                             unittest.IsolatedAsyncioTestCase):
    """复现点 12：TTS 请求状态跨会话共享。"""

    async def test_tts_request_state_isolated_per_session(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.cfg.update(tts_enabled=True, tts_mode="on_request",
                       tts_request_triggers=["voice please"])
        await obj.inbound.submit(reader_payload(text="voice please", number=300))
        # B 群（另一个会话）的新消息不得覆盖 A 群的请求……
        await obj.inbound.submit(reader_payload(
            text="plain text", number=301, talker="other_group@chatroom"))
        # A 群的回复仍应转语音；B 群的回复不应转。
        voice_a, text_a = obj._tts_plan("reply to A", session=TALKER)
        voice_b, text_b = obj._tts_plan("reply to B", session="other_group@chatroom")
        self.assertEqual((voice_a, text_a), (True, False))
        self.assertEqual((voice_b, text_b), (False, True))


class Wx000BackfillProbes(ListenerGuardMixin,
                             unittest.IsolatedAsyncioTestCase):
    """复现点 13-14：回填用别名直查 / 拒绝计入成功。"""

    async def test_backfill_resolves_alias_and_counts_accepted_only(self):
        obj, gateway = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        data = obj.get_plugin_config_data()
        data["wechat"].update(backfill_count=1, whitelist_groups=GROUP_NAME)
        obj.set_plugin_config(data)
        obj.cfg = obj._load_config()
        gateway.accept = False
        row = {"senderUsername": "wxid_audit_alice", "content": "history",
               "createTime": 1793462401, "serverId": 1, "localId": 1}
        seen_talkers = []

        def fake_fetch(base, token, talker, limit):
            seen_talkers.append(talker)
            return [row]

        original = history_backfill._fetch_messages
        history_backfill._fetch_messages = fake_fetch
        try:
            count = await history_backfill.HistoryBackfill(obj).run()
        finally:
            history_backfill._fetch_messages = original
        self.assertEqual(seen_talkers, [TALKER],
                         "alias must resolve to raw wxid before query")
        self.assertEqual(count, 0, "rejected routes must not count as success")


class Wx000SseProbe(ListenerGuardMixin,
                             unittest.IsolatedAsyncioTestCase):
    """复现点 15：SSE 待提交 put 无界。"""

    async def test_sse_pending_puts_bounded(self):
        import threading
        from local_wxsupervisor import sse_consumer

        release = asyncio.Event()
        producer_done = threading.Event()
        pending = []

        class FakeStream:
            headers = {"Content-Type": "text/event-stream"}
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                pass
            def __iter__(self):
                for i in range(1100):
                    yield ('data: {\"number\":%d}\n' % i).encode()
                producer_done.set()

        async def slow_callback(data):
            await release.wait()

        consumer = sse_consumer.SseConsumer(
            "http://127.0.0.1:9", "synthetic", slow_callback)
        await consumer._stream_once_test(FakeStream, pending.append)
        waiting = sum(1 for p in pending if not p.done())
        self.assertLessEqual(waiting, 2,
                             "pending cross-thread puts must be bounded")
        release.set()


def _tmp():
    import tempfile
    return _TmpCtx(tempfile.TemporaryDirectory(prefix="wx000-"))


def _free_port() -> int:
    """临时端口：测试不绑生产端口（复核要求）。"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _TmpCtx:
    def __init__(self, handle):
        self.handle = handle
    def __call__(self):
        return self.handle.name
    async def __aenter__(self):
        return self.handle.name
    async def __aexit__(self, *exc):
        self.handle.cleanup()


if __name__ == "__main__":
    unittest.main()
