"""复核返修回归（R1~R13）：19 个复现点逐项修复后行为断言 + 故障注入。

测试经由真实入口（start/_run/网关/OneBot 分发/配置事务），不用
_stream_once_test 作为唯一 SSE 验收；后台异常与资源泄漏使测试失败。
"""
from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from helpers import (OWN_WXID, TALKER, fresh_plugin, reader_payload)
from local_wxsupervisor import history_backfill, perf, recovery, wx_message
from local_wxsupervisor.identity import normalize_payload
from local_wxsupervisor.onebot_server import OneBotServer
from local_wxsupervisor.sse_consumer import SseConsumer


def _tmp():
    import tempfile
    return _TmpCtx(tempfile.TemporaryDirectory(prefix="rfix-"))


class _TmpCtx:
    def __init__(self, handle):
        self.handle = handle
    def __call__(self):
        return self.handle.name
    async def __aenter__(self):
        return self.handle.name
    async def __aexit__(self, *exc):
        self.handle.cleanup()


class R1RealSse(unittest.IsolatedAsyncioTestCase):
    """R1：真实 start → _run → 流读取 → 回调 → stop（含重连）。"""

    async def test_start_run_callback_stop(self):
        consumer = SseConsumer("http://127.0.0.1:9", "synthetic", AsyncMock())
        with patch("urllib.request.urlopen",
                   side_effect=OSError("network forbidden")) as opener:
            await consumer.start()
            await asyncio.wait_for(
                _wait_true(lambda: bool(consumer.last_error)
                           and opener.call_count >= 1), timeout=5)
            self.assertIn("OSError", consumer.last_error,
                          "真实 _run 必须执行到网络打开（不再 NameError）")
            self.assertGreaterEqual(opener.call_count, 1,
                                    "start 后必须实际尝试打开流")
            await consumer.stop()
        self.assertFalse(consumer.running())

    async def test_full_receive_chain_with_fake_stream(self):
        seen = []

        async def on_event(data):
            seen.append(data)

        consumer = SseConsumer("http://127.0.0.1:9", "synthetic", on_event,
                               queue_size=8, thread_channel_size=2)
        with patch("urllib.request.urlopen") as opener:
            opener.return_value = _FakeResponse([
                b'data: {"n": 1}\n\n', b': heartbeat\n\n',
                b'data: {"n": 2}\n\n', b"data: broken\n\n",
            ])
            await consumer.start()
            await asyncio.wait_for(_wait_true(lambda: consumer.received >= 2),
                                   timeout=5)
            self.assertEqual(seen, [{"n": 1}, {"n": 2}],
                             "坏数据跳过、心跳跳过、合法数据回调")
            await consumer.stop()
        self.assertEqual(consumer.received, 2)

    async def test_reconnect_after_disconnect(self):
        consumer = SseConsumer("http://127.0.0.1:9", "synthetic", AsyncMock(),
                               max_backoff_s=0.2)
        responses = [_FakeResponse([b"data: {: heartbeat}\n\n"], then_raise=True)]
        with patch("urllib.request.urlopen", side_effect=OSError("conn reset")) as opener:
            await consumer.start()
            await asyncio.wait_for(_wait_true(lambda: opener.call_count >= 2),
                                   timeout=5)
            self.assertGreaterEqual(opener.call_count, 2, "断开后必须重连")
            await consumer.stop()


class _FakeResponse:
    def __init__(self, lines, then_raise=False):
        self.lines = lines
        self.then_raise = then_raise
        self.headers = {"Content-Type": "text/event-stream"}

    def __iter__(self):
        yield from self.lines
        if self.then_raise:
            raise OSError("connection reset")

    def close(self):
        pass


async def _wait_true(pred, interval=0.05):
    while not pred():
        await asyncio.sleep(interval)


class R2PermitLifecycle(unittest.IsolatedAsyncioTestCase):
    """R2：连续发送超过并发上限多轮，in_flight 归零。"""

    async def test_many_rounds_in_flight_zero(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.cfg.update(max_per_minute=1000, max_per_ten_minutes=10000,
                       min_interval_ms=1, private_min_interval_ms=1,
                       round_min_gap_ms=1, max_wait_ms=1000)
        out = {"raw_message": [{"type": "text", "data": "synthetic"}]}
        for _round in range(3):
            for _i in range(5):
                result = await obj.handle_wxsupervisor_gateway(
                    out, {"group_id": TALKER})
                self.assertTrue(result.get("success"),
                                f"不应因累计发送被拒: {result}")
            self.assertEqual(obj._send_scheduler._in_flight, 0,
                             "每轮结束 in_flight 必须归零")
        self.assertEqual(len(obj.hook.calls), 15)

    async def test_exception_path_releases(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.cfg.update(min_interval_ms=1, round_min_gap_ms=1, max_wait_ms=1000)
        obj._execute_outbound = AsyncMock(side_effect=RuntimeError("boom"))
        out = {"raw_message": [{"type": "text", "data": "x"}]}
        result = await obj.handle_wxsupervisor_gateway(out, {"group_id": TALKER})
        self.assertFalse(result.get("success"))
        self.assertEqual(obj._send_scheduler._in_flight, 0,
                         "异常路径必须归还并发名额")

    async def test_cancellation_releases(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.cfg.update(min_interval_ms=1, round_min_gap_ms=1, max_wait_ms=1000)
        obj.hook = _BlockingHook()
        out = {"raw_message": [{"type": "text", "data": "x"}]}
        task = asyncio.create_task(obj.handle_wxsupervisor_gateway(
            out, {"group_id": TALKER}))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.05)
        self.assertEqual(obj._send_scheduler._in_flight, 0,
                         "取消路径必须归还并发名额")


class _BlockingHook:
    def send_text(self, *a):
        import time
        time.sleep(5)
        return {"ret": 0}

    def health(self):
        return {"integration": "WeChatAgent-1", "backend": "wechat-hook",
                "startupState": "ready"}


class R7Idempotency(unittest.IsolatedAsyncioTestCase):
    """R7：幂等命中返回原结果；不重复发送；不被覆盖。"""

    async def test_sequential_duplicate_returns_first_result(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.cfg.update(max_per_minute=100, max_per_ten_minutes=1000,
                       min_interval_ms=1, round_min_gap_ms=1, max_wait_ms=1000)
        out = {"raw_message": [{"type": "text", "data": "synthetic"}]}
        results = []
        for _i in range(3):
            r = await obj.handle_wxsupervisor_gateway(
                out, {"group_id": TALKER}, {"request_id": "same-req"})
            results.append(r.get("success"))
        self.assertEqual(results, [True, True, True],
                         "重复请求返回成功（首个结果），不是限流失败")
        self.assertEqual(len(obj.hook.calls), 1, "实际只发送一次")

    async def test_submitted_not_overwritten_by_rejected(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        from local_wxsupervisor.send_queue import SendRequest, SendStatus
        req = SendRequest(target=TALKER, chat_type="group", request_id="keep-1")
        permit, _ = await obj._send_scheduler.acquire(req)
        obj._send_scheduler.finish(req, SendStatus.SUBMITTED, permit=permit)
        # 模拟重复查询后误报 REJECTED
        obj._send_scheduler.finish(req, SendStatus.REJECTED, reason="x")
        entry = obj._send_scheduler._idempotency.get(
            obj._send_scheduler.idem_key(req))
        self.assertEqual(entry.status, SendStatus.SUBMITTED,
                         "SUBMITTED 不得被 REJECTED 覆盖")

    async def test_concurrent_duplicate_waits_for_first(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.cfg.update(max_per_minute=100, max_per_ten_minutes=1000,
                       min_interval_ms=1, round_min_gap_ms=1, max_wait_ms=5000)
        obj.hook = _SlowHook(0.3)
        out = {"raw_message": [{"type": "text", "data": "synthetic"}]}
        both = await asyncio.gather(
            obj.handle_wxsupervisor_gateway(out, {"group_id": TALKER},
                                            {"request_id": "conc-1"}),
            obj.handle_wxsupervisor_gateway(out, {"group_id": TALKER},
                                            {"request_id": "conc-1"}))
        self.assertTrue(all(r.get("success") for r in both),
                        "并发重复两个请求都成功（一个实发一个等首个）")
        self.assertEqual(len(obj.hook.calls), 1, "只实际发送一次")


class _SlowHook:
    def __init__(self, delay):
        self.delay = delay
        self.calls = []

    def send_text(self, *a):
        import time
        self.calls.append(time.time())
        time.sleep(self.delay)
        return {"ret": 0}

    def health(self):
        return {"integration": "WeChatAgent-1", "backend": "wechat-hook",
                "startupState": "ready"}


class R8R9PolicyBypass(unittest.IsolatedAsyncioTestCase):
    """R8：非空白名单解析失败不放行。R9：OneBot 统一入口。"""

    async def test_unresolvable_whitelist_rejects(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()),
                                    wechat__whitelist_groups="UnresolvableGroup")
        msg = normalize_payload(reader_payload(), account=OWN_WXID)
        self.assertFalse(obj.inbound.policy.allows_inbound(msg),
                         "配置了名单但解析失败 → 拒绝（不是全部放行）")
        self.assertFalse(obj.inbound.policy.allows_outbound(TALKER))

    async def test_empty_whitelist_still_allows(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        msg = normalize_payload(reader_payload(), account=OWN_WXID)
        self.assertTrue(obj.inbound.policy.allows_inbound(msg),
                        "真正留空 → 不限制（语义保留）")

    async def test_onebot_send_uses_unified_entry(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        data = obj.get_plugin_config_data()
        data["wechat"]["disabled_chats"] = TALKER
        obj.set_plugin_config(data)
        obj.cfg = obj._load_config()
        server = OneBotServer("127.0.0.1", 9, "s", obj.hook, obj.reader,
                              lambda: "a", send_outbound=obj.send_outbound)
        with self.assertRaises(ValueError):
            await server._send("send_group_msg",
                               {"group_id": TALKER, "message": "synthetic"})
        self.assertEqual(len(obj.hook.calls), 0, "停用会话不得触达 hook")
        self.assertEqual(len(obj._send_scheduler.history_entries), 0)

    async def test_onebot_send_success_path(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.cfg.update(min_interval_ms=1, round_min_gap_ms=1, max_wait_ms=1000)
        server = OneBotServer("127.0.0.1", 9, "s", obj.hook, obj.reader,
                              lambda: "a", send_outbound=obj.send_outbound)
        result = await server._send("send_group_msg",
                                    {"group_id": TALKER, "message": "hi"})
        self.assertEqual(len(obj.hook.calls), 1, "正常路径实发一次")
        self.assertTrue(result.get("message_id"))


class R10ConfigTransaction(unittest.IsolatedAsyncioTestCase):
    """R10：await 重建、真回滚、reader 全引用、版本拒绝。"""

    async def test_rebuild_failure_rolls_back(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        original_mode = obj._settings().wechat.wake_mode
        old_flat = dict(obj.cfg)

        async def fail_rebuild(old, new):
            raise RuntimeError("synthetic rebuild failure")
        obj._apply_config_diff = fail_rebuild
        result = await obj.config_service.apply(
            {"wechat": {"wake_mode": "mention"}}, persist=False)
        self.assertFalse(result["ok"])
        self.assertEqual(obj._settings().wechat.wake_mode, original_mode,
                         "失败后恢复旧配置实例")
        self.assertEqual(obj.cfg.get("wake_mode"), old_flat.get("wake_mode"))

    async def test_version_rejected_not_overwritten(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        result = await obj.config_service.apply(
            {"plugin": {"config_version": "999.0.0"}}, persist=False)
        self.assertFalse(result["ok"], "不兼容版本必须拒绝")
        self.assertNotEqual(
            obj._settings().plugin.config_version, "999.0.0")

    async def test_reader_update_rebuilds_server_reference(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        old_reader = obj.reader
        obj.server = _FakeServerWithReader(old_reader)
        old_flat = dict(obj.cfg)
        obj.cfg["reader_base_url"] = "http://127.0.0.1:15031"
        async def fake_start_sse():
            pass
        async def fake_start_server():
            obj.server = _FakeServerWithReader(obj.reader)
        obj._start_sse = fake_start_sse
        obj._start_server = fake_start_server
        rebuilt = await obj._apply_config_diff(old_flat, obj.cfg)
        self.assertIn("server", rebuilt)
        self.assertIs(obj.server.reader, obj.reader,
                      "OneBot 持有的 reader 引用必须同步更新")

    async def test_apply_awaits_rebuild_no_background_task(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        captured = []
        orig_create = asyncio.get_running_loop().create_task

        def spy(coro, *a, **k):
            task = orig_create(coro, *a, **k)
            captured.append(task)
            return task

        with patch.object(asyncio.get_running_loop(), "create_task",
                          side_effect=spy):
            result = await obj.config_service.apply(
                {"wechat": {"wake_mode": "mention"}}, persist=False)
        self.assertTrue(result["ok"])
        background = [t for t in captured
                      if not t.done() and "apply_config_diff" in
                      repr(t.get_coro())]
        self.assertEqual(background, [],
                         "配置重建不得用 create_task 丢后台")


class _FakeServerWithReader:
    def __init__(self, reader):
        self.reader = reader

    async def stop(self):
        pass


class R11Counting(unittest.IsolatedAsyncioTestCase):
    """R11：按最终发送计划计额度。"""

    async def test_one_image_one_native(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.cfg.update(max_per_minute=1, max_per_ten_minutes=100)
        img = {"raw_message": [{"type": "image", "data": "",
                                "binary_data_base64": "R0lGODlhAQABAAAAACw="}]}
        result = await obj.handle_wxsupervisor_gateway(img, {"group_id": TALKER})
        self.assertTrue(result.get("success"), f"单图应可发送: {result}")
        self.assertEqual(len(obj.hook.calls), 1, "一张图=一次原生发送")

    async def test_tts_both_counts_two(self):
        from local_wxsupervisor.send_queue import plan_native_count
        self.assertEqual(plan_native_count(
            has_text=True, want_voice=True, want_text=True,
            voice_fallback_enabled=True, media_count=0), 2)
        self.assertEqual(plan_native_count(
            has_text=True, want_voice=True, want_text=False,
            voice_fallback_enabled=True, media_count=0), 2,
            "voice_only + 兜底 = 2")
        self.assertEqual(plan_native_count(
            has_text=False, want_voice=False, want_text=True,
            voice_fallback_enabled=True, media_count=1), 1,
            "纯图片=1，不凭空多算文本")


class R3R4R5R6Recovery(unittest.IsolatedAsyncioTestCase):
    """R3 水位 / R4 游标补收 / R5 未确认持久化 / R6 接收去重。"""

    async def test_r3_watermark_on_accepted(self):
        obj, gw = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.recovery = recovery.RecoveryManager(obj)
        await obj.inbound.submit(reader_payload())
        mark = obj.recovery.watermarks.get(OWN_WXID, TALKER)
        self.assertIsNotNone(mark, "正常入站成功后必须有水位")
        self.assertEqual(mark["ts"], float(reader_payload()["timestamp"]))

    async def test_r4_gap_full_and_chronological(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.recovery = recovery.RecoveryManager(obj)
        obj.recovery.watermarks.update(OWN_WXID, TALKER, "prior", 100)
        all_rows = [dict(senderUsername="wxid_audit_alice",
                         senderDisplay="Alice", localType=1,
                         parsedContent="m", createTime=i, localId=i,
                         serverId=i) for i in range(101, 701)]
        pages = [all_rows[:500], all_rows[500:]]
        delivered = []

        def fake_page(base, token, talker, limit, after_time=None,
                      after_id="", timeout=20.0):
            idx = 0 if after_id == "prior" else 1
            rows = pages[min(idx, 1)]
            cursor = None
            if idx == 0 and rows:
                last = rows[-1]
                cursor = {"after_time": last["createTime"],
                          "after_id": "rawid-%d" % last["localId"]}
            return {"messages": rows, "cursor": cursor}

        async def confirmed(key, payload, replay=False, account=''):
            delivered.append(payload.get("timestamp"))
            return True
        obj.recovery._deliver_payload = confirmed
        with patch.object(recovery, "fetch_messages_page",
                          side_effect=fake_page) as fp:
            fetched = await obj.recovery.backfill_gap()
        self.assertEqual(fetched, 600, "600 条缺口必须全部补收")
        self.assertGreaterEqual(fp.call_count, 2, "必须跨页")
        self.assertEqual(delivered, sorted(delivered), "必须正序投递")

    async def test_r5_watermark_held_and_pending_restored(self):
        d = await self.enterAsyncContext(_tmp())
        obj, _ = await fresh_plugin(d)
        obj.recovery = recovery.RecoveryManager(obj)
        obj.recovery.watermarks.update(OWN_WXID, TALKER, "prior", 100)

        async def reject(key, payload, replay=False, account=''):
            return False
        obj.recovery._deliver_payload = reject
        row = dict(senderUsername="wxid_audit_alice", parsedContent="m",
                   localType=1, createTime=200, serverId=200, localId=200)
        with patch.object(recovery, "fetch_messages_page",
                          return_value={"messages": [row], "cursor": None}):
            await obj.recovery.backfill_gap()
        self.assertEqual(obj.recovery.watermarks.get(OWN_WXID, TALKER)["ts"],
                         100.0, "未确认不得越过缺口推进水位")
        self.assertEqual(obj.replay.pending(), 1, "未确认事件进重放")

        # 进程重建：读回
        obj2, _ = await fresh_plugin(d)
        obj2.recovery = recovery.RecoveryManager(obj2)
        self.assertEqual(obj2.replay.pending(), 1,
                         "重建后未确认事件必须恢复")

    async def test_r6_duplicate_not_staged(self):
        obj, gw = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.recovery = recovery.RecoveryManager(obj)
        duplicate = reader_payload()
        await obj.inbound.submit(duplicate)          # 首次：接受 + 去重标记
        gw.accept = False
        await obj.inbound.submit(duplicate)          # 第二次：去重丢弃
        self.assertEqual(obj.replay.pending(), 0,
                         "重复 rawid 不进失败重放")
        self.assertEqual(len(gw.calls), 1, "重复不重复投递")

    async def test_r6_failed_delivery_then_replay_ok(self):
        d = await self.enterAsyncContext(_tmp())
        obj, gw = await fresh_plugin(d)
        obj.recovery = recovery.RecoveryManager(obj)
        gw.accept = False
        payload = reader_payload()
        await obj.inbound.submit(payload)
        self.assertEqual(obj.replay.pending(), 1, "真失败进重放")
        gw.accept = True
        key = list(obj.replay._order)[0]
        await obj.recovery._redeliver(key, obj.replay.take(key))
        self.assertEqual(obj.replay.pending(), 0, "重放成功后 ack")
        self.assertEqual(len(gw.calls), 3, "重放实际再投递")


class R12MediaLease(unittest.IsolatedAsyncioTestCase):
    """R12：租约保护 + 计划内删除。"""

    async def test_lease_protects_recent_file(self):
        d = await self.enterAsyncContext(_tmp())
        base = Path(d) / "state" / "media" / "outgoing"
        base.mkdir(parents=True, exist_ok=True)
        pending = base / "pending.png"
        pending.write_bytes(b"data" * 10)
        janitor = perf.MediaJanitor(Path(d), ttl_hours=48, max_total_mb=0.0001)
        janitor.lease(pending)
        self.assertEqual(janitor.candidates(), [], "有租约不进候选")
        deleted = janitor.enforce()
        self.assertTrue(pending.exists(), "租约文件不被容量清理删除")
        self.assertEqual(deleted, [])

    async def test_expired_file_deleted_within_plan(self):
        import os
        import time as _time
        d = await self.enterAsyncContext(_tmp())
        base = Path(d) / "state" / "voice" / "outgoing"
        base.mkdir(parents=True, exist_ok=True)
        old = base / "old.silk"
        old.write_bytes(b"x" * 10)
        t = _time.time() - 100 * 3600
        os.utime(old, (t, t))
        janitor = perf.MediaJanitor(Path(d), ttl_hours=48, max_total_mb=1)
        cands = [str(p) for p in janitor.candidates()]
        self.assertIn(str(old), cands)
        deleted = janitor.enforce()
        self.assertTrue(any(str(old) in x for x in deleted))
        self.assertFalse(old.exists())


class R13MediaContract(unittest.IsolatedAsyncioTestCase):
    """R13：语音段 + 补收媒体契约。"""

    async def test_voice_generates_voice_segment(self):
        voice_payload = {"sessionId": TALKER, "type": 34, "content": "[语音]",
                         "voice": {"localId": 1, "serverId": 2, "createTime": 3}}
        _, segments = wx_message.build_raw_segments(voice_payload)
        self.assertTrue(any(s["type"] == "voice" for s in segments),
                        f"必须有 voice 段: {[s['type'] for s in segments]}")

    async def test_gap_media_payload_complete(self):
        row = {"localType": 3, "senderUsername": "wxid_audit_alice",
               "senderDisplay": "Alice", "parsedContent": "[图片]",
               "fullText": "[图片]", "createTime": 123, "serverId": 1,
               "localId": 1, "groupName": "AuditGroup"}
        payload = history_backfill.payload_from_reader_row(TALKER, row)
        self.assertIsNotNone(payload)
        self.assertIn("image", payload, "图片描述必须保留")
        self.assertEqual(payload["groupName"], "AuditGroup",
                         "groupName 是群名，不是发言人昵称")

    async def test_gap_filters_self_and_unsupported(self):
        self_row = {"localType": 1, "senderUsername": OWN_WXID,
                    "parsedContent": "self msg", "isSend": True,
                    "createTime": 1, "serverId": 1, "localId": 1}
        self.assertIsNone(history_backfill.payload_from_reader_row(
            TALKER, self_row), "自身消息（isSend）必须过滤")
        unknown = {"localType": 9999, "senderUsername": "wxid_audit_alice",
                   "parsedContent": "?", "createTime": 2, "serverId": 2,
                   "localId": 2}
        self.assertIsNone(history_backfill.payload_from_reader_row(
            TALKER, unknown), "不支持类型必须过滤")


if __name__ == "__main__":
    unittest.main()
