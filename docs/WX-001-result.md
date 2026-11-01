# WX-001 结果：统一消息身份与会话策略

## 实现

新增 `source/local_wxsupervisor/identity.py`：

- `NormalizedMessage`：入站边界统一身份结构（account / session_id（原始 wxid）/
  conversation_id（reader 稳定数字 ID）/ group_name / sender_id / sender_name /
  session_display / rawid / timestamp / text / 原始 payload 引用）。
- `normalize_payload()`：字段映射严格按 reader 实际契约 ——
  发言人=`talkerId`（兼容旧 `sender`，优先级 talkerId > sender > sessionId）、
  群名=`groupName`、群聊 `sourceName`=发言人显示名只作别名。
- `SessionDirectory`：稳定 wxid ↔ 显示名/数字 ID 目录；`resolve_alias()` 返回
  ok / ambiguous / unknown，同名群歧义直接暴露不猜测。
- `SessionPolicy`：白名单（群/私聊**分别**判空，空=该类不限）、停用
  （入站/出站/重放/回填共用，发送前重新检查）、唤醒模式（分群覆盖键解析到
  稳定 wxid）。settings 经 `plugin._settings()` 动态读取，热更即时生效。

改造 `inbound.py`：submit 走 normalize → policy.allows_inbound → wake_allows；
`policy_allows()` 保留为兼容入口。`gateway.build_message_dict()` 移除
「空 sender 回退群 ID」：群聊缺发言人时保留空 user_id 并告警，绝不合并成员身份。

## 测试（tests/test_wx001_008_stages.py::Wx001IdentityTests + WX-000 身份用例）

- 两人同群不混身份（user_id=各自 wxid，group_name 固定）
- talkerId/sender 兼容优先级；私聊 sourceName 作显示名
- 群名/数字 ID/原始 wxid 三种白名单写法均放行；仅群白名单不封私聊
- 同名歧义返回 ambiguous；停用按别名解析且阻断出站；数字 ID 分群唤醒覆盖生效
- 停用后发送前重检（test_send_recheck_rejects_disabled_after_queue）

命令：`python -m unittest discover -s tests`（在 tests 目录）。结果：全绿。
