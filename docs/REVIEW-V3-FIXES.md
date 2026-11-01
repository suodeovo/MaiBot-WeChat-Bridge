# V3 发布门槛返修结果（1.4.0-wxfix.4）

对照 `review_20261101_v3/boundaries.py` 的 4 项发布门槛。返修后
**boundaries 4/4 通过（failed=0, exit 0）**，全量回归 123 项 OK，
验收探针 20/20（exit 0）。旧候选 1.3.0-wxfix.3 作废。

## 逐项修复

| # | 发布门槛 | 根因 | 修复 | 验证（boundaries.py 复跑） |
|---|---|---|---|---|
| G1 | 真正新进程后来源账号丢失 | `_event_accounts` 是类级可变字典（跨实例污染）；`_restore_pending()` 不恢复 origin | `_event_accounts` 改实例属性；`_restore_pending` 完整恢复 origin（含空串占位防回退当前账号）；来源缺失的旧记录隔离处理不猜账号 | 全新子进程：restored_origin=来源账号 ✓；当前账号≠来源时不投递、pending 保留 ✓ |
| G2 | 两个缺口只确认第一个就跨第二个 | 最早未确认只存 min_ts，确认后直接删标志不重算 | `_pending_ts` 有序集合（account/session → sorted ts 列表）；ack_event 按确切事件移除；水位推进 = 剩余 pending 的 min 边界与 confirmed_latest 的较小者；sync/async 两路径同一语义；水位 rawid+ts 来自同一 payload | 确认 A 后 B 仍 pending、水位不越 B ✓ |
| G3 | 真实组件 stop 后不重启 | `_is_active` 对无 active 属性对象缺省 True，真实 SseConsumer/OneBotServer 无该属性 | 真实组件实现 `is_active()`（SseConsumer=running()；OneBotServer=_server 非 None）；`_is_active` 优先调用 `is_active()` → `active` 属性 → 无接口视为可用 | 真实类 stop 后 `_is_active=False`，回滚 `rebuild_stopped` 重启两个工厂 ✓ |
| G4 | 游标锚点缺失丢同秒邻居 | 锚点消失分支用 `> after_time` 排除整个同秒 | 游标扩展 `after_local_id`（完整排序键后半）：锚点缺失 + 有 local_id → `(ts, local_id) >` 精确比较；旧格式游标 → 受控重扫 `>= after_time`（去重由调用方兜底）；cursor 响应带 `after_local_id` | 锚点缺失返回 3 条同秒邻居 ✓ |

## 附加修正

- `_redeliver`/`on_message_processed` 的 ack 调用统一传 account+payload
  （G2 边界重算需要确切事件身份）。
- 水位保存的 rawid 与 ts 来自同一 payload（身份一致，复核要求）。
- HTTP 游标路由新增 `after_local_id` 参数（向后兼容：不带时走旧语义）。

## 交付物

- 插件包 `dist/local_wxsupervisor-1.4.0-wxfix.4.zip`
  SHA-256 `<plugin_sha256>`
- reader 补丁 `patches/reader-api.cursor-patch.py`
  SHA-256 `<reader_sha256>`
- 逐文件校验 `dist/SHA256SUMS.txt`

## 测试命令与结果

```
cd .\tests
C:\...\python.exe -m unittest discover -s .            # 123 项 OK
C:\...\python.exe final_acceptance_probes.py           # 20/20, exit 0
cd .\review_20261101_v3
C:\...\python.exe boundaries.py                        # 4/4 passed, exit 0
```

## 仍为未完成（不计入发布门槛）

1. 历史回填进入模型上下文（宿主契约未实现）。
2. 接收语音音频解码/转写（规格见 reader-cursor-patch.md 附录）。
3. 临时目录 ResourceWarning（helpers 层清理时序，不影响正确性；下轮清理）。
4. PROGRESS.md 表格与 1.2.0 内容混排（本轮已更新版本号与状态段）。
