# 安装、升级与回退

## 安装

1. 准备 MaiBot；按 [运行环境安装](runtime/README.md) 安装必需的 WeChatAgent 0.4.1，完成匹配微信 4.1.10.27 的登录和本机数据库验证。完整包附安装器，源码/市场安装可运行 `tools/download-runtime.ps1` 下载并校验。
2. 若使用含客户端完整包，按 [客户端部署说明](runtime/README.md) 放置微信客户端及 hook 文件，并在目标机器初始化认证。
3. 备份现有 reader；更新已有安装时，先暂停桥接插件和 reader，并备份插件、配置与运行状态。
4. **先 reader，后插件**：以 `patches/reader-api.cursor-patch.py` 替换 WeChatAgent 的 `scripts/reader-api.py`，重新启动 reader。
5. 从插件市场安装，或将仓库全部内容放入 `MaiBot/plugins/local_wxsupervisor/`；该目录下应直接存在 `_manifest.json` 和 `plugin.py`。插件专用 ZIP 已带 `local_wxsupervisor/` 外层目录，可直接解压到 `plugins/`。
6. 可运行 `python tools/prepare-runtime.py --project-root "实际的data/workspace" --patch-reader --configure`，检查必装环境并备份后部署补丁、填写路径。手动配置时从 `config.toml.example` 复制并填写 `project.root` 和 reader 的独立解释器；更新时保留已有配置。
7. 启动或重新加载 MaiBot，检查插件、reader 和 hook 状态。

## 升级

- 1.4.2 补齐 WeChatAgent 安装器与运行环境准备工具；声明兼容 MaiBot 1.3.16–1.3.19，见 [兼容说明](docs/COMPATIBILITY.md)。
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
