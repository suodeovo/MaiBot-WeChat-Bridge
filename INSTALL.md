# 微信桥接使用说明

## 安装

1. **先安装 MaiBot。** Windows 用户可下载官方 MaiBot OneKey 启动器，用它安装并启动 MaiBot 1.4.3。打开 WebUI，选择要接入微信的 Bot，配置模型供应商和聊天模型。[下载启动器](https://github.com/Mai-with-u/MaiBotOneKey/releases) · [官方部署教程](https://docs.maibot.chat/manual/)
2. **准备微信环境。** 第一次安装可下载含 WeChatAgent 安装器的完整包。安装 WeChatAgent 0.4.1，登录微信 4.1.10.27，完成数据库连接验证。已有匹配环境可直接使用其 `data/workspace`。[插件发布页](https://github.com/suodeovo/MaiBot-WeChat-Bridge/releases/tag/v1.4.3) · [运行环境说明](runtime/README.md)
3. **安装插件。** 在 WebUI 选中目标 Bot，再进入插件页安装。手动安装可把 `local_wxsupervisor-1.4.3.zip` 解压到该 Bot 的 `plugins/`。MaiBot 1.4.3 新建 Bot 的目录通常为 `<工作区>/bots/<bot_id>/plugins/`，原地迁移的旧 Bot 沿用原目录。确认 `local_wxsupervisor/` 下直接包含 `plugin.py` 和 `_manifest.json`。[官方实例目录说明](https://github.com/Mai-with-u/MaiBot/blob/v1.4.3/docs/multi-bot-host.md)
4. **配置路径。** 暂停插件、WeChatAgent 聊天服务和旧 reader，保留已登录的微信。打开插件目录，将命令中的路径换成实际安装位置。准备工具会检查独立的 reader 环境、备份旧文件、部署补丁并写入插件配置。

   ```powershell
   python tools/prepare-runtime.py --project-root "C:\WeChatAgent\data\workspace" --patch-reader --configure
   ```

   若手动安装后提示插件自身缺少依赖，在该 Bot 使用的 Python 环境中运行 `python -m pip install -r requirements.txt`。

5. **启用并试用。** 在插件配置中填写允许回复的私聊或群聊，再启用插件。用另一个微信账号发一条测试消息，在 WebUI 或日志确认 MaiBot 收到，回到微信确认回复送达。

## 升级

- 1.4.3 的声明范围为 MaiBot 1.3.16–1.4.3。[兼容说明](docs/COMPATIBILITY.md)
- 暂停相关服务，备份现有插件、reader、配置和运行状态。
- 按 reader → 插件的顺序更新；客户端或 hook 有更新时，按客户端部署说明处理。
- 保留生产配置，并核对新版本的配置要求。
- 恢复服务后，分别检查连接、基础收发及受影响功能。

## 回退

恢复备份中匹配的插件、reader 和配置，再重新启动服务。客户端和 hook 若一并更新，也应使用备份中的匹配版本。

## 部署检查

- reader 状态端点：`GET http://127.0.0.1:5031/api/v1/reader/status`。
- 补收接口：`GET /api/v1/messages`，配套补丁支持游标分页。
- MaiBot 插件加载日志无相关错误。
- 若启用 OneBot 服务，确认配置端口正在监听；默认端口为 `7999`。
- 从外部微信账号验证真实入站消息，并通过 MaiBot 验证实际出站。

reader 和 hook 可能要求认证。请求应使用目标机器生成的凭据，避免将凭据写入文档或分享包。
