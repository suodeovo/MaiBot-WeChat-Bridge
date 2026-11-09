# 安装与使用

## 1. 安装 MaiBot 和插件

- 用官方启动器安装并启动 MaiBot 1.4.3。在 WebUI 选择要接入微信的 Bot，配置模型供应商和聊天模型。[MaiBot OneKey 下载](https://github.com/Mai-with-u/MaiBotOneKey/releases) · [MaiBot 官方部署教程](https://docs.maibot.chat/manual/)
- 在该 Bot 的插件页安装微信桥接，或把发布包里的 `local_wxsupervisor-1.4.4.zip` 解压到该 Bot 的 `plugins/`。MaiBot 1.4.3 新建 Bot 通常使用 `<工作区>/bots/<bot_id>/plugins/`，原地迁移的旧 Bot 沿用原目录。确认 `local_wxsupervisor/` 下直接包含 `plugin.py` 和 `_manifest.json`。[官方实例目录说明](https://github.com/Mai-with-u/MaiBot/blob/v1.4.3/docs/multi-bot-host.md)
- 先保持插件停用。在已安装的 `local_wxsupervisor/` 目录打开 PowerShell，执行以下命令。

## 2. 准备独立微信环境

需要 Windows x64 和 64 位 Python 3.12。下载并解压 `maibot-wechat-bridge-1.4.4-with-wechat-4.1.10.27.zip`，将其中的 `runtime` 目录填入下面的 `$assets`。准备工具创建独立 Python 环境，安装 reader 依赖，并复制配套微信和 hook。 [下载本版发布包](https://github.com/suodeovo/MaiBot-WeChat-Bridge/releases/tag/v1.4.4) · [下载 Python](https://www.python.org/downloads/windows/)

```powershell
$wxRuntime = 'C:\MaiBot-WeChat-Runtime'
$assets = 'C:\Downloads\maibot-wechat-bridge-1.4.4\runtime'
py -3.12 tools/setup-standalone.py --project-root $wxRuntime prepare --assets $assets
```

首次安装使用空运行目录。准备完成后可移走下载包的解压副本。Python 包装入运行目录的 `.venv`，与 MaiBot 的依赖分开。

## 3. 登录并连接账号

从托盘退出其他微信客户端，再执行以下命令。请在打开的配套微信中登录自己的账号。

```powershell
py -3.12 tools/setup-standalone.py --project-root $wxRuntime start-client
```

确认微信已登录并生成账号数据库后，运行连接命令。若工具提示重新登录，请在微信中退出账号后重新登录。检测到多个账号时，用 `--db-path "实际微信账号数据目录"` 指定包含 `db_storage` 的账号目录。

```powershell
$wxPython = Join-Path $wxRuntime '.venv\Scripts\python.exe'
& $wxPython tools/setup-standalone.py --project-root $wxRuntime connect
```

工具验证本机数据库、用 Windows 当前用户保护读取凭据，并填写插件连接路径。请用同一个 Windows 用户运行 MaiBot。

## 4. 选择会话并启用

先搜索会话，再把输出第一列的标识填入 `--allow`。可以重复传入 `--allow`。每次设置会替换 reader 的整份接收名单。

```powershell
& $wxPython tools/setup-standalone.py --project-root $wxRuntime chats --search '测试群'
& $wxPython tools/setup-standalone.py --project-root $wxRuntime chats --allow '12345678@chatroom'
```

在 MaiBot 插件页启用桥接。可在插件页配置独立的回复白名单和唤醒词。保持配套微信登录，并用另一个账号在允许的会话发一条测试消息。日常需要打开微信时可再次运行 `start-client`。

如果手动安装后提示插件依赖缺失，在 **MaiBot 插件使用的 Python 环境**中运行 `python -m pip install -r requirements.txt`。 reader 依赖由第 2 步管理。

## 升级与回退

已经接通的旧环境可以继续使用现有运行目录。暂停插件和旧 reader，备份插件、配置与运行状态，使用下列工具更新游标补丁和插件连接路径。将路径换为你的现有运行目录。回退时恢复匹配的插件、reader 和配置，再启动服务。

```powershell
py -3.12 tools/prepare-runtime.py --project-root 'C:\Existing\data\workspace' --patch-reader --configure
```

相关文件和常见问题见 [独立运行环境说明](runtime/README.md)。
