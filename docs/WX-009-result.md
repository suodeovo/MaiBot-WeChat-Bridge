# WX-009 结果：性能优化、集成验收与候选包

## 实现（perf.py 新增 + plugin.py/inbound.py/onebot_server.py 接入）

1. **同步操作出事件循环**：`WorkerPool`（受限 ThreadPoolExecutor，可配
   `project.media_workers`）承接图片转码等阻塞操作；`_account_async()`
   带 3s 缓存；OneBot `_self_id()` 带 10s 缓存 —— 没有 OneBot 客户端时
   不再逐消息查询 reader。
2. **慢媒体按会话调度**：`SessionMediaScheduler` —— 同会话保序
   （asyncio.Lock per session）、跨会话并行、全局并发受 semaphore 约束；
   入站图片解码已接入（一个群的慢图片不堵其他群文本）。
3. **媒体暂存清理**：`MediaJanitor` —— 候选清单（`candidates()`）先于
   删除（`enforce()`）；只处理插件自有目录 allowlist（state/{media,voice,
   stickers,files}/outgoing）；TTL（project.media_ttl_hours，默认 48h）+
   总量上限（project.media_max_mb，默认 512MB）；保留期内文件永不进候选
   （待发送/待回执文件都是新写入）；插件卸载时停止清理任务。
4. **依赖配置化**：pilk / pycryptodome / wechat_media 脚本路径从硬编码
   D 盘路径改为 `project.pilk_paths / crypto_paths / media_scripts_dir`
   （分号分隔，有默认值兼容现状）；`perf._configured_paths()` 注入；
   manifest 依赖声明同步更新。不把整个运行时加进全局搜索路径。

## 集成验收（test_wx009_integration.py，12 项）

- 入站→策略→投递→拒绝重试→暂存→重放→ack 全链路
- 配置热更后策略即时生效且无旧配置残留（停用/解除）
- 幂等+额度+停用组合（停用会话不触达原生；稳定 ID 去重）
- SSE 重连回调触发补收；生命周期 load/unload/load 无遗留任务
- 媒体清理候选先验证后删除；工作池与保序调度器行为
- **故障注入复验**：旧身份形状（群 ID 合并）、旧 take 形状（先删后返回）、
  旧节流形状（账本清空窗口）、旧 SSE unbounded 模式 —— 四项注入均被
  当前断言机制发现，测试没有测到替身自身。

结果：68 项全绿（连续两次稳定，exit 0）。

## 配置消费点清单（全部界面选项）

| 节.字段 | 消费点 | 状态 |
|---|---|---|
| plugin.enabled / auto_start | on_load | 有效 |
| plugin.config_version | config_service.validate / SDK | 有效 |
| project.root / python | reader_spec / 状态目录 | 有效 |
| project.media_ttl_hours / media_max_mb / media_workers | MediaJanitor / WorkerPool / Scheduler | 有效（WX-009 新增） |
| project.pilk_paths / crypto_paths / media_scripts_dir | perf._configured_paths | 有效（WX-009 新增） |
| reader.base_url / autostart / stop_on_unload / watchdog / watchdog_seconds | ReaderClient/托管/守护 | 有效 |
| hook.base_url / expected_version / wechat_executable | HookNative / launcher | 有效 |
| hook.request_timeout_ms / receipt_timeout_ms | _load_config 产出，发送调度以 max_wait_ms 与 hook 超时为准 | **标注：单请求超时由 hook 端点硬编码超时承担，插件侧未逐请求应用 —— 见限制说明** |
| hook.max_concurrent_sends | SendScheduler.max_concurrent | 有效（WX-004 接通） |
| launcher.* | 配置动作/动态 API | 有效 |
| server.host / port / token / enable | OneBotServer + 三处开关 | 有效 |
| wechat.whitelist_groups / whitelist_private / disabled_chats / administrators / chat_modes / wake_mode / wake_words | SessionPolicy（WX-001 统一） | 有效 |
| wechat.max_reply_messages | **无运行消费点**（MaiBot 拆条由核心决定） | **标注限制：界面保留但实际由宿主管；说明已写入配置描述** |
| wechat.backfill_count | HistoryBackfill | 有效 |
| limits.batch_ms | **无运行消费点**（聚批由宿主防抖承担） | **标注限制：同上** |
| limits.min_interval_ms / private_min_interval_ms / max_per_minute / max_per_ten_minutes / round_* / max_wait_ms | SendScheduler | 有效 |
| tts.*（全部） | _tts_plan / 合成 / 发送 | 有效 |

限制说明（不静默）：`hook.request_timeout_ms/receipt_timeout_ms`、
`wechat.max_reply_messages`、`limits.batch_ms` 在当前宿主契约下无插件侧
消费点；字段保留并已在 config 描述中标注「由宿主/hook 端点决定」，
避免用户误以为插件侧生效。

## 候选包

见 `dist/`（打包脚本 `scripts/build_candidate.py`，SHA-256 见
`dist/SHA256SUMS.txt`）。
