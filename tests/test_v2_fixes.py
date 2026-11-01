"""V2 复核返修回归（13 个复现点 → 修复后行为断言 + 故障注入）。"""
from __future__ import annotations

import asyncio
import base64
import json
import queue
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from helpers import OWN_WXID, TALKER, fresh_plugin, reader_payload
from local_wxsupervisor import capabilities, history_backfill, perf, recovery, sticker, wx_image, wx_message
from local_wxsupervisor.sse_consumer import SseConsumer


def _tmp():
    import tempfile
    return _TmpCtx(tempfile.TemporaryDirectory(prefix="v2fix-"))


class _TmpCtx:
    def __init__(self, handle):
        self.handle = handle
    def __call__(self):
        return self.handle.name
    async def __aenter__(self):
        return self.handle.name
    async def __aexit__(self, *exc):
        self.handle.cleanup()


def _load_cursor_reader():
    """提取 reader 补丁实际方法执行（不用预制分页响应）。"""
    import ast
    import hashlib
    patch_path = Path(__file__).resolve().parent.parent / "patches" / "reader-api.cursor-patch.py"
    tree = ast.parse(patch_path.read_text(encoding="utf-8-sig"))
    methods = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
               and n.name in ("messages_after", "row_rawid")]
    cls = ast.ClassDef(name="CursorReader", bases=[], keywords=[],
                       body=methods, decorator_list=[])
    ns = {"hashlib": hashlib, "json": json}
    exec(compile(ast.fix_missing_locations(
        ast.Module(body=[cls], type_ignores=[])), str(patch_path), "exec"), ns)
    return ns["CursorReader"]()


class V21ReaderCursor(unittest.TestCase):
    """V2-1：同秒分页、锚点消失。"""

    def test_same_second_600_rows_cross_page(self):
        reader = _load_cursor_reader()
        same_second = [dict(createTime=100, localId=i, serverId=i,
                            senderUsername="wxid_audit_alice",
                            parsedContent="m", localType=1)
                       for i in range(1, 601)]
        reader.selected_talkers = lambda: [TALKER]
        reader.messages = lambda *a, **k: list(same_second)
        first = reader.messages_after(TALKER, 500, 99, "")
        self.assertEqual(len(first["messages"]), 500)
        cur = first["cursor"]
        second = reader.messages_after(TALKER, 500,
                                       cur["after_time"], cur["after_id"])
        self.assertEqual(len(second["messages"]), 100,
                         "同秒剩余消息不得被游标过滤")
        total = len(first["messages"]) + len(second["messages"])
        self.assertEqual(total, 600)

    def test_anchor_disappears_falls_back(self):
        reader = _load_cursor_reader()
        rows = [dict(createTime=100, localId=i, serverId=i,
                     senderUsername="a", parsedContent="m", localType=1)
                for i in range(1, 11)]
        reader.selected_talkers = lambda: [TALKER]
        reader.messages = lambda *a, **k: list(rows)
        # G4：锚点消失 + 无 after_local_id → 受控重扫（>= after_time），
        # 同秒邻居返回；调用方去重兜底防重复投递。
        page = reader.messages_after(TALKER, 5, 100, "nonexistent")
        self.assertEqual(len(page["messages"]), 5,
                         "受控重扫返回同秒及之后行（去重由调用方兜底）")


class V22SseScheduling(unittest.IsolatedAsyncioTestCase):
    """V2-2：背压分支完成信号 + 会话级有序投递。"""

    async def test_backpressure_17_events_completion(self):
        consumer = SseConsumer("http://127.0.0.1:9", "synthetic",
                               lambda p: asyncio.sleep(0.001))
        channel = queue.Queue(maxsize=32)
        for i in range(17):
            channel.put(("data", json.dumps({"i": i})))
        channel.put(("eof", None))
        done_event = asyncio.Event()
        await asyncio.wait_for(consumer._pump(channel, done_event), 5)
        self.assertTrue(done_event.is_set(),
                        "进入并发上限分支后完成事件必须置位")
        self.assertEqual(consumer.received, 17)
        self.assertEqual(consumer._inflight_events, set(), "无遗留任务")
        consumer._shutdown_executor()

    async def test_32_events_no_leftover(self):
        consumer = SseConsumer("http://127.0.0.1:9", "synthetic",
                               lambda p: asyncio.sleep(0.001))
        channel = queue.Queue(maxsize=64)
        for i in range(32):
            channel.put(("data", json.dumps({"i": i})))
        channel.put(("eof", None))
        await asyncio.wait_for(consumer._pump(channel, asyncio.Event()), 5)
        self.assertEqual(consumer.received, 32)
        self.assertEqual(consumer._inflight_events, set())
        consumer._shutdown_executor()

    async def test_same_session_order_with_slow_media(self):
        obj, gateway = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        image = reader_payload(text="[图片]", number=1)
        image.update(type=3, image={"localId": 1})
        text = reader_payload(text="later text", number=2)
        order = []
        release = asyncio.Event()

        async def slow_decode(*a, **k):
            await release.wait()
            return None

        consumer = SseConsumer("http://127.0.0.1:9", "synthetic",
                               lambda p: obj.inbound.submit(p))
        ch = queue.Queue(maxsize=8)
        ch.put(("data", json.dumps(image)))
        ch.put(("data", json.dumps(text)))
        ch.put(("eof", None))
        with patch.object(wx_image, "enrich_image_payload", slow_decode):
            pump = asyncio.create_task(consumer._pump(ch, asyncio.Event()))
            await asyncio.sleep(0.15)          # 图片进入解码（阻塞）
            release.set()
            await asyncio.wait_for(pump, 5)
        # 同会话：图片解码慢，但投递顺序必须图片在前
        msgs = [c["message"]["message_id"] for c in gateway.calls]
        self.assertEqual(msgs, [image["rawid"], text["rawid"]],
                         f"同会话乱序: {msgs}")
        self.assertEqual(consumer._inflight_events, set())
        consumer._shutdown_executor()

    async def test_stop_cleans_inflight(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def on_event(payload):
            started.set()
            await release.wait()

        consumer = SseConsumer("http://127.0.0.1:9", "synthetic", on_event)
        ch = queue.Queue(maxsize=8)
        ch.put(("data", json.dumps({"sessionId": "s"})))
        ch.put(("eof", None))
        consumer._stop.clear()
        done = asyncio.Event()
        pump = asyncio.create_task(consumer._pump(ch, done))
        await asyncio.wait_for(started.wait(), timeout=2)
        release.set()
        await asyncio.wait_for(pump, 5)
        self.assertEqual(consumer._inflight_events, set())
        consumer._shutdown_executor()


class V23SendPlan(unittest.IsolatedAsyncioTestCase):
    """V2-3：计划一次构建；on_request 执行时才消费。"""

    async def test_on_request_synth_once_via_send_outbound(self):
        obj, gateway = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.cfg.update(tts_enabled=True, tts_mode="on_request",
                       tts_request_triggers=["voice please"],
                       min_interval_ms=1, max_per_minute=100,
                       max_per_ten_minutes=1000)
        obj.note_inbound_text(TALKER, "voice please")
        obj._synthesize_voice = AsyncMock(return_value=None)
        result = await obj.send_outbound(
            {"raw_message": [{"type": "text", "data": "synthetic"}]},
            route={"group_id": TALKER})
        self.assertTrue(result.get("success"))
        self.assertEqual(obj._synthesize_voice.await_count, 1,
                         "合成必须恰好尝试一次（计划消费不在计数阶段）")
        text_calls = [c for c in obj.hook.calls if c[0] == "send_text"]
        self.assertLessEqual(len(text_calls), 1,
                             "want_voice 且合成失败时最多一次兜底文字")

    async def test_throttle_rejection_does_not_consume(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.cfg.update(tts_enabled=True, tts_mode="on_request",
                       tts_request_triggers=["voice please"],
                       max_per_minute=0, max_wait_ms=10)   # 额度 0 → 必拒
        obj.note_inbound_text(TALKER, "voice please")
        result = await obj.send_outbound(
            {"raw_message": [{"type": "text", "data": "x"}]},
            route={"group_id": TALKER})
        self.assertFalse(result.get("success"))
        # 请求未被消费：下一次（放开限额）仍应走语音
        obj.cfg.update(max_per_minute=100)
        obj._synthesize_voice = AsyncMock(return_value=None)
        await obj.send_outbound(
            {"raw_message": [{"type": "text", "data": "x"}]},
            route={"group_id": TALKER})
        self.assertEqual(obj._synthesize_voice.await_count, 1,
                         "被限流拒绝不得消费语音请求")


class V24V29ConfirmTx(unittest.IsolatedAsyncioTestCase):
    """V2-4：确认事务（pending/in-flight/confirmed）；V2-9：账号绑定。"""

    async def test_cancelled_delivery_not_marked_received(self):
        obj, gateway = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.recovery = recovery.RecoveryManager(obj)
        payload = reader_payload()

        async def cancelled_host(**kwargs):
            raise asyncio.CancelledError()
        gateway.route_message = cancelled_host
        with self.assertRaises(asyncio.CancelledError):
            await obj.inbound.submit(payload)
        # 重建恢复管理器：事件必须可恢复，去重表不得有假阳性
        restored = recovery.RecoveryManager(obj)
        self.assertFalse(restored.dedup.seen_before(OWN_WXID, TALKER,
                                                   payload["rawid"]),
                         "未确认事件不得写入去重表（V2-4）")
        self.assertEqual(restored.plugin.replay.pending(), 1,
                         "取消后事件必须保持 pending 可恢复")

    async def test_successful_replay_marks_dedup(self):
        obj, gateway = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        manager = recovery.RecoveryManager(obj)
        payload = reader_payload()
        manager.stage_unconfirmed(payload["rawid"], payload,
                                  origin_account=OWN_WXID)
        await manager._redeliver(payload["rawid"], payload)
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(obj.replay.pending(), 0)
        self.assertTrue(manager.dedup.seen_before(OWN_WXID, TALKER,
                                                  payload["rawid"]),
                        "重放成功必须写入 ReceiveDedup（V2-4）")

    async def test_pending_event_replay_keeps_origin_account(self):
        obj, gateway = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        manager = recovery.RecoveryManager(obj)
        payload = reader_payload()
        manager.stage_unconfirmed(payload["rawid"], payload,
                                  origin_account=OWN_WXID)
        # reader 切换账号（V2-9 场景）：复核要求「隔离保留，不重新归属」
        obj.reader.own_wxid = "wxid_synthetic_second_account"
        await manager._redeliver(payload["rawid"], payload)
        self.assertEqual(len(gateway.calls), 0,
                         "账号已切换：事件隔离保留，不得投递到新账号")
        self.assertEqual(obj.replay.pending(), 1,
                         "事件保留（未丢失），可回原账号后重放")
        self.assertEqual(manager._origin_account_of(payload["rawid"]),
                         OWN_WXID, "来源账号绑定不变")

    async def test_rebuild_restores_origin_account(self):
        d = await self.enterAsyncContext(_tmp())
        obj, _ = await fresh_plugin(d)
        obj.recovery = recovery.RecoveryManager(obj)
        obj.recovery.stage_unconfirmed("rawid-x", reader_payload(),
                                       origin_account="wxid_origin_a")
        obj2, _ = await fresh_plugin(d)
        obj2.recovery = recovery.RecoveryManager(obj2)
        self.assertEqual(obj2.replay.pending(), 1)
        self.assertEqual(obj2.recovery._origin_account_of("rawid-x"),
                         "wxid_origin_a", "重建后来源账号必须恢复")


class V25WatermarkBoundary(unittest.IsolatedAsyncioTestCase):
    """V2-5：最早未确认边界 + 分页进展。"""

    async def test_new_message_not_past_pending_gap(self):
        obj, gateway = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.recovery = recovery.RecoveryManager(obj)
        old_payload, new_payload = reader_payload(number=1), reader_payload(number=2)
        gateway.accept = False
        await obj.inbound.submit(old_payload)     # 旧：暂存
        gateway.accept = True
        await obj.inbound.submit(new_payload)     # 新：成功
        mark = obj.recovery.watermarks.get(OWN_WXID, TALKER)
        if mark is not None:
            self.assertLessEqual(mark["ts"], float(old_payload["timestamp"]),
                                 "水位不得越过未确认缺口（V2-5）")
        # 无论 None 或旧值，都不得推进到新消息时间
        # 旧事件重放确认后水位推进
        gateway.accept = True
        key = list(obj.replay._order)[0]
        await obj.recovery._redeliver(key, obj.replay.take(key))
        mark2 = obj.recovery.watermarks.get(OWN_WXID, TALKER)
        self.assertGreaterEqual(mark2["ts"], float(new_payload["timestamp"]),
                                "缺口确认后水位推进")

    async def test_gap_backfill_advances_watermark(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        manager = recovery.RecoveryManager(obj)
        manager.watermarks.update(OWN_WXID, TALKER, "prior", 100)
        row = dict(createTime=200, localId=1, serverId=1, localType=1,
                   senderUsername="wxid_audit_alice", parsedContent="m")
        with patch.object(recovery, "fetch_messages_page",
                          return_value={"messages": [row], "cursor": None}):
            fetched = await manager.backfill_gap()
        self.assertEqual(fetched, 1)
        self.assertEqual(manager.watermarks.get(OWN_WXID, TALKER)["ts"], 200.0,
                         "补收成功必须推进并持久化水位（V2-5）")

    async def test_all_duplicate_page_continues_to_next(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        manager = recovery.RecoveryManager(obj)
        manager.watermarks.update(OWN_WXID, TALKER, "prior", 100)
        all_rows = [dict(createTime=i, localId=i, serverId=i, localType=1,
                         senderUsername="wxid_audit_alice", parsedContent="m")
                    for i in range(101, 701)]
        first_rows = all_rows[:500]
        for row in first_rows:
            payload = history_backfill.payload_from_reader_row(TALKER, row)
            manager.dedup.mark(OWN_WXID, TALKER, payload["rawid"])
        last_payload = history_backfill.payload_from_reader_row(TALKER, first_rows[-1])
        pages = [{"messages": first_rows,
                  "cursor": {"after_time": 600, "after_id": last_payload["rawid"]}},
                 {"messages": all_rows[500:], "cursor": None}]
        with patch.object(recovery, "fetch_messages_page",
                          side_effect=pages) as query:
            fetched = await manager.backfill_gap()
        self.assertEqual(query.call_count, 2, "全重复页不得阻断下一页")
        self.assertEqual(fetched, 100, "下一页新消息必须补收")


class V26ConfigRollback(unittest.IsolatedAsyncioTestCase):
    """V2-6：回滚真正恢复服务 + 清理新建组件。"""

    async def test_rollback_restarts_stopped_components(self):
        class FakeService:
            def __init__(self):
                self.active = True
            async def stop(self):
                self.active = False
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        old_sse, old_server = FakeService(), FakeService()
        obj.sse, obj.server = old_sse, old_server
        replacement = FakeService()
        restored_sse = FakeService()
        restored_server = FakeService()
        server_calls = []
        sse_calls = []

        async def start_sse():
            sse_calls.append(1)
            if len(sse_calls) == 1:
                obj.sse = replacement          # 事务期：新组件
            else:
                obj.sse = restored_sse         # 回滚期：按旧配置重建

        async def fail_then_recover_server():
            server_calls.append(1)
            if len(server_calls) == 1:
                raise OSError("synthetic bind failure")
            obj.server = restored_server
        with patch.object(obj, "_start_sse", new=start_sse), \
                patch.object(obj, "_start_server", new=fail_then_recover_server):
            result = await obj.config_service.apply(
                {"reader": {"base_url": "http://127.0.0.1:15031"}}, persist=False)
        self.assertFalse(result["ok"])
        # 已 stop 的旧组件无法复活 —— 回滚按旧配置重建可用组件
        self.assertIsNot(obj.sse, replacement, "半途新建组件必须被替换")
        self.assertFalse(replacement.active, "半途新建组件必须被清理")
        self.assertIs(obj.sse, restored_sse, "插件引用指向回滚重建的 SSE")
        self.assertTrue(getattr(obj.sse, "active", False),
                        "回滚后 SSE 必须是旧配置的可用组件")
        self.assertTrue(restored_server.active,
                        "回滚重建的 server 必须可用（可重新收发）")
        self.assertFalse(replacement.active, "半途新建组件必须被清理")


class V27UnknownResult(unittest.IsolatedAsyncioTestCase):
    """V2-7：响应丢失 → UNKNOWN，不自动重试。"""

    async def test_timeout_marks_unknown_no_retry(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        obj.cfg.update(tts_enabled=False, min_interval_ms=1, round_min_gap_ms=1,
                       max_per_minute=100, max_per_ten_minutes=1000)
        committed = []

        def ambiguous_send(*args):
            committed.append(True)
            if len(committed) == 1:
                raise TimeoutError("synthetic response lost after native submit")
            return {"ret": 0}
        obj.hook.result = ambiguous_send
        message = {"raw_message": [{"type": "text", "data": "synthetic"}]}
        first = await obj.send_outbound(message, {"group_id": TALKER},
                                        {"request_id": "same-timeout-request"})
        self.assertFalse(first.get("success"))
        self.assertEqual(first.get("metadata", {}).get("delivery"), "unknown",
                         "响应丢失必须标记 UNKNOWN")
        second = await obj.send_outbound(message, {"group_id": TALKER},
                                         {"request_id": "same-timeout-request"})
        self.assertEqual(len(committed), 1,
                         "结果未知不得自动重试（会重复提交）")
        self.assertEqual(second.get("metadata", {}).get("delivery"), "unknown",
                         "重复请求应返回 UNKNOWN 快照（不重试）")

    async def test_resolve_unknown_recovers_state(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        from local_wxsupervisor.send_queue import SendRequest, SendStatus
        req = SendRequest(target=TALKER, chat_type="group", request_id="u-1")
        obj._send_scheduler.mark_unknown(req, "response lost")
        # 回执确认送达
        self.assertTrue(obj._send_scheduler.resolve_unknown("u-1", True))
        hit, _cached, _f = obj._send_scheduler._lookup_idempotent(req)
        self.assertTrue(hit)
        # 确认未提交 → 允许重试
        req2 = SendRequest(target=TALKER, chat_type="group", request_id="u-2")
        obj._send_scheduler.mark_unknown(req2, "response lost")
        self.assertTrue(obj._send_scheduler.resolve_unknown("u-2", False))
        hit2, _c2, _f2 = obj._send_scheduler._lookup_idempotent(req2)
        self.assertFalse(hit2, "确认未提交后应允许重新发送")


class V28MediaLease(unittest.IsolatedAsyncioTestCase):
    """V2-8：真实发送登记租约；发送期间清理器不删。"""

    async def test_image_send_holds_lease_against_janitor(self):
        obj, _ = await fresh_plugin(await self.enterAsyncContext(_tmp()))
        gif = b"GIF89a" + b"synthetic-image" * 12
        path = sticker.stage_image(obj.cfg["project_root"], gif)
        expired = time.time() - 72 * 3600
        import os
        os.utime(path, (expired, expired))
        janitor = perf.MediaJanitor(obj.cfg["project_root"], ttl_hours=48)
        deleted_during_send = []

        def image_send(*args):
            deleted_during_send.extend(janitor.enforce())
            return {"ret": 0}
        obj.hook.send_image = image_send
        registry = perf.MediaLeaseRegistry.shared()
        ref_before = registry._refs.get(str(path), [0])[0]
        ctx = capabilities.SendContext(
            wxid=TALKER, display="AuditGroup", account="synthetic-account",
            hook=obj.hook, cfg=obj.cfg, loop=asyncio.get_running_loop())
        await capabilities._handle_image(ctx, {
            "type": "image",
            "binary_data_base64": base64.b64encode(gif).decode()})
        self.assertNotIn(str(path), [str(p) for p in deleted_during_send],
                         "发送期间清理器不得删除有租约的待发文件")
        self.assertTrue(path.exists())
        # 发送完成后引用计数回到发送前水平（配对 release）
        ref_after = registry._refs.get(str(path), [0])[0]
        self.assertEqual(ref_after, ref_before, "租约必须配对释放")

    async def test_concurrent_sends_share_lease_refcount(self):
        gif = b"GIF89a" + b"x" * 12
        d = await self.enterAsyncContext(_tmp())
        path = sticker.stage_image(d, gif)
        registry = perf.MediaLeaseRegistry.shared()
        # staging 已登记 1 次；第二个并发发送再借用 1 次 → 引用计数 2
        registry.acquire(path)
        self.assertTrue(registry.is_leased(path))
        registry.release(path)
        self.assertTrue(registry.is_leased(path),
                        "并发使用者之一释放后仍保护（staging 租约在）")
        registry.release(path)
        self.assertFalse(registry.is_leased(path),
                         "全部释放后租约解除")

    async def test_janitor_reads_live_registry(self):
        d = await self.enterAsyncContext(_tmp())
        base = Path(d) / "state" / "media" / "outgoing"
        base.mkdir(parents=True, exist_ok=True)
        f = base / "live.png"
        f.write_bytes(b"x" * 10)
        janitor = perf.MediaJanitor(Path(d), ttl_hours=0.0001)
        perf.MediaLeaseRegistry.shared().acquire(f)
        self.assertEqual(janitor.candidates(), [],
                         "清理器必须读取当前有效租约")


class V2VoiceChain(unittest.IsolatedAsyncioTestCase):
    """语音接收链路现状断言：voice 段投递、字节解码未实现（明确边界）。"""

    async def test_voice_segment_present_with_descriptor(self):
        voice_payload = {"sessionId": TALKER, "type": 34, "content": "[语音]",
                         "voice": {"localId": 1, "serverId": 2, "createTime": 3}}
        _, segments = wx_message.build_raw_segments(voice_payload)
        voice_segs = [s for s in segments if s["type"] == "voice"]
        self.assertTrue(voice_segs)
        self.assertIn("localId=1", voice_segs[0].get("data") or "")


if __name__ == "__main__":
    unittest.main()
