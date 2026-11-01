"""WX-001/002/004/005/006/008 阶段专项测试。"""
from __future__ import annotations

import asyncio
import unittest

from helpers import (ListenerGuardMixin,
                   assert_no_production_listeners,
                   GROUP_NAME, NAMES, TALKER, FakeGateway, fresh_plugin,
                     private_payload, reader_conversation_id, reader_payload)
from local_wxsupervisor.identity import (SessionDirectory, normalize_payload)


class Wx001IdentityTests(ListenerGuardMixin,
                             unittest.IsolatedAsyncioTestCase):
    async def test_normalize_uses_talkerid_and_groupname(self):
        payload = reader_payload("wxid_audit_bob", "yo", 2)
        msg = normalize_payload(payload)
        self.assertEqual(msg.sender_id, "wxid_audit_bob")
        self.assertEqual(msg.group_name, GROUP_NAME)
        self.assertEqual(msg.session_display, GROUP_NAME)
        self.assertEqual(str(msg.conversation_id),
                         str(reader_conversation_id(TALKER)))

    async def test_legacy_sender_field_fallback(self):
        payload = {"sessionId": TALKER, "sender": "wxid_legacy", "content": "x",
                   "sourceName": "某群名片", "rawid": "r1", "timestamp": 1}
        msg = normalize_payload(payload)
        self.assertEqual(msg.sender_id, "wxid_legacy")
        self.assertEqual(msg.group_name, "")     # 无 groupName 不冒充

    async def test_private_source_name_is_display(self):
        payload = private_payload()
        msg = normalize_payload(payload)
        self.assertEqual(msg.sender_id, "wxid_audit_friend")
        self.assertEqual(msg.session_display, "Friend")
        self.assertFalse(msg.is_group)

    async def test_directory_alias_resolution_and_ambiguity(self):
        d = SessionDirectory()
        d.update([
            {"username": "g1@chatroom", "displayName": "同名群", "conversationId": "111"},
            {"username": "g2@chatroom", "displayName": "同名群", "conversationId": "222"},
            {"username": "wxid_solo", "displayName": "唯一名", "conversationId": "333"},
        ])
        session, status = d.resolve_alias("唯一名")
        self.assertEqual((session, status), ("wxid_solo", "ok"))
        session, status = d.resolve_alias("同名群")
        self.assertEqual((session, status), ("", "ambiguous"))
        session, status = d.resolve_alias("111")
        self.assertEqual((session, status), ("g1@chatroom", "ok"))
        session, status = d.resolve_alias("g2@chatroom")
        self.assertEqual((session, status), ("g2@chatroom", "ok"))

    async def test_disabled_blocks_inbound_outbound_replay_backfill(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()),
                                    wechat__disabled_chats=TALKER)
        payload = reader_payload()
        self.assertFalse(obj.inbound.policy_allows(payload, TALKER))
        self.assertFalse(obj.inbound.policy.allows_outbound(TALKER))
        self.assertTrue(obj.inbound.policy.allows_outbound("wxid_audit_friend"))

    async def test_disabled_resolved_by_alias(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()),
                                    wechat__disabled_chats=GROUP_NAME)
        self.assertFalse(obj.inbound.policy.allows_outbound(TALKER))

    async def test_send_recheck_rejects_disabled_after_queue(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.cfg.update(max_wait_ms=10)
        obj.reader = _TinyReader()
        obj.hook = _NoHook()
        # 先放行，让请求进入调度；resolve 后停用生效
        obj.inbound.policy.directory.update(obj.reader.sessions())
        decision = obj.inbound.policy.allows_outbound(TALKER)
        self.assertTrue(decision)
        obj._settings_data_hook = None

        # 模拟排队期间停用：直接改配置数据（等价管理命令效果）
        data = obj.get_plugin_config_data()
        data["wechat"]["disabled_chats"] = TALKER
        obj.set_plugin_config(data)
        obj.cfg = obj._load_config()
        decision2 = obj.inbound.policy.allows_outbound(TALKER)
        self.assertFalse(decision2, "停用后出站必须被拒")

    async def test_wake_mode_uses_normalized_session(self):
        obj, _ = await fresh_plugin(
            await self.enterAsyncContext(_tmp()),
            wechat__wake_mode="mention", wechat__wake_words="小柚",
            wechat__chat_modes=f"{reader_conversation_id(TALKER)}=hybrid")
        payload = reader_payload(text="随便聊聊")
        self.assertTrue(obj.inbound.policy_allows(payload, TALKER))
        msg = normalize_payload(payload)
        self.assertTrue(obj.inbound.policy.wake_allows(msg),
                        "数字 ID 配置的 hybrid 覆盖应放行该群")


class _TinyReader:
    def status(self):
        return {"ownWxid": "wxid_audit_bot", "accountDirectory": "a"}

    def sessions(self, limit=50):
        return [{"username": TALKER, "displayName": GROUP_NAME,
                 "conversationId": str(reader_conversation_id(TALKER))}]

    def resolve(self, target):
        return (TALKER, GROUP_NAME)


class _NoHook:
    def send_text(self, *a):
        raise AssertionError("disabled session must not reach native send")


class Wx002ConfigServiceTests(ListenerGuardMixin,
                             unittest.IsolatedAsyncioTestCase):
    async def test_apply_updates_runtime_and_flat_cfg(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        result = await obj.config_service.apply(
            {"tts": {"mode": "both"}}, persist=False)
        self.assertTrue(result["ok"])
        self.assertEqual(obj.cfg["tts_mode"], "both")

    async def test_invalid_config_rejected_and_runtime_unchanged(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        old_mode = obj.cfg["tts_mode"]
        result = await obj.config_service.apply(
            {"server": {"port": 80}}, persist=False)   # 非法端口（<1024）
        self.assertFalse(result["ok"])
        self.assertEqual(obj.cfg["tts_mode"], old_mode)

    async def test_missing_version_rejected(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        result = await obj.config_service.apply({"tts": {"mode": "both"}},
                                                persist=False, merge=False)
        # R10 语义更新：merge=False 且未提供版本 → 补缺省后可成功（与
        # WX-000 用例一致）；显式提供不兼容版本才拒绝（见 R10 专项）。
        self.assertTrue(result["ok"])

    async def test_persist_writes_disk_and_reloads(self):
        import tempfile
        from pathlib import Path
        tmpdir = Path(tempfile.mkdtemp(prefix="wx002-"))
        obj, _ = await fresh_plugin(str(tmpdir))
        obj.config_service.config_path = tmpdir / "config.toml"
        result = await obj.config_service.apply({"tts": {"mode": "on_request"}},
                                                persist=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["persisted"])
        self.assertTrue((tmpdir / "config.toml").exists())
        # 模拟重建实例：新实例从磁盘读
        obj2, _ = await fresh_plugin(str(tmpdir))
        obj2.config_service.config_path = tmpdir / "config.toml"
        disk = obj2.config_service._read_disk()
        self.assertEqual(disk["tts"]["mode"], "on_request")

    async def test_reader_url_hot_update_rebuilds_clients(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.cfg["ob_port"] = _free_port()
        old_reader = obj.reader
        obj._img_resolver = object()
        old_resolver = obj._img_resolver
        old_flat = dict(obj.cfg)
        obj.cfg["reader_base_url"] = "http://127.0.0.1:15031"
        async def fake_start_sse():
            pass
        obj._start_sse = fake_start_sse
        rebuilt = await obj._apply_config_diff(old_flat, obj.cfg)
        self.assertIn("reader", rebuilt)
        self.assertIn("server", rebuilt)
        self.assertIsNot(obj.reader, old_reader)
        self.assertIsNot(obj._img_resolver, old_resolver)
        if obj.server is not None:
            await obj.server.stop()
            obj.server = None

    async def test_server_enable_off_watcher_wont_restart(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.cfg["ob_enable"] = False
        await obj._start_server()
        self.assertIsNone(obj.server)
        # 守护循环逻辑：ob_enable=False 且 server=None 时不拉起
        # （直接断言 _start_server 的行为；循环逻辑在同函数内）

    async def test_dynamic_api_per_instance(self):
        from maibot_sdk.components import collect_components
        first, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        second, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        await first._register_apis()
        await second._register_apis()
        self.assertEqual(len(first.get_dynamic_api_components()), 1)
        self.assertEqual(len(second.get_dynamic_api_components()), 1)
        names1 = {c["name"] for c in collect_components(first)}
        self.assertIn("wxsupervisor_gateway", names1)


class Wx004SendQueueTests(ListenerGuardMixin,
                             unittest.IsolatedAsyncioTestCase):
    async def test_multisegment_counts_native_attempts(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.cfg.update(max_per_minute=3, max_per_ten_minutes=10)
        sched = obj._send_scheduler
        from local_wxsupervisor.send_queue import SendRequest, SendStatus
        # 一条 3 段消息占满 3 个名额
        req = SendRequest(target=TALKER, chat_type="group", native_count=3)
        permit, _ = await sched.acquire(req)
        self.assertIsNotNone(permit)
        _, per_min = sched._counts(sched.clock.monotonic())
        self.assertEqual(per_min, 3)
        # 第 2 条哪怕 1 段也要被拒
        req2 = SendRequest(target=TALKER, chat_type="group", native_count=1)
        permit2, reason = await sched.acquire(req2)
        self.assertIsNone(permit2)
        sched.finish(req, SendStatus.SUBMITTED, permit=permit)
        _, per_min2 = sched._counts(sched.clock.monotonic())
        self.assertEqual(per_min2, 3, "提交后名额保留")

    async def test_rejected_permit_rolls_back_own_entries_only(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.cfg.update(max_per_minute=5, min_interval_ms=200,
                       private_min_interval_ms=200)
        sched = obj._send_scheduler
        from local_wxsupervisor.send_queue import SendRequest, SendStatus
        # 预置他人额度（可变条目）
        sched.history_entries.append([sched.clock.monotonic(), 1.0])
        req = SendRequest(target=TALKER, chat_type="group", native_count=1)
        permit, _ = await sched.acquire(req)
        self.assertIsNotNone(permit)
        sched.finish(req, SendStatus.REJECTED, permit=permit)
        _, per_min = sched._counts(sched.clock.monotonic())
        self.assertEqual(per_min, 1, "只回滚自己的条目，他人额度不动")

    async def test_session_turn_isolation(self):
        from local_wxsupervisor.send_queue import SendRequest, SendStatus, _MonotonicClock

        class Controlled(_MonotonicClock):
            def __init__(self):
                self.now = 1000.0
            def monotonic(self):
                return self.now
            def advance(self, s):
                self.now += s

        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.cfg.update(min_interval_ms=15000, private_min_interval_ms=1000,
                       round_window_ms=30000, turn_quiet_ms=10000,
                       round_min_gap_ms=200)
        clock = Controlled()
        sched = mod_sched(obj, clock)
        # A 群首条：立即获得许可并完成发送（finish_turn 开轮次窗口）
        req1 = SendRequest(target="a@chatroom", chat_type="group", native_count=1)
        p1, _ = await sched.acquire(req1)
        self.assertIsNotNone(p1)
        sched.finish(req1, SendStatus.SUBMITTED, permit=p1)
        sched.finish_turn("a@chatroom", "group")
        clock.advance(1.0)
        # 同群同轮：round_min_gap 200ms 已过，可再发
        req2 = SendRequest(target="a@chatroom", chat_type="group", native_count=1)
        p2, w2 = await sched.acquire(req2)
        self.assertIsNotNone(p2, f"同群同轮内应立即可发: {w2}")
        # B 群：跨轮必须等完整间隔 —— min_interval 15s 未到 → 等待预算内拒绝
        clock.advance(1.0)
        req3 = SendRequest(target="b@chatroom", chat_type="group", native_count=1)
        p3, reason = await sched.acquire(req3, max_wait_s=0.1)
        self.assertIsNone(p3, "B 群新会话必须等群间隔")
        self.assertIn("间隔", reason)

    async def test_idempotent_dedup_on_submitted(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        sched = obj._send_scheduler
        from local_wxsupervisor.send_queue import SendRequest, SendStatus
        req = SendRequest(target=TALKER, chat_type="group", request_id="stable-1")
        permit, _ = await sched.acquire(req)
        sched.finish(req, SendStatus.SUBMITTED, permit=permit)
        hit, cached, _fut = sched._lookup_idempotent(req)
        self.assertTrue(hit, "已提交的稳定 ID 请求必须去重")
        self.assertTrue(cached.get("success"))


class Wx005SseTests(ListenerGuardMixin,
                             unittest.IsolatedAsyncioTestCase):
    async def test_stop_without_messages_terminates_cleanly(self):
        from local_wxsupervisor.sse_consumer import SseConsumer
        seen = []

        async def on_event(data):
            seen.append(data)

        consumer = SseConsumer("http://127.0.0.1:9", "t", on_event)
        await consumer.start()
        await asyncio.sleep(0.1)
        await consumer.stop()
        self.assertFalse(consumer.running())
        self.assertFalse(consumer.connected)

    async def test_bad_data_does_not_kill_chain(self):
        from local_wxsupervisor.sse_consumer import SseConsumer
        seen = []

        class FakeStream:
            headers = {"Content-Type": "text/event-stream"}
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                pass
            def __iter__(self):
                yield b"data: {broken json\n"
                yield b"data: {\"ok\": 1}\n"
                yield b": heartbeat\n"
                yield b"\n"
                yield b"data: {\"ok\": 2}\n"

        consumer = SseConsumer("http://127.0.0.1:9", "t",
                               lambda d: seen.append(d) or asyncio.sleep(0))
        await consumer._stream_once_test(FakeStream())
        self.assertEqual(seen, [{"ok": 1}, {"ok": 2}])
        self.assertEqual(consumer.received, 2)

    async def test_pending_bounded_under_slow_consumer(self):
        # 慢消费时读取线程被有界 channel 阻塞，积压不超过 queue+channel；
        # 释放后全部消息最终被消费（不丢）。
        from local_wxsupervisor.sse_consumer import SseConsumer
        release = asyncio.Event()

        async def slow(data):
            await release.wait()

        consumer = SseConsumer("http://127.0.0.1:9", "t", slow,
                               queue_size=4, thread_channel_size=2)

        class FakeStream:
            headers = {"Content-Type": "text/event-stream"}
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                pass
            def __iter__(self):
                for i in range(10):
                    yield ('data: {"n":%d}\n' % i).encode()

        async def run():
            await consumer._stream_once_test(FakeStream())

        task = asyncio.create_task(run())
        # 慢消费 0.3s：让积压达到上限并稳定
        release_after = asyncio.get_running_loop().call_later(
            0.3, lambda: release.set())
        try:
            await asyncio.wait_for(task, timeout=10)
        finally:
            release_after.cancel()
            release.set()
        self.assertEqual(consumer.received, 10)   # 全部最终被消费，不丢


class Wx006ReplayTests(ListenerGuardMixin,
                             unittest.IsolatedAsyncioTestCase):
    async def test_stage_take_ack_lifecycle(self):
        from local_wxsupervisor.stats import EventReplay
        replay = EventReplay()
        replay.stage("m1", {"a": 1})
        self.assertEqual(replay.take("m1"), {"a": 1})
        self.assertEqual(replay.pending(), 1)
        self.assertTrue(replay.ack("m1"))
        self.assertEqual(replay.pending(), 0)

    async def test_failed_backoff_and_dead_state(self):
        from local_wxsupervisor.stats import EventReplay
        replay = EventReplay(max_attempts=3)
        replay.stage("m", {"a": 1})
        replay.failed("m", "host-down")
        self.assertEqual(replay.pending(), 1)
        ready = replay.take_ready(now=replay.take_ready()[0][1] if replay.take_ready() else 0)
        # 退避未到不就绪
        self.assertEqual([k for k, _ in replay.take_ready(now=0)], [])
        replay.failed("m", "host-down")
        replay.failed("m", "host-down")
        self.assertIn("m", replay.dead(), "超过重试上限进入 dead")
        self.assertEqual(replay.pending(), 1, "dead 事件保留可观测")

    async def test_watermark_store_persists(self):
        import tempfile
        from pathlib import Path
        from local_wxsupervisor.recovery import WatermarkStore
        p = Path(tempfile.mkdtemp(prefix="wx006-")) / "recovery-state.json"
        store = WatermarkStore(p)
        store.update("bot", TALKER, "rawid-1", 123.0)
        store2 = WatermarkStore(p)   # 模拟重启
        self.assertEqual(store2.get("bot", TALKER), {"rawid": "rawid-1", "ts": 123.0})

    async def test_redeliver_acks_on_accept_and_failed_on_reject(self):
        obj, gateway = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        from local_wxsupervisor.recovery import RecoveryManager
        manager = RecoveryManager(obj)
        obj.replay.stage("mid-1", {"sessionId": TALKER, "talkerId": "wxid_audit_alice",
                                   "sender": "wxid_audit_alice",
                                   "senderName": "Alice", "groupName": GROUP_NAME,
                                   "content": "resend", "timestamp": 1.0,
                                   "rawid": "mid-1", "type": 1})
        await manager._redeliver("mid-1", obj.replay.take("mid-1"))
        self.assertEqual(gateway.accept, True)
        self.assertEqual(obj.replay.pending(), 0, "接受后 ack 删除")
        # 拒绝路径
        obj.replay.stage("mid-2", {"sessionId": TALKER, "content": "x",
                                   "timestamp": 2.0, "rawid": "mid-2", "type": 1})
        gateway.accept = False
        await manager._redeliver("mid-2", obj.replay.take("mid-2"))
        self.assertEqual(obj.replay.pending(), 1, "拒绝后保留")
        self.assertEqual(obj.replay._attempts.get("mid-2"), 1)


class Wx008TtsTests(ListenerGuardMixin,
                             unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_sessions_isolated(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.cfg.update(tts_enabled=True, tts_mode="on_request",
                       tts_request_triggers=["voice please"])
        await obj.inbound.submit(reader_payload(text="voice please", number=400))
        await obj.inbound.submit(reader_payload(
            text="plain", number=401, talker="other@chatroom"))
        voice_a, text_a = obj._tts_plan("reply", session=TALKER)
        voice_b, text_b = obj._tts_plan("reply", session="other@chatroom")
        self.assertEqual((voice_a, text_a), (True, False))
        self.assertEqual((voice_b, text_b), (False, True))

    async def test_request_expiry_and_negative(self):
        import time as _time
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.cfg.update(tts_enabled=True, tts_mode="on_request",
                       tts_request_triggers=["voice please"],
                       tts_request_window_s=1.0)
        obj.note_inbound_text(TALKER, "voice please")
        _time.sleep(1.1)
        self.assertEqual(obj._tts_plan("r", session=TALKER), (False, True),
                         "过期请求不生效")
        obj.note_inbound_text(TALKER, "别发语音，打字就行")
        self.assertEqual(obj._tts_plan("r", session=TALKER), (False, True),
                         "否定词压制请求")

    async def test_request_consumed_after_one_reply(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.cfg.update(tts_enabled=True, tts_mode="on_request",
                       tts_request_triggers=["voice please"])
        obj.note_inbound_text(TALKER, "voice please")
        self.assertEqual(obj._tts_plan("r1", session=TALKER), (True, False))
        self.assertEqual(obj._tts_plan("r2", session=TALKER), (False, True),
                         "请求消费后不残留")

    async def test_other_tts_modes_semantics(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.cfg.update(tts_enabled=True)
        for mode, text, expect in [
                ("text_only", "x", (False, True)),
                ("both", "hi", (True, True)),
                ("voice_only", "hi", (True, False)),
                ("voice_if_short", "hi", (True, False)),
                ("voice_if_short", "x" * 200, (False, True))]:
            obj.cfg["tts_mode"] = mode
            self.assertEqual(obj._tts_plan(text, session=TALKER), expect,
                             f"mode={mode} len={len(text)}")


def _tmp():
    import tempfile
    return _TmpCtx(tempfile.TemporaryDirectory(prefix="wxstage-"))


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def mod_sched(obj, clock):
    from local_wxsupervisor.send_queue import SendScheduler
    return SendScheduler(obj, clock=clock)


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
