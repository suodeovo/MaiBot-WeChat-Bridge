# WX-000 结果：基线与回归测试

## 基线核实（2026-11-01）

| 项 | 值 |
|---|---|
| 宿主 Python | 3.12.10（54622d30 环境） |
| MaiBot | 1.3.16 |
| maibot-plugin-sdk | 2.16.0 |
| websockets | 17.3 |
| 插件 18 个 .py SHA-256 | 与审计结果 JSON **全部一致**（源码自审计后未变） |
| AGENTS.md | 任务涉及路径下无项目级 AGENTS.md（启动器 AGENTS.md 属 Electron 项目，不适用） |

隔离目录 `.`：
- `source/local_wxsupervisor/` 生产插件只读副本（基线 = 审计哈希）
- `source/reader/reader-api.py` reader 只读副本
- `tests/` 正向回归套件（unittest，宿主 Python 可直接跑）
- 生产插件目录、reader、宿主安装目录未改动

## 复现点 → 正向断言映射表

原 14 个 `observed=true` 复现点逐条映射到修复后的行为断言（`tests/test_wx000_regression.py` 及后续 WX 阶段测试）。每条都有对应的故障注入/旧形状对照，防止测到替身自身。

| # | 复现点 | 正向断言 | 所在测试 | 故障注入对照 |
|---|---|---|---|---|
| 1 | group_sender_identity_collapses | 两个成员投递后 user_id 分别为两个 wxid，group_name 保持 reader 的 groupName | WX000IdentityProbes.test_two_members_keep_distinct_user_ids | collapsed 形状断言 NotEqual + 去重计数 |
| 2 | documented_group_whitelist_forms_rejected | 群名 / reader 数字 ID / 原始 wxid 三种白名单写法均放行 | test_group_whitelist_accepts_name_and_reader_numeric_id、test_group_whitelist_accepts_raw_wxid | 旧实现按 sessionId/sourceName 比较的形状已由审计 JSON 记录 |
| 3 | group_whitelist_blocks_unrestricted_private_chats | 仅填群白名单时私聊不受限；私聊白名单独立生效 | test_private_chats_unrestricted_when_only_group_whitelist_set、test_private_whitelist_filters_private_chats | — |
| 4 | five_admin_commands_not_registered | collect_components 收集到 5 条命令 + 网关（控制组） | Wx000AdminCommandProbes.test_five_commands_registered_and_collected | 审计 JSON collected_commands=[] 记录旧形状 |
| 5 | admin_config_writes_rejected_by_sdk | 写命令走 config_service（完整配置+版本+合并），不再用 set_plugin_config 局部节 | test_admin_config_write_uses_persistent_service | PluginConfigVersionError 行为已由审计记录 |
| 6 | replay_take_deletes_without_returning | take() 返回事件且 ack 前不删除；ack 后清零 | Wx000ReplayProbe.test_take_returns_event_and_keeps_until_ack | OldReplay 子类复刻旧 take 形状，确认断言可发现 |
| 7 | disabled_onebot_server_still_starts | server.enable=false 时 _start_server 不创建/不启动 | Wx000LifecycleProbes.test_disabled_onebot_server_never_starts | FakeServer 计数为旧形状断言 |
| 8 | reader_url_reload_leaves_clients_stale | reader 地址变更重建 ReaderClient/ImageResolver/SSE/服务端引用 | test_reader_url_reload_rebuilds_all_clients | 旧形状（reader is old_reader）由审计记录 |
| 9 | new_instance_loses_dynamic_open_wechat_api | 同进程两个实例各自注册并 replace，各有 1 个 API | test_new_instance_keeps_dynamic_api | _apis_registered 类标志复现路径保留在审计 JSON |
| 10 | concurrent_sends_exceed_per_minute_limit | 上限 1 时两个并发请求只有 1 个成功，计数为 1 | Wx000ThrottleProbe.test_concurrent_sends_capped_by_per_minute_limit | 两请求都成功的旧形状由审计记录 |
| 11 | tts_request_state_shared_across_chats | 语音请求按会话隔离：A 群请求不影响 B 群判定 | Wx000TtsProbe.test_tts_request_state_isolated_per_session | 单份全局状态的旧形状由审计记录 |
| 12 | backfill_uses_whitelist_alias_as_database_talker | 查询参数必须是解析后的原始 wxid | Wx000BackfillProbes.test_backfill_resolves_alias_and_counts_accepted_only | fetch 收到别名的旧形状由审计记录 |
| 13 | backfill_counts_rejected_routes_as_success | Host 拒绝的投递不计成功 | 同上（count==0 断言） | count==1 旧形状由审计记录 |
| 14 | sse_queue_does_not_bound_pending_puts | 跨线程待提交 put 数有明确上限（≤2） | Wx000SseProbe.test_sse_pending_puts_bounded | 588 个待完成 put 的旧形状由审计记录 |

## 缺陷状态

- **已复现（14/14）**：全部由审计脚本在隔离环境观察到，源码哈希一致 → 14 项全部有效。
- **尚未复现**：无。
- **已被其他修改解决**：无（基线未变）。

## 新核实的宿主契约（影响 WX-007）

宿主 1.3.16（`src/maisaka/runtime.py`、`src/chat/replyer/maisaka_generator_base.py`、`src/maisaka/reasoning_engine.py`）：
- `is_notify=True` 消息会入库，但被 **三处上下文恢复、回复器历史、planner 注入全部过滤**。
- 旧插件「is_notify 消息进模型上下文」的前提与宿主实际相反。
- `route_message` 返回的 accepted 只是 platform_io_manager 接收，无历史导入专用契约。

结论：WX-007 的「历史确实进入模型上下文」在宿主 1.3.16 上无现有安全契约可达成；
按任务书保留安全行为（is_notify=True 不触发回复），宿主接口候选单独交付，标记待真实兼容验收。

## 统一测试入口

```
cd .
C:\Users\baby1\...\54622d30...\python.exe -m unittest discover -s tests -v
```

（WX-000 骨架中引用的 `policy_allows` / `config_service` / `_clock` / `SendScheduler` /
`_tts_plan(session=)` / `_stream_once_test` 是后续阶段交付的 API；未实现前这些用例按
预期失败/报错，即「红」。每个 WX 阶段完成后对应用例转「绿」。）
