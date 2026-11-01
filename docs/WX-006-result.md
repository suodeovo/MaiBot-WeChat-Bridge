# WX-006 结果：失败重放与断线补收

## 实现

**stats.EventReplay 重写**（修复 `take()` 先删后返回恒 None）：

- `take(key)` = peek（返回不删除）；`ack(key)` = 确认后删除；
  `failed(key, error)` = 计数 + 指数退避（5s×2^n 封顶 10 分钟）；
  超过 max_attempts(8) 进入 dead（**保留事件**，`snapshot()` 可观测），
  不静默丢弃。stage 覆盖保留最新，容量 200 淘汰最旧，全部计数可见。

**recovery.py 新增**：

- `WatermarkStore`：接收水位按 (账号, 原始会话) 记录 rawid+ts，
  持久化到插件自有目录 `state/wxsupervisor/recovery-state.json`（原子写）。
- `RecoveryManager`：worker 每 5s 取 take_ready 事件重投（经统一规范化、
  停用/白名单重检、去重键 rawid）；Host 接受 → ack + 水位推进；
  拒绝/异常 → failed() 退避。Host accepted 只代表 Host 接收（文档明确，
  不宣称端到端 exactly-once）。
- `backfill_gap(reason)`：SSE 重连回调/启动时按水位 -5s 重叠回看，
  从 reader `/api/v1/messages`（已核实支持 from_time；隔离副本验证）
  分页拉取（500×3 页），rawid 去重后经统一入站管线投递；超过页数上限
  记入 `gaps` 明确暴露。首次启动无水位不回看。
- on_load 启动、on_unload stop；补收与实时流共用 `_submit_inbound`
  一条管线，不形成重复入队。

## reader 契约核实

`/api/v1/messages` 原生支持 `from_time` 参数（reader-api.py:362
`messages(talker, limit, from_time)`），当前安装版已可覆盖水位回看；
无需立即打 reader 补丁。若断线窗口超过 3 页（1500 条/会话），gap 显式记录。

## 测试

- stage/take/ack 生命周期；退避与 dead 状态保留
- 水位持久化跨实例读回
- 重投：接受 ack 清零；拒绝保留且 attempts=1（故障注入 gateway.accept=False）

结果：全绿。
