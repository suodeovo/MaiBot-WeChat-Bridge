# WX-007 结果：历史回填与模型上下文验证

## 宿主契约核实（决定交付形态）

宿主 MaiBot 1.3.16（`src/maisaka/runtime.py` 304/335/429、
`src/chat/replyer/maisaka_generator_base.py`、`src/maisaka/reasoning_engine.py:604`、
`src/maisaka/context/{history,planner_messages}.py`）：

- `is_notify=True` 消息会入库，但被**所有模型上下文恢复路径过滤**，
  且不触发回复。
- 旧 docstring「is_notify 进上下文」与宿主实际相反。
- `route_message` accepted 只是 platform_io_manager 接收（host/supervisor.py:1760）。
- 宿主无安全历史导入契约（全核心无 history_import 类接口）。

## 实现（history_backfill.py 重写）

- 白名单别名先经 SessionDirectory 解析到**原始会话 wxid** 再查询；
  无法解析的条目跳过并告警（修复数字别名直传数据库）。
- 查询参数 `urllib.parse.urlencode`（修复未编码）；支持 `from_time`。
- 只回填未停用且白名单允许的会话（停用会话不回填）。
- reader 倒序拉、**时间正序**投；payload 统一身份字段（talkerId/groupName）；
  rawid 与 reader push_payload 同算法（message_id 一致，宿主去重生效 →
  重复回填不增重复历史）。
- **只累计 Host 实际接受**的投递；拒绝/异常计入 rejected 并告警
  （修复虚报成功）。
- 去重基础与断线补收共享（rawid），但语义分开：主动回填只补上下文
  （is_notify=True，不触发回复）；断线补收恢复实时事件（WX-006）。

## 「历史进入模型上下文」的验证结论

按任务书「审计已看到宿主部分路径会过滤 is_notify，不能凭
is_notify=True 或 accepted 宣布成功」：在宿主 1.3.16 上**无法**用现有
安全契约让回填历史进入模型上下文 —— is_notify=True 是防回复触发的
正确开关，但同开关被上下文恢复排除。

**交付状态**：
1. is_notify=True 回填（默认）：安全、可验收 —— 消息入库、重复回填去重、
   绝不触发回复；「进上下文」标记为**待宿主契约**。
2. 宿主接口候选：`docs/host-history-import-proposal.md`（最小
   `host.import_history` RPC 提案 + 测试计划），未实现、单独交付、
   不藏在插件包中。

## 测试

- WX-000：别名解析为原始 wxid 才查询；Host 拒绝不计成功（count=0）
- 补充：停用会话跳过回填；payload 身份字段正确；正序投递

结果：全绿。
