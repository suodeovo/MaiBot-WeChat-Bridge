# 宿主历史导入契约提案（WX-007 候选，未实现）

## 背景

宿主 MaiBot 1.3.16 中 `is_notify=True` 消息会入库，但被所有模型上下文
恢复路径过滤（maisaka/runtime.py 304/335/429、replyer 历史、planner 注入），
且不触发回复。插件历史回填要实现「进入模型上下文且不触发回复」，
宿主需要提供安全历史导入契约。

## 候选接口：`host.import_history`（最小扩展）

在 `plugin_runtime/host/supervisor.py` 的 RPC 注册表（`host.route_message`
旁）新增：

```
host.import_history(envelope) -> envelope
payload: {
    "gateway_name": str,          # 必须与网关 ready 声明一致
    "messages": [message_dict],   # 与 route_message 同构，时间正序
    "dry_run": bool,              # True 只校验不落库
}
response: {"accepted": int, "rejected": int, "errors": [...]}
```

语义约束（安全层）：

1. 消息标记为 `source_kind="history_import"`，入库时 `is_notify=True`
   保留（回复链路照旧过滤）；
2. `_restore_recent_context_from_db` / `restore_proactive_user_context` /
   `build_retro_reply_history` 三处的过滤条件从 `message.is_notify` 放宽为
   `message.is_notify and message.source_kind != "history_import"`；
3. 接受数量以 platform_io_manager 实际入库为准；
4. 幂等沿用 message_id 去重（runtime.py:429 既有逻辑）；
5. 限频：每会话每分钟最多 1 次 import_history 调用。

## 验收测试计划（宿主侧）

1. dry_run 校验：非法 gateway/会话拒绝，不入库。
2. 导入后 `find_messages` 可见且 `source_kind=="history_import"`。
3. `_restore_recent_context_from_db` 恢复的历史包含导入消息。
4. 同会话正常消息照常触发回复（is_notify 语义未被破坏）。
5. 导入消息不进入 planner 注入（reasoning_engine 604 过滤保持）。

## 插件侧对应改动（宿主接受后）

`history_backfill.HistoryBackfill._deliver_one` 改走
`ctx.gateway.import_history(...)`（SDK 网关能力新增方法），回退路径
保留现有 is_notify=True route_message（安全层不变）。

## 当前状态

- 宿主 1.3.16 无此接口 —— **未实现、未打补丁**。
- 本提案单独交付，不藏在插件候选包内。
- 在宿主接受提案前，插件回填保持 is_notify=True（安全：不触发回复、
  消息入库、宿主去重生效），「进入模型上下文」标记为待真实兼容验收。
