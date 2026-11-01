# 首次安装所需运行环境

微信桥接插件需要 **WeChatAgent 0.4.1 + 微信 4.1.10.27 + 配套 hook + reader 依赖 + 本机账号配置**。

## 获取必装环境

- **完整包**：已带 `runtime/installers/WeChatAgent-Setup-0.4.1-x64.exe`，打开该安装器，选择自己的安装目录。
- **源码或市场安装**：在插件目录运行 `powershell -NoProfile -ExecutionPolicy Bypass -File tools/download-runtime.ps1`，脚本下载固定版本并核对 SHA-256，不自动启动安装器。
- 也可直接从 [WeChatAgent 0.4.1 发布页](https://github.com/sourcepro5/WeChatAgent/releases/tag/v0.4.1) 获取同名安装器。来源项目：[sourcepro5/WeChatAgent](https://github.com/sourcepro5/WeChatAgent)。

安装器大小约 503 MiB，SHA-256 为：

```text
9acf2635a6b6c2beed2ac3a3791bd66eea00470051b2809a602908fa3de6882c
```

该版本的运行包提供 reader 所需脚本、NT 读取组件、独立 Python 及 SQLCipher/图片处理依赖。官方校验记录中的内置 Python 为 3.13.16，与 MaiBot 使用的 Python 分开；应让插件使用安装器配置的 reader 解释器。

本插件仍以微信 **4.1.10.27** 为配套版本；其他 WeChatAgent/微信版本没有在本次检查中验证，不能直接替换为最新版。

## 完成本机连接

1. 在 WeChatAgent 中打开匹配的 4.1.10.27 客户端，登录自己的账号。
2. 在其微信连接设置中选择本机账号目录，完成数据库连接与验证。此过程在目标机器生成私有配置和凭据。
3. 找到安装目录下的 `data/workspace`。其中应有 `scripts/reader-api.py`、`scripts/wechat_*.py`、`components/weflow-cli/scripts/nt_decrypt.py` 等文件。
4. 暂停 WeChatAgent 的聊天/reader 服务，保留已登录微信，交由 MaiBot 插件托管 reader，避免两个程序重复启动服务或回复。

接入 MaiBot 只需要微信/reader 链路，无需启用 WeChatAgent 的 DSH 聊天功能。

## 部署补丁并填写插件路径

先把插件装入 MaiBot 的 `plugins/local_wxsupervisor/` 并保持停用。进入该插件目录，运行：

```powershell
python tools/prepare-runtime.py --project-root "C:\WeChatAgent\data\workspace" --patch-reader --configure
```

将路径替换为实际位置；若原配置未保存可用解释器，可额外提供 `--python "实际的内置python.exe路径"`。

工具先检查必需文件、独立 Python 依赖及账号配置文件；通过后才备份并更新 reader 和插件配置。备份位于当前 Windows 用户的 `MaiBotBridgeBackups` 目录，路径会显示在结果中。仅运行检查时省略 `--patch-reader --configure`，此时不修改文件。

工具不会登录账号、输出凭据、读取消息或停止其他程序。操作前应按上述步骤暂停相关服务。

## 完整包中的独立客户端

为保留原分发方式，完整包同时提供：

```text
runtime/
├── installers/WeChatAgent-Setup-0.4.1-x64.exe
├── native/build.json
├── native/version.dll
└── wechat-4.1.10.27/Weixin.exe
```

已有匹配的 WeChatAgent 实例可复用其客户端，不必重复覆盖。需要使用独立客户端时，将整个客户端目录放入该实例的 `state/hook/wechat-4.1.10.27/`，native 文件放入 `state/hook/native/`，通过实例自己的启动/配置流程生成和同步本机 token。

## 依赖不完整时

优先修复或重新安装匹配的 WeChatAgent 内置运行环境。自行维护 reader 解释器时，所需包见 [reader-requirements.txt](reader-requirements.txt)；Windows 上 SQLCipher 含原生依赖，单独执行 pip 安装不一定能替代内置环境。

源码/市场包包含下载和配置工具，大型安装器与客户端通过完整 Release 包或上游发布页获取。任何包都不包含打包机器的 token、账号登录状态、聊天数据库或生产配置。第三方程序的原始版权与许可保持适用。
