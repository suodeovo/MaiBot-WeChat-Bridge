# WX-002 结果：配置持久化、热更新、服务开关与动态 API 生命周期

## 实现

新增 `source/local_wxsupervisor/config_service.py`：

- 唯一写入路径 `apply(changes, persist, merge)`：完整配置保留
  `plugin.config_version` → 模型校验（WxSupervisorSettings.model_validate +
  validate_runtime）→ 运行态应用（set_plugin_config + 扁平 cfg + 消费者差分重建）
  → 原子写盘（toml_writer，宿主无 tomli_w 时内置最小写出器）。
- 持久化成功与运行态应用失败分别处理：验证失败不动运行态；运行态失败回滚；
  写盘失败明确暴露（ok=True/persisted=False），不静默。
- 修改合并（深合并）而非重置其他节。

`plugin.py` 改造：

- `on_config_update` 区分 scope：`self` 走 config_service；`bot` 是全局广播，
  只记录不覆盖插件配置节（修复把全局配置当插件配置）。
- `_apply_config_diff` 按依赖原子重建：reader 地址/令牌变化 → ReaderClient +
  图片解析器缓存失效 + 账号缓存失效 → SSE → hook → OneBot 服务端。
- `_start_server` 读取 `server.enable`（修复读成 `ob_enable` 无消费点）；
  启动、热更、守护循环三处统一遵守；关闭 OneBot 不影响网关与 SSE；
  看门狗不再拉起被关闭的服务。
- `_register_apis` 按实例注册 + sync（replace 契约），删除类标志
  `_apis_registered`（修复第二实例 API 表为空）。
- `on_unload` 清理 watchdog/SSE/server/端口等待，重复加载可清理已建任务。

## 测试（Wx002ConfigServiceTests + WX-000 生命周期用例）

- apply 更新运行态扁平 cfg；非法配置拒绝且运行态不变；缺版本节自动补齐
- persist 写盘后新实例从磁盘读回（重建实例配置保留）
- reader 地址热更重建 ReaderClient/ImageResolver（旧实例引用断言）
- server.enable=false 不启动；同进程两实例各有 1 个动态 API
- 禁用 OneBot 服务后 _start_server 不创建（FakeServer 计数=0）

结果：全绿。
