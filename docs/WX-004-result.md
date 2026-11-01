# WX-004 结果：统一出站调度、限流与发送状态

## 实现

新增 `source/local_wxsupervisor/send_queue.py::SendScheduler`：

- **原子预留**：`acquire()` 在锁内完成额度检查 + 名额入账 + permit 签发；
  旧「检查后、发送后计数」的竞态窗口消除。permit 持有自己预留条目的引用，
  回滚只删自己的条目（修复盲滚最近一条会误伤他人额度的语义）。
- **额度单位 = 实际原生发送尝试**：多段消息按 `native_count` 计数
  （1 文本 + 各媒体段）；429 内部重试不重复计数；REJECTED 回滚、
  SUBMITTED 保留。每分钟/每十分钟上限按此口径。
- **时钟**：注入单调时钟（ControlledClock 测试可控），等待/超时预算
  全部单调。
- **会话隔离**：按规范化目标维护轮次（round_window/turn_quiet/round_min_gap
  语义保留）；B 群新会话必须等完整 min_interval。`_throttle_wait`/
  `_mark_sent` 保留兼容入口委托 scheduler。
- **并发上限**：`hook.max_concurrent_sends` 接通为 `_in_flight` 上限。
- **幂等**：带稳定 request_id 的请求在 TTL 300s 内已提交/已确认即去重；
  无稳定 ID 该层无保证（明确边界）。
- **状态机**：UNATTEMPTED → REJECTED / SUBMITTED → CONFIRMED / UNKNOWN /
  PARTIAL / FAILED。`ret=0` 记 SUBMITTED 不升级；metadata 带
  `delivery: submitted`。

网关入口接通调度：空消息检查 → acquire（含 max_wait 预算）→ resolve →
**出站前停用重检** → 发送 → finish(SUBMITTED/FAILED) + finish_turn。

## 测试（Wx004SendQueueTests + WX-000 节流用例）

- 上限 1 时两个并发请求都被拒且 0 次原生调用（额度占满 + 注入对照）
- 3 段消息占满 3 名额，第 2 条 1 段被拒；提交后名额保留
- REJECTED 只回滚自己的条目
- A 群同轮立即可发、B 群新会话等 15s 间隔（可控时钟）
- 稳定 ID 去重命中

结果：全绿。
