# 版本兼容说明

## 1.4.4

MaiBot 和 SDK 声明范围沿用 1.4.3。新增独立 reader 安装流程，完整包移除 WeChatAgent 桌面安装器。详见 [安装说明](../INSTALL.md)。

## 1.4.3

插件声明兼容 MaiBot 1.3.16–1.4.3 和 SDK 2.16.0–2.22.1。MaiBot 1.4.3 的官方依赖要求 SDK 至少为 2.22.1。下方为旧版的验证记录。[MaiBot 1.4.3 依赖清单](https://github.com/Mai-with-u/MaiBot/blob/v1.4.3/pyproject.toml)

## 1.4.2

- 完整包新增固定版本 WeChatAgent 0.4.1 原始安装器，SHA-256 与上游发布记录一致；源码/市场安装新增固定版本下载工具。
- 提供部署 reader 补丁和填写路径的准备工具；本机登录、数据库连接与凭据生成仍由目标机器的 WeChatAgent 完成。
- 托管 reader 前检查完整模块、账号配置文件和独立 Python 依赖；缺少前置环境或 reader 启动失败时明确报告加载失败。
- reader 守护端口从 `reader.base_url` 读取，避免配置端口与托管等待端口不一致。
- 验证范围为隔离安装检查、合成配置部署与回归；没有运行 WeChatAgent 安装器、升级生产实例或发送微信消息。

## 1.4.1

本版本将插件入口调整到仓库根目录，适用于插件市场的源码安装方式。
插件 ID 保持 `local.wxsupervisor`，配置结构版本保持 `1.0.0`。

| 组件 | 声明范围或参考版本 |
| --- | --- |
| MaiBot | 1.3.16–1.3.19 |
| maibot-plugin-sdk | 2.16.0–2.21.0 |
| MaiBot 1.3.19 配套 SDK | 2.20.0 或 2.21.0 |
| Python | 3.12.x |
| 微信客户端 | 4.1.10.27，使用匹配 hook |

MaiBot 1.3.19 的官方依赖要求 SDK 不低于 2.20.0。旧版宿主使用原有配套 SDK，不应为安装本插件单独降级宿主依赖。

官方依据：

- [MaiBot v1.3.19](https://github.com/Mai-with-u/MaiBot/releases/tag/v1.3.19)
- [1.3.19 依赖清单](https://github.com/Mai-with-u/MaiBot/blob/v1.3.19/pyproject.toml)
- [插件市场提交规范](https://github.com/Mai-with-u/plugin-repo/blob/main/CONTRIBUTING.md)

## 验证范围

- SDK 2.16.0、2.20.0、2.21.0 各运行现有 123 项隔离回归，全部通过。中间版本未逐个运行。
- 使用官方 1.3.19 的清单校验器和插件加载器，分别配合 SDK 2.20.0 / 2.21.0，验证干净目录下的插件发现、入口导入、实例创建与生命周期契约。
- 组件声明正常收集：1 个消息网关、5 条管理命令。这仅说明声明有效，不代表 `/wx` 已完成线上分发验证。
- 验证新安装配置写入、重建实例后读回，以及关闭自动启动时的加载/卸载；测试未连接真实微信。
- 使用市场当前官方校验器验证 manifest，无校验错误。

宿主日志、路径与版本探测服务在隔离加载检查中使用替身；没有启动完整 MaiBot Host，也没有升级生产实例。1.3.19 的真实微信收发仍需在目标环境完成验收。

## 安装布局变化

1.4.0 仓库将插件放在 `local_wxsupervisor/` 子目录；1.4.1 改为根目录直接包含 `plugin.py`、`_manifest.json` 和所有插件模块。

- 插件市场或 Git 安装：将仓库作为 `MaiBot/plugins/local_wxsupervisor/`，避免再嵌套一层。
- 插件 ZIP：外层仍是 `local_wxsupervisor/`，解压到 `MaiBot/plugins/` 即可。
- 更新时保留原 `config.toml`，并备份 reader 与运行状态；reader 补丁仍需单独部署。
- manifest 移除 Host 不接受的非标准 `credits` 字段，来源信息保留在第三方说明。

## 未完成能力

本版本没有实现入站语音音频获取/解码或历史回填进入模型上下文；`/wx` 线上分发问题保持已知限制。大批量补收、同秒边界、重复重放和发送异常仍需实际链路验收，隔离测试不能替代这些结论。
