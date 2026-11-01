# MaiBot WeChat Bridge

[项目主页](https://github.com/suodeovo/MaiBot-WeChat-Bridge) · [下载发布包](https://github.com/suodeovo/MaiBot-WeChat-Bridge/releases) · [问题反馈](https://github.com/suodeovo/MaiBot-WeChat-Bridge/issues)

MaiBot 的非官方微信桥接插件，通过 WeChatAgent hook 发送消息，通过 reader SSE 接收消息。当前处于测试阶段。

## 功能

| 功能 | 说明 |
| --- | --- |
| 消息桥接 | 文本、图片、表情、文件等消息类型的转换与发送 |
| 连接管理 | reader 托管、状态检查与断线重连 |
| 离线补收 | 根据持久化水位，通过 reader 游标分页补收消息 |
| 接收去重 | 按账号、会话和消息标识识别重复消息 |
| 发送调度 | 并发控制、限流、请求幂等与发送结果状态管理 |
| 语音发送 | 转发兼容音频；文字转语音为可选功能 |

收发路径：

```text
接收：微信消息 → reader（SSE）→ 本插件 → MaiBot
发送：MaiBot → 本插件 → WeChatAgent hook → 微信
```

MaiBot 主链路使用插件网关。OneBot WebSocket 服务供外部客户端连接，可通过 `server.enable` 单独开关。

## 运行环境

当前适配 MaiBot **1.3.19**，插件版本 **1.4.2**。兼容范围为 MaiBot **1.3.16–1.3.19**。

**首次安装必须先准备 WeChatAgent 运行环境。** 插件本身不能替代数据库读取器、微信 hook 和本机账号配置。1.4.2 完整包已加入匹配的 WeChatAgent 安装器，源码/市场安装提供固定版本下载脚本，见 [运行环境安装](runtime/README.md)。

| 组件 | 版本 |
| --- | --- |
| 操作系统 | Windows |
| WeChatAgent | 0.4.1，必装；完整包附安装器，内含 reader 模块和独立 Python 运行环境 |
| MaiBot | 1.3.19；兼容范围 1.3.16–1.3.19 |
| maibot-plugin-sdk | 1.3.19 配套使用 2.20.0–2.21.0；旧环境保留 2.16.0 兼容 |
| Python | MaiBot 使用 3.12.x；reader 使用 WeChatAgent 安装器配置的独立解释器 |
| 微信客户端 | 4.1.10.27，完整包已附带匹配客户端及 hook DLL |

完整包同时附带 WeChatAgent 0.4.1 原始安装器、匹配的微信客户端及 hook 文件，存放于 `runtime/`。市场安装和源码包不携带大型二进制，可运行 `tools/download-runtime.ps1` 下载并校验匹配安装器。

先安装 WeChatAgent，并在其界面完成目标机器的微信登录和数据库连接验证；账号凭据由目标机器生成，不随任何发布包分发。

1.3.19 适配验证覆盖官方清单校验、插件入口加载、组件注册及 SDK 2.20.0 / 2.21.0 隔离回归；不代表已在该版本完成真实微信收发验收。详见 [版本兼容说明](docs/COMPATIBILITY.md)。

### 插件依赖

在运行 MaiBot 插件的 Python 环境中安装：

```powershell
python -m pip install "websockets>=12.0" "pydantic>=2.0"
```

按功能选装：

| 依赖 | 用途 |
| --- | --- |
| 豆包语音合成插件 `volc_tts` | 将文字回复合成为音频 |
| `pilk` | 将 PCM/WAV 音频编码为微信使用的 SILK |
| `pycryptodome` | 图片本地降级解码 |

基础消息收发无需安装语音合成插件。reader 必需 `sqlcipher3`、`cryptography`、`zstandard`、`pycryptodome`、`Pillow` 和 `imageio-ffmpeg`，由匹配安装器的独立 Python 提供，修复清单见 [reader 依赖](runtime/reader-requirements.txt)。这些包应装入 reader 的解释器环境。

## 安装

1. 按 [运行环境安装](runtime/README.md) 安装 WeChatAgent 0.4.1，打开匹配微信 4.1.10.27，在目标机器完成账号连接和数据库验证。
2. 暂停 WeChatAgent 的聊天服务、旧 reader 和桥接插件，保留已登录微信；更新时先备份插件、配置及运行状态。
3. 从插件市场安装，或将仓库内容放入 `MaiBot/plugins/local_wxsupervisor/`；该目录直接包含 `_manifest.json` 和 `plugin.py`。此时先保持插件停用。
4. 在插件目录运行 `python tools/prepare-runtime.py --project-root "C:\WeChatAgent\data\workspace" --patch-reader --configure`，将示例路径替换为实际位置。工具先检查运行环境，再备份并部署 reader 补丁、填写插件路径；默认从 WeChatAgent 配置读取 reader 解释器。
5. 启动或重新加载插件，检查 reader 和 hook 状态，再从外部账号验证真实收发。详细手工步骤见 [INSTALL.md](INSTALL.md)。

reader 补丁提供断线补收所需的游标分页接口；需在 reader 停止时部署，重新启动后生效。缺少必需文件、解释器或账号配置时，托管启动会明确报错，避免仅显示“加载成功”却不能接收。

## 配置

完整字段见 [配置示例](config.toml.example)。首次使用优先核对以下设置：

| 配置项 | 说明 |
| --- | --- |
| `project.root` | 已安装实例的 `data/workspace`，包含全部 reader 模块 |
| `project.python` | WeChatAgent 内置、已安装 reader 依赖的 Python；可由准备工具自动填写 |
| `reader.base_url` | reader 地址，默认 `http://127.0.0.1:5031` |
| `hook.base_url` | hook 地址，默认 `http://127.0.0.1:30001` |
| `wechat.whitelist_groups` | 群聊白名单，留空表示不限制 |
| `wechat.whitelist_private` | 私聊白名单，留空表示不限制 |
| `wechat.wake_mode` | `hybrid` 或 `mention`，控制回复触发方式 |
| `limits` | 发送间隔、额度和等待时间 |
| `server.enable` | 是否启用 OneBot 服务，默认端口为 `7999` |

配置示例中的路径和身份占位符需替换为实际值。可通过配置文件或 MaiBot 插件配置页调整参数。MaiBot 主链路不依赖 OneBot 服务；不连接外部 OneBot 客户端时可关闭 `server.enable`，启用时检查默认 `7999` 端口是否空闲。

## 语音功能

### 转发音频

已有的 SILK 音频可直接走语音发送路径，无需调用语音合成插件。PCM/WAV 音频需要 `pilk` 转码。当前发送路径不支持直接解码 MP3、OGG、FLAC，需先转换为支持的格式。

### 文字转语音

自动合成当前对接豆包插件接口 `local.volc.tts2.synthesize`。其他 TTS 插件需要适配接口与音频格式。

默认 `text_only` 模式不会主动合成语音。不使用自动合成时，在现有 `[tts]` 节中设置：

```toml
[tts]
enabled = false
mode = "text_only"
voice_fallback_text = true
```

该设置关闭自动文字转语音，仍允许转发上游提供的兼容音频段。

语音合成失败时，开启 `voice_fallback_text` 可退回文字。若选择 `voice_only` 并关闭文字兜底，合成失败会导致该条回复无法发送。

## 当前限制

| 项目 | 当前状态 |
| --- | --- |
| 入站语音音频 | 音频获取与解码尚未实现；描述性语音段不包含可用音频，不能用于语音识别 |
| `/wx` 管理命令 | 当前环境中未能正常分发，暂用配置文件或插件配置页调整设置 |
| 历史消息进入模型上下文 | 尚未实现 |
| 线上验证 | 大批量补收、同秒边界、受控重复重放及发送异常等场景仍需实际链路验收 |

## 测试

在仓库根目录、已安装 MaiBot SDK 和基础依赖的 Python 环境中运行：

```powershell
python -m unittest discover -s tests -v
```

测试使用合成数据和替身接口。线上实际收发、补收及异常恢复需要单独验收。

## 升级与回退

升级前暂停相关服务并备份插件、reader、配置和运行状态；按 **reader → 插件** 的顺序更新，保留生产配置，并核对新版本的配置要求。

回退时恢复备份中匹配的插件、reader 和配置，再重新启动服务。

## 发布包

- **插件包**：可直接放入 MaiBot `plugins/` 的 `local_wxsupervisor/` 目录，包含插件及安装说明。
- **源码包**：插件源码、reader 补丁、文档与测试，适合提交到 GitHub 仓库。
- **完整包**：源码、WeChatAgent 0.4.1 原始安装器、微信 4.1.10.27 和配套 hook。运行安装器后再初始化本机账号配置。

完整包不携带本机认证文件、聊天记录或生产配置。安装器和客户端较大，作为 GitHub Release 附件分发。

插件市场安装的是插件源码；其中的下载和准备工具用于完成上述前置安装，不能跳过本机微信登录与账号验证。

## 来源与许可

- 宿主平台：[MaiBot](https://github.com/Mai-with-u/MaiBot)
- 插件 SDK：[maibot-plugin-sdk](https://github.com/Mai-with-u/maibot-plugin-sdk)

项目采用 **GPL-3.0-or-later**。许可证全文见 [LICENSE](LICENSE)，第三方组件说明见 [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md)。
