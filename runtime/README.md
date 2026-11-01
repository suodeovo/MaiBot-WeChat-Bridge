# 微信客户端与 hook 运行时

含客户端完整包提供以下文件。GitHub 源码包仅包含本说明，客户端二进制由完整包提供。

```text
runtime/
├── native/
│   ├── build.json
│   └── version.dll
└── wechat-4.1.10.27/
    ├── Weixin.exe
    ├── version.dll
    └── 4.1.10.27/
        └── 微信客户端依赖文件
```

客户端文件版本为 **4.1.10.27**。两个 `version.dll` 为同一份配套 hook；`build.json` 仅保留版本和文件校验信息。

## 放置文件

在目标机器的 WeChatAgent 工作区中：

1. 将 `runtime/wechat-4.1.10.27/` 整个目录复制到 `<WeChatAgent>/state/hook/wechat-4.1.10.27/`，保留内部版本子目录。
2. 将 `runtime/native/` 中的文件放到 `<WeChatAgent>/state/hook/native/`。
3. 将 WeChatAgent 的 hook 客户端版本设置为 `4.1.10.27`，可执行文件路径指向 `state/hook/wechat-4.1.10.27/Weixin.exe`。

已有安装请先退出对应客户端并备份原文件、配置及 hook 元数据，再进行替换。

## 初始化与启动

使用目标 WeChatAgent 的配置初始化流程准备本机认证。配套环境中的 `scripts/project-config.mjs` 会在 hook 模式下生成本机 token；`scripts/launch-hook-wechat.ps1` 会将其同步到客户端目录并启动客户端。

原有认证配置应由目标机器管理。完整包不带打包机器的 `native-token.txt` 或 `wechatagent-hook.token`，也不包含账号登录状态或聊天数据库。

客户端就绪并完成目标账号登录后，再配置 reader 与 MaiBot 插件。完整包提供客户端和 hook 资源，WeChatAgent 的 Node/Python 环境、其余脚本与配置仍需自行准备。

## 校验与来源

完整包根目录的 `FILE-MANIFEST.txt` 包含逐文件 SHA-256。客户端来源为当前部署使用的 Tencent 微信客户端目录；hook 来源为配套 WeChatAgent 构建产物。第三方组件的原有版权与许可声明保持适用，项目许可证不对其重新授权。
