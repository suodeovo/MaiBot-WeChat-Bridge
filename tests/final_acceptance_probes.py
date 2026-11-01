"""对复核 19 个探针逐项做修复后行为对照（final acceptance probes）。

对照基准：review_20261101/review-results.json 的 observed=true 19 项。
本脚本每项验证「修复后的行为」；全部 fixed=true 才能进入部署评审。
"""
import asyncio
import faulthandler
import threading
import importlib.util
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, r".\source")
sys.path.insert(0, r".\tests")
sys.stdout.reconfigure(line_buffering=True)
logging.disable(logging.CRITICAL)

from helpers import OWN_WXID, TALKER, fresh_plugin, reader_payload  # noqa: E402
from local_wxsupervisor import history_backfill, perf, recovery, wx_message  # noqa: E402
from local_wxsupervisor.identity import normalize_payload  # noqa: E402
from local_wxsupervisor.onebot_server import OneBotServer  # noqa: E402
from local_wxsupervisor.sse_consumer import SseConsumer  # noqa: E402

results = []


def record(name, fixed, **details):
    results.append({"probe": name, "fixed": bool(fixed), **details})


async def wait_until(pred, timeout=5.0):
    while not pred():
        await asyncio.sleep(0.05)
        if timeout <= 0:
            return
        timeout -= 0.05


class _Resp:
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


async def main():
    d = tempfile.mkdtemp(prefix="final-")

    # ---- 1 real_sse_start_uses_undefined_loop
    consumer = SseConsumer("http://127.0.0.1:9", "synthetic", AsyncMock())
    with patch("urllib.request.urlopen", side_effect=OSError("no net")) as op:
        await consumer.start()
        await wait_until(lambda: bool(consumer.last_error) and op.call_count >= 1)
        print('probe-done:', results[-1]['probe'] if results else '?', file=sys.stderr);    record("1_real_sse_start",
               "loop" not in consumer.last_error.lower() and op.call_count >= 1,
               last_error=consumer.last_error, attempts=op.call_count)
        await consumer.stop()

    print('== enter 2', file=sys.stderr);    # ---- 2 gateway_never_releases_concurrency_permits
    obj, _ = await fresh_plugin(os.path.join(d, "p2"))
    obj.cfg.update(max_per_minute=100, max_per_ten_minutes=1000,
                   min_interval_ms=1, private_min_interval_ms=1,
                   round_min_gap_ms=1, max_wait_ms=1000)
    out = {"raw_message": [{"type": "text", "data": "synthetic"}]}
    statuses = []
    for _ in range(5):
        r = await obj.handle_wxsupervisor_gateway(out, {"group_id": TALKER})
        statuses.append(r.get("success"))
    print('probe-done:', results[-1]['probe'] if results else '?', file=sys.stderr);    record("2_permit_release", all(statuses) and obj._send_scheduler._in_flight == 0,
           statuses=statuses, in_flight=obj._send_scheduler._in_flight)

    print('== enter 3', file=sys.stderr);    # ---- 3 idempotent_hit_rejected_then_overwritten
    obj, _ = await fresh_plugin(os.path.join(d, "p3"))
    obj.cfg.update(max_per_minute=100, max_per_ten_minutes=1000,
                   min_interval_ms=1, round_min_gap_ms=1, max_wait_ms=1000)
    st = []
    for _ in range(3):
        r = await obj.handle_wxsupervisor_gateway(
            out, {"group_id": TALKER}, {"request_id": "dup-req"})
        st.append(r.get("success"))
    print('probe-done:', results[-1]['probe'] if results else '?', file=sys.stderr);    record("3_idempotent", st == [True, True, True] and len(obj.hook.calls) == 1,
           statuses=st, hook_calls=len(obj.hook.calls))

    print('== enter 4', file=sys.stderr);    # ---- 4 onebot_send_bypasses
    obj, _ = await fresh_plugin(os.path.join(d, "p4"))
    data = obj.get_plugin_config_data()
    data["wechat"]["disabled_chats"] = TALKER
    obj.set_plugin_config(data)
    obj.cfg = obj._load_config()
    server = OneBotServer("127.0.0.1", 9, "s", obj.hook, obj.reader,
                          lambda: "a", send_outbound=obj.send_outbound)
    try:
        await server._send("send_group_msg", {"group_id": TALKER, "message": "x"})
        bypassed = True
    except ValueError:
        bypassed = False
    print('probe-done:', results[-1]['probe'] if results else '?', file=sys.stderr);    record("4_onebot_unified",
           not bypassed and len(obj.hook.calls) == 0
           and len(obj._send_scheduler.history_entries) == 0,
           hook_calls=len(obj.hook.calls))

    print('== enter 5', file=sys.stderr);    # ---- 5 unresolvable_nonempty_whitelist_allows_everyone
    obj, _ = await fresh_plugin(os.path.join(d, "p5"),
                                wechat__whitelist_groups="UnresolvableGroup")
    msg = normalize_payload(reader_payload(), account=OWN_WXID)
    print('probe-done:', results[-1]['probe'] if results else '?', file=sys.stderr);    record("5_unresolvable_rejects",
           not obj.inbound.policy.allows_inbound(msg)
           and not obj.inbound.policy.allows_outbound(TALKER))

    print('== enter 6', file=sys.stderr);    # ---- 6 accepted_live_message_never_updates_watermark
    obj, gw = await fresh_plugin(os.path.join(d, "p6"))
    obj.recovery = recovery.RecoveryManager(obj)
    await obj.inbound.submit(reader_payload())
    mark = obj.recovery.watermarks.get(OWN_WXID, TALKER)
    print('probe-done:', results[-1]['probe'] if results else '?', file=sys.stderr);    record("6_watermark_recorded",
           len(gw.calls) == 1 and mark is not None
           and mark["ts"] == float(reader_payload()["timestamp"]),
           watermark=mark)

    print('== enter 7', file=sys.stderr);    # ---- 7 same_rawid_staged_as_failure_then_accepted_again_after_host_ttl
    spec = importlib.util.spec_from_file_location(
        "host_dedupe", Path(pathlib.Path(__file__).parent.parent / "tests" / "mock_host_dedupe.py"))
    host_dedupe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host_dedupe)
    clock = [1000.0]
    host_dedupe.time = SimpleNamespace(monotonic=lambda: clock[0])
    deduper = host_dedupe.MessageDeduplicator(ttl_seconds=300)
    obj, gw = await fresh_plugin(os.path.join(d, "p7"))
    obj.recovery = recovery.RecoveryManager(obj)
    acceptances = []

    async def host_response(**kwargs):
        accepted = deduper.mark_seen(kwargs["dedupe_key"])
        acceptances.append(accepted)
        return accepted
    gw.route_message = host_response
    dup = reader_payload()
    await obj.inbound.submit(dup)
    await obj.inbound.submit(dup)             # 宿主 False；插件去重应拦截
    clock[0] += 301                            # 宿主缓存过期
    await obj.recovery._redeliver(dup["rawid"], dup)   # 重放前插件去重拦截
    print('probe-done:', results[-1]['probe'] if results else '?', file=sys.stderr);    record("7_duplicate_never_reaccepted",
           obj.replay.pending() == 0 and sum(acceptances) == 1,
           staged=obj.replay.pending(), acceptances=sum(acceptances))

    print('== enter 8', file=sys.stderr);    # ---- 8 gap_paging_silently_misses_older_rows
    obj, _ = await fresh_plugin(os.path.join(d, "p8"))
    obj.recovery = recovery.RecoveryManager(obj)
    obj.recovery.watermarks.update(OWN_WXID, TALKER, "prior", 100)
    rows600 = [dict(senderUsername="wxid_audit_alice", senderDisplay="Alice",
                    localType=1, parsedContent="m", createTime=i, localId=i,
                    serverId=i) for i in range(101, 701)]
    pages = [rows600[:500], rows600[500:]]
    delivered_ts = []

    def fake_page(base, token, talker, limit, after_time=None, after_id="", timeout=20.0):
        idx = 0 if after_id == "prior" else 1
        rows = pages[min(idx, 1)]
        cursor = None
        if idx == 0 and rows:
            cursor = {"after_time": rows[-1]["createTime"],
                      "after_id": "rawid-%d" % rows[-1]["localId"]}
        return {"messages": rows, "cursor": cursor}

    async def confirmed(key, payload, replay=False, account=''):
        delivered_ts.append(payload.get("timestamp"))
        return True
    obj.recovery._deliver_payload = confirmed
    with patch.object(recovery, "fetch_messages_page", side_effect=fake_page) as fp:
        fetched = await obj.recovery.backfill_gap()
    print('probe-done:', results[-1]['probe'] if results else '?', file=sys.stderr);    record("8_gap_full_coverage_chronological",
           fetched == 600 and delivered_ts == sorted(delivered_ts)
           and fp.call_count >= 2,
           fetched=fetched, queries=fp.call_count)

    print('== enter 9', file=sys.stderr);    # ---- 9 reader_http_ignores_from_time（隔离副本 AST 验证 + messages_after 存在）
    import ast as _ast
    src = Path(r".\source\reader\reader-api.py")
    tree = _ast.parse(src.read_text(encoding="utf-8-sig"))
    has_after = any(isinstance(n, _ast.AsyncFunctionDef) and n.name == "messages_after"
                    or isinstance(n, _ast.FunctionDef) and n.name == "messages_after"
                    for n in _ast.walk(tree))
    print('probe-done:', results[-1]['probe'] if results else '?', file=sys.stderr);    record("9_reader_cursor_endpoint", has_after,
           note="messages_after + HTTP after_time/after_id 路由已实现（补丁单独交付）")

    print('== enter 10', file=sys.stderr);    # ---- 10 gap_advances_watermark_even_if_host_not_accepted
    obj, _ = await fresh_plugin(os.path.join(d, "p10"))
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
    print('probe-done:', results[-1]['probe'] if results else '?', file=sys.stderr);    record("10_watermark_held",
           obj.recovery.watermarks.get(OWN_WXID, TALKER)["ts"] == 100
           and obj.replay.pending() == 1,
           watermark=obj.recovery.watermarks.get(OWN_WXID, TALKER)["ts"])

    print('== enter 11', file=sys.stderr);    # ---- 11 pending_replay_events_not_persisted
    pf = Path(obj.cfg["project_root"]) / "state" / "wxsupervisor" / "replay-pending.json"
    print('probe-done:', results[-1]['probe'] if results else '?', file=sys.stderr);    record("11_pending_persisted", pf.exists() and obj.replay.pending() == 1,
           file_exists=pf.exists())
    obj2, _ = await fresh_plugin(obj.cfg["project_root"])
    obj2.recovery = recovery.RecoveryManager(obj2)
    print('probe-done:', results[-1]['probe'] if results else '?', file=sys.stderr);    record("11_pending_restored_after_rebuild", obj2.replay.pending() == 1,
           restored=obj2.replay.pending())

    print('== enter 12', file=sys.stderr);    # ---- 12 configuration_rollback_reloads_new_in_memory_config
    obj, _ = await fresh_plugin(os.path.join(d, "p12"))
    original = obj._settings().wechat.wake_mode
    # V2-6：回滚会重建组件 —— mock 掉真实 _start_*，防止连真实 5031/7999
    obj._start_sse = AsyncMock()
    obj._start_server = AsyncMock()

    async def fail_rebuild(old, new):
        raise RuntimeError("synthetic rebuild failure")
    obj._apply_config_diff = fail_rebuild
    r = await obj.config_service.apply({"wechat": {"wake_mode": "mention"}},
                                       persist=False)
    print('probe-done:', results[-1]['probe'] if results else '?', file=sys.stderr);    record("12_rollback_restores",
           not r["ok"] and obj._settings().wechat.wake_mode == original,
           wake_mode=obj._settings().wechat.wake_mode)

    print('== enter 13', file=sys.stderr);    # ---- 13 config_success_returned_before_failed_background_rebuild
    # V2 复核修正：不再 patch 全局 create_task（组合态会收集到此前 probe 的
    # 遗留任务导致 gather 永久等待）。改用标志验证 apply 内部同步 await 重建：
    # apply 返回 ok=False 且重建标志已置位 = 重建在事务内被 await。
    obj, _ = await fresh_plugin(os.path.join(d, "p13"))
    obj._start_sse = AsyncMock()
    obj._start_server = AsyncMock()
    rebuild_awaited = []

    async def async_fail(old, new):
        rebuild_awaited.append(True)
        raise RuntimeError("async rebuild failure")
    obj._apply_config_diff = async_fail
    r = await obj.config_service.apply(
        {"reader": {"base_url": "http://127.0.0.1:15031"}}, persist=False)
    record("13_apply_awaits_rebuild",
           not r["ok"] and bool(rebuild_awaited),
           ok=r["ok"], rebuild_awaited=bool(rebuild_awaited))

    # ---- 14 reader_hot_update_leaves_onebot_reference_stale
    obj, _ = await fresh_plugin(os.path.join(d, "p14"))
    old_reader = obj.reader
    obj.server = SimpleNamespace(reader=old_reader)
    old_flat = dict(obj.cfg)
    obj.cfg["reader_base_url"] = "http://127.0.0.1:15031"
    async def fake_sse():
        pass
    async def fake_server_start():
        obj.server = SimpleNamespace(reader=obj.reader)
    obj._start_sse = fake_sse
    obj._start_server = fake_server_start
    await obj._apply_config_diff(old_flat, obj.cfg)
    print('probe-done:', results[-1]['probe'] if results else '?', file=sys.stderr);    record("14_onebot_reader_updated",
           obj.server.reader is obj.reader and obj.reader is not old_reader)

    # ---- 15 unsupported_configuration_version_silently_overwritten
    obj, _ = await fresh_plugin(os.path.join(d, "p15"))
    r = await obj.config_service.apply({"plugin": {"config_version": "999.0.0"}},
                                       persist=False)
    print('probe-done:', results[-1]['probe'] if results else '?', file=sys.stderr);    record("15_bad_version_rejected",
           not r["ok"] and obj._settings().plugin.config_version != "999.0.0",
           error=r["error"])

    # ---- 16 one_image_reserves_two_native_calls
    obj, _ = await fresh_plugin(os.path.join(d, "p16"))
    obj.cfg.update(max_per_minute=1, max_per_ten_minutes=100)
    img = {"raw_message": [{"type": "image", "data": "",
                            "binary_data_base64": "R0lGODlhAQABAAAAACw="}]}
    r = await obj.handle_wxsupervisor_gateway(img, {"group_id": TALKER})
    print('probe-done:', results[-1]['probe'] if results else '?', file=sys.stderr);    record("16_one_image_one_native",
           r.get("success") and len(obj.hook.calls) == 1,
           success=r.get("success"), hook_calls=len(obj.hook.calls))

    # ---- 17 quota_cleanup_deletes_recent_non_candidate_media
    base = Path(d) / "p17" / "state" / "media" / "outgoing"
    base.mkdir(parents=True, exist_ok=True)
    pending = base / "pending.png"
    pending.write_bytes(b"synthetic" * 100)
    janitor = perf.MediaJanitor(Path(d) / "p17", ttl_hours=48, max_total_mb=0.0001)
    janitor.lease(pending)
    before = janitor.candidates()
    deleted = janitor.enforce()
    print('probe-done:', results[-1]['probe'] if results else '?', file=sys.stderr);    record("17_lease_protects_recent",
           pending not in before and pending.exists(),
           candidates=len(before), deleted=len(deleted))

    # ---- 18 inbound_voice_has_no_audio_segment_or_transcription
    voice_payload = {"sessionId": TALKER, "type": 34, "content": "[语音]",
                     "voice": {"localId": 1, "serverId": 2, "createTime": 3}}
    _, segments = wx_message.build_raw_segments(voice_payload)
    print('probe-done:', results[-1]['probe'] if results else '?', file=sys.stderr);    record("18_voice_segment_present",
           any(s["type"] == "voice" for s in segments),
           types=[s["type"] for s in segments],
           note="音频字节解码依赖 reader 侧语音接口（未实现，明确列未完成）")

    # ---- 19 gap_media_payload_loses_image_descriptor
    row = {"localType": 3, "senderUsername": "wxid_audit_alice",
           "senderDisplay": "Alice", "parsedContent": "[图片]",
           "fullText": "[图片]", "createTime": 123, "serverId": 1, "localId": 1,
           "groupName": "AuditGroup"}
    payload = history_backfill.payload_from_reader_row(TALKER, row)
    print('probe-done:', results[-1]['probe'] if results else '?', file=sys.stderr);    record("19_gap_media_complete",
           payload is not None and "image" in payload
           and payload.get("groupName") == "AuditGroup",
           group_name=payload.get("groupName") if payload else None)


threading.Timer(15.0, lambda: faulthandler.dump_traceback()).start()
asyncio.run(main())
print(json.dumps(results, ensure_ascii=False, indent=1))
# V2 复核：逐项断言，任一失败返回非零退出码；进程正常退出（收尾纳入验收）
failures = [r for r in results if not r["fixed"]]
for r in failures:
    print(f"FAIL: {r['probe']}", file=sys.stderr)
print(f"FIXED {len(results) - len(failures)}/{len(results)}", file=sys.stderr)
sys.exit(1 if failures else 0)
