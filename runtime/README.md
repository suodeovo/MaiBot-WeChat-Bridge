# 独立运行环境

首次安装按 [安装与使用](../INSTALL.md) 操作。准备工具将运行所需的组件集中到自选目录，无需安装 WeChatAgent 桌面程序。

| 组件 | 获取方式 |
| --- | --- |
| Windows x64、64 位 Python 3.12 | 本机环境 |
| reader、游标补丁和账号连接脚本 | 插件仓库和发布包 |
| SQLCipher 和媒体依赖 | `prepare` 按 [依赖清单](reader-requirements.txt) 安装到独立 `.venv` |
| 微信 4.1.10.27、配套 hook 和账号连接组件 | [完整发布包](https://github.com/suodeovo/MaiBot-WeChat-Bridge/releases/tag/v1.4.4) 的 `runtime/` |
| 本机微信登录和数据库凭据 | 使用 `start-client` 登录，再执行 `connect` |

`prepare` 支持 `--wheels <目录>`，可从本地 wheel 目录安装 Python 依赖。源码包与市场安装包含 reader 脚本，首次独立部署还需完整发布包中的二进制组件。

## 常见问题

- **找不到微信数据库**。先确认配套微信已登录。数据目录不在默认位置时，用 `connect --db-path` 指定包含 `db_storage` 的账号目录。
- **等待数据库密钥超时**。根据 `connect` 的提示在配套微信中重新登录。微信、账号连接工具和 MaiBot 应由同一 Windows 用户运行。
- **reader 在线但没有入站消息**。用 `chats --search` 确认会话标识，再用 `chats --allow` 保存接收名单。reader 只读取名单中的会话，首次接收名单为空。回复还受插件自己的回复白名单和唤醒配置控制。
- **端口被占用**。独立环境默认使用 reader `127.0.0.1:5031`、hook `127.0.0.1:30001`。使用前先停止占用同一端口的旧服务。OneBot 的 `7999` 可在插件配置中单独开关。
- **已有运行环境**。可用 `tools/prepare-runtime.py` 接入既有账号配置。全新独立环境应使用新目录。

运行目录下的 `state/` 含本机凭据和桥接状态。凭据绑定当前 Windows 用户，备份时请妥善保管。第三方组件来源见 [许可说明](../THIRD-PARTY-LICENSES.md)。
