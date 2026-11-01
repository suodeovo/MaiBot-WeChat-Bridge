# WX-003 结果：五条管理命令与权限

## 实现

`admin.py` 重写为 `AdminCommandsMixin`：五条命令（wx_status / wx_disable_chat /
wx_enable_chat / wx_chat_mode / wx_tts_mode）作为插件实例方法上的 `@Command`
装饰器声明 —— `collect_components` 扫实例属性即可收集（修复局部函数收集为 0）。

- 回复：`_admin_reply` **await** `ctx.send.text()` 并检查返回（修复未 await）。
- 权限：`_is_admin` 稳定 wxid **精确匹配**（trim 后全等），移除子串/后缀/
  去 `wxid_` 前缀的模糊匹配。**行为变更：管理员名单为空时所有命令拒绝**
  （含 /wx状态）；磁盘当前名单为空，部署后需先在 WebUI 配置管理员。
- 写命令经 `config_service.apply()`：完整合并 + 版本字段 + 持久化 + 应用；
  保存失败回复「保存失败: …（配置未生效）」，不回「已修改」；
  写盘失败（运行态已生效）附加「写盘失败，重启后丢失」提示。
- 会话参数经 SessionDirectory 解析到稳定 wxid；歧义/未知明确回复。

## 测试（tests/test_wx003_admin.py，8 项专项 + WX-000 收集用例）

- 空管理员名单：写命令拒绝且回复发出、配置未生效
- 精确匹配成功；相似 wxid（wxid_boss_admin / boss / xxwxid_boss）全部拒绝且配置未动
- 回复确实被 await 且检查结果（send 返回 False 时命令结果为失败）
- 写命令持久化后重建实例从磁盘读回（disabled_chats=原始 wxid）
- 保存失败明确回复失败（disk-full 注入），不回成功样式
- 数字 ID 停用参数解析到原始 wxid 入名单
- 歧义别名拒绝写入停用名单（目录 merge 语义修复后通过）

命令：`python -m unittest discover -s tests`。结果：56 项全绿（连续两次稳定）。
