# MaiBot WeChat Bridge

[项目主页](https://github.com/suodeovo/MaiBot-WeChat-Bridge) · [下载发布包](https://github.com/suodeovo/MaiBot-WeChat-Bridge/releases) · [问题反馈](https://github.com/suodeovo/MaiBot-WeChat-Bridge/issues)

MaiBot 的非官方微信桥接插件，通过配套微信 hook 发送消息，通过 reader SSE 接收消息。当前处于测试阶段。

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
发送：MaiBot → 本插件 → 微信 hook → 微信
```

MaiBot 主链路使用插件网关。OneBot WebSocket 服务供外部客户端连接，可通过 `server.enable` 单独开关。

## 运行环境

插件 **1.4.4** 声明兼容 MaiBot **1.3.16–1.4.3**。Windows 首次安装按 [使用说明](INSTALL.md) 下载 MaiBot、安装插件、准备微信环境和连接账号。

| 组件 | 要求 |
| --- | --- |
| 系统 | Windows x64 |
| MaiBot | 1.3.16–1.4.3，推荐 1.4.3 |
| maibot-plugin-sdk | 2.16.0–2.22.1，由 MaiBot 管理 |
| reader Python | 64 位 Python 3.12，准备工具创建独立虚拟环境 |
| 微信客户端 | 4.1.10.27，完整包提供匹配客户端、hook 和账号连接组件 |

微信桥接无需安装 WeChatAgent 桌面程序。完整包保留桥接所需的运行组件，首次连接需要在本机登录微信。 [独立运行环境](runtime/README.md) · [版本兼容说明](docs/COMPATIBILITY.md)

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

基础消息收发无需安装语音合成插件。reader 必需 `sqlcipher3`、`cryptography`、`zstandard`、`pycryptodome`、`Pillow` 和 `imageio-ffmpeg`，由准备工具按 [依赖清单](runtime/reader-requirements.txt) 安装到 reader 的独立环境。

## 安装

1. 下载并启动 MaiBot，选择目标 Bot，安装插件并暂时停用。 [MaiBot OneKey 下载](https://github.com/Mai-with-u/MaiBotOneKey/releases)
2. 解压完整发布包，运行 `tools/setup-standalone.py` 的 `prepare` 命令。
3. 用 `start-client` 打开配套微信，登录后用 `connect` 连接账号，再用 `chats` 选择接收会话。
4. 启用插件，在已选会话里测试收发。 [完整安装步骤](INSTALL.md)

## 配置

完整字段见 [配置示例](config.toml.example)。首次使用优先核对以下设置：

| 配置项 | 说明 |
| --- | --- |
| `project.root` | 微信桥接运行目录，包含 reader 和本机账号配置 |
| `project.python` | reader 的独立 Python，由准备工具填写 |
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

- **插件包**包含插件、reader 脚本和安装说明，可解压到目标 Bot 的 `plugins/`。
- **源码包**包含源码、reader 补丁、文档和测试。
- **完整包**另附微信 4.1.10.27、匹配 hook 和首次账号连接组件，供新环境安装。

账号凭据和聊天数据均在目标机器本地生成。

## 来源与许可

- 宿主平台：[MaiBot](https://github.com/Mai-with-u/MaiBot)
- 插件 SDK：[maibot-plugin-sdk](https://github.com/Mai-with-u/maibot-plugin-sdk)

项目采用 **GPL-3.0-or-later**。许可证全文见 [LICENSE](LICENSE)，第三方组件说明见 [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md)。
