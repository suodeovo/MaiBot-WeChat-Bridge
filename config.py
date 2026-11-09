"""插件配置模型：对齐 WeChatAgent 的组件划分，并在 MaiBot WebUI 里可视化编辑。

WeChatAgent 的 `config/wechatagent.json` 分成这些节：

    runtime   python / weflowMode / sender / dshDesktop
    hook      baseUrl / expectedVersion / wechatExecutable /
              requestTimeoutMs / receiptTimeoutMs / maxConcurrentSends
    ports     weflow / onebot / adapter / dshPlugin
    account   nicknames
    wechat    whitelist / wake_mode / wake_words / history / replyLayout /
              stickers / memeLibrary / assistant / proactive / search ...

本插件只托管「收发链路」这一段，所以挑的是与它直接相关的部分：

    plugin   插件总开关、配置版本
    project  WeChatAgent 项目根、python 解释器
    reader   reader-api 地址、托管开关、自愈
    hook     微信 hook 地址、期望版本、超时
    server   本插件的 OneBot 服务端（MaiBot 连这里）
    wechat   白名单、唤醒模式、唤醒词、回复布局（对齐 WeChatAgent）
    limits   并发与节流（对齐 WeChatAgent 的 social 节）

字段命名和语义尽量沿用 WeChatAgent，便于对照它的界面。
"""
from __future__ import annotations

from typing import Any, ClassVar

from maibot_sdk import Field, PluginConfigBase

SUPPORTED_CONFIG_VERSION = '1.0.0'
EXPECTED_WECHAT_VERSION = '4.1.10.27'


def _meta(label: str, order: int, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {'label': label, 'order': order}
    data.update(extra)
    return data


def _i18n(label_en: str, label_zh: str, hint_en: str = '', hint_zh: str = '') -> dict[str, dict[str, str]]:
    en: dict[str, str] = {'label': label_en}
    zh: dict[str, str] = {'label': label_zh}
    if hint_en:
        en['hint'] = hint_en
    if hint_zh:
        zh['hint'] = hint_zh
    return {'en_US': en, 'zh_CN': zh}


# ---------------------------------------------------------------- 各配置节

class PluginOptions(PluginConfigBase):
    """插件总开关。"""

    __ui_label__: ClassVar[str] = '插件设置'
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(
        default=True,
        description='是否启用微信链路托管。',
        json_schema_extra=_meta(
            '启用插件', 0,
            i18n=_i18n('Enable plugin', '启用插件',
                       'Disable to keep the plugin idle.', '关闭后插件保持空闲，不起服务。'),
        ),
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description='配置结构版本，勿手改。',
        json_schema_extra=_meta(
            '配置版本', 99, disabled=True, hidden=True,
            i18n=_i18n('Config version', '配置版本'),
        ),
    )
    auto_start: bool = Field(
        default=True,
        description='MaiBot 启动时自动托管 reader 并开始收发。',
        json_schema_extra=_meta(
            '随 MaiBot 自动启动', 1,
            i18n=_i18n('Auto start with MaiBot', '随 MaiBot 自动启动',
                       'Start reader and messaging on plugin load.',
                       '插件加载时即拉起 reader 并开始收发。'),
        ),
    )


class ProjectConfig(PluginConfigBase):
    """reader-api.py 所在的微信桥接运行目录。"""

    __ui_label__: ClassVar[str] = '项目与运行时'
    __ui_order__: ClassVar[int] = 1

    root: str = Field(
        default=r'C:\path\to\wechat-runtime',
        description='微信桥接运行目录，其下应有 scripts/reader-api.py。',
        json_schema_extra=_meta(
            '项目根目录', 0,
            i18n=_i18n('Project root', '项目根目录',
                       'WeChat bridge runtime directory containing scripts/reader-api.py.',
                       '微信桥接运行目录，scripts/reader-api.py 应在其下。'),
        ),
    )
    python: str = Field(
        default=r'C:\path\to\python\python.exe',
        description='运行 reader-api.py 的 Python 解释器。',
        json_schema_extra=_meta(
            'Python 解释器', 1,
            i18n=_i18n('Python interpreter', 'Python 解释器'),
        ),
    )
    media_ttl_hours: float = Field(
        default=48.0,
        description='出站媒体暂存保留时长（小时），超期清理。',
        json_schema_extra=_meta(
            '媒体保留时长（小时）', 2,
            i18n=_i18n('Media retention (hours)', '媒体保留时长（小时）'),
        ),
    )
    media_max_mb: float = Field(
        default=512.0,
        description='出站媒体暂存总量上限（MB），超限从最旧清理。',
        json_schema_extra=_meta(
            '媒体总量上限（MB）', 3,
            i18n=_i18n('Media storage cap (MB)', '媒体总量上限（MB）'),
        ),
    )
    media_workers: int = Field(
        default=4,
        description='慢媒体/转码工作线程并发上限。',
        json_schema_extra=_meta(
            '媒体工作线程', 4,
            i18n=_i18n('Media workers', '媒体工作线程'),
        ),
    )
    pilk_paths: str = Field(
        default=r'C:\path\to\pilk\site-packages',
        description='pilk（SILK 编码）搜索路径，分号分隔；为空则只常规 import。',
        json_schema_extra=_meta(
            'pilk 路径', 5,
            i18n=_i18n('pilk search paths', 'pilk 路径',
                       'Semicolon-separated extra search paths for pilk.',
                       'pilk 额外搜索路径，分号分隔。'),
        ),
    )
    crypto_paths: str = Field(
        default=r'C:\path\to\crypto\site-packages',
        description='pycryptodome 搜索路径，分号分隔（图片本地降级解码用）。',
        json_schema_extra=_meta(
            'Crypto 路径', 6,
            i18n=_i18n('Crypto search paths', 'Crypto 路径'),
        ),
    )
    media_scripts_dir: str = Field(
        default=r'C:\path\to\wechatagent\scripts',
        description='wechat_media 等脚本目录（图片本地降级用）。',
        json_schema_extra=_meta(
            '媒体脚本目录', 7,
            i18n=_i18n('Media scripts dir', '媒体脚本目录'),
        ),
    )


class ReaderConfig(PluginConfigBase):
    """reader-api：读微信数据库并 SSE 推送。"""

    __ui_label__: ClassVar[str] = '微信读取服务'
    __ui_order__: ClassVar[int] = 2

    base_url: str = Field(
        default='http://127.0.0.1:5031',
        description='reader-api 的 HTTP 基址。',
        json_schema_extra=_meta(
            '读取服务地址', 0,
            i18n=_i18n('Reader base URL', '读取服务地址'),
        ),
    )
    autostart: bool = Field(
        default=True,
        description='由本插件拉起 reader-api（否则假定你已手动启动）。',
        json_schema_extra=_meta(
            '由插件托管启动', 1,
            i18n=_i18n('Managed by plugin', '由插件托管启动',
                       'Let the plugin spawn reader-api and restart it on crash.',
                       '由插件启动 reader-api，崩溃时自动重启。'),
        ),
    )
    stop_on_unload: bool = Field(
        default=True,
        description='插件卸载（或 MaiBot 退出）时停掉 reader-api。',
        json_schema_extra=_meta(
            '卸载时停止', 2,
            i18n=_i18n('Stop on unload', '卸载时停止'),
        ),
    )
    watchdog: bool = Field(
        default=True,
        description='守护循环：reader 掉了自动拉起。',
        json_schema_extra=_meta(
            '崩溃自愈', 3,
            i18n=_i18n('Auto restart', '崩溃自愈',
                       'Restart reader automatically when its port stops answering.',
                       'reader 端口不通时自动重新拉起。'),
        ),
    )
    watchdog_seconds: float = Field(
        default=15.0,
        description='守护检查间隔（秒）。',
        json_schema_extra=_meta(
            '检查间隔（秒）', 4,
            i18n=_i18n('Watch interval (s)', '检查间隔（秒）'),
        ),
    )


class HookConfig(PluginConfigBase):
    """微信 hook 客户端：原生发送接口。"""

    __ui_label__: ClassVar[str] = '微信发送组件'
    __ui_order__: ClassVar[int] = 3

    base_url: str = Field(
        default='http://127.0.0.1:30001',
        description='微信 hook 的 HTTP 基址（必须本机回环）。',
        json_schema_extra=_meta(
            'Hook 地址', 0,
            i18n=_i18n('Hook base URL', 'Hook 地址'),
        ),
    )
    expected_version: str = Field(
        default=EXPECTED_WECHAT_VERSION,
        description='要求匹配的微信版本；不一致时 hook 不会就绪。',
        json_schema_extra=_meta(
            '期望微信版本', 1,
            i18n=_i18n('Expected WeChat version', '期望微信版本',
                       'The hook only activates on this exact client version.',
                       'hook 只在这个微信版本上生效。'),
        ),
    )
    wechat_executable: str = Field(
        default='state/hook/wechat-4.1.10.27/Weixin.exe',
        description='带 hook 的微信可执行文件（相对项目根或绝对路径）。',
        json_schema_extra=_meta(
            'Hook 版微信路径', 2,
            i18n=_i18n('Hook WeChat executable', 'Hook 版微信路径',
                       'Used by WeChatAgent when launching the matching client.',
                       'WeChatAgent 启动匹配版本客户端时使用。'),
        ),
    )
    request_timeout_ms: int = Field(
        default=5000,
        description='单次 hook HTTP 请求超时（毫秒）。当前由 hook 端点内部超时承担，插件侧不逐请求应用（保留供对齐）。',
        json_schema_extra=_meta(
            '请求超时（毫秒）', 3,
            i18n=_i18n('Request timeout (ms)', '请求超时（毫秒）',
                       'Enforced by the hook endpoint itself; kept for parity.',
                       '由 hook 端点内部超时承担，插件侧不逐请求应用。'),
        ),
    )
    receipt_timeout_ms: int = Field(
        default=18000,
        description='等待发送回执的超时（毫秒）。当前回执以 reader 数据库为准，此字段暂未逐请求应用（保留供对齐）。',
        json_schema_extra=_meta(
            '回执超时（毫秒）', 4,
            i18n=_i18n('Receipt timeout (ms)', '回执超时（毫秒）',
                       'Receipts come from the reader DB; kept for parity.',
                       '回执以 reader 数据库为准，暂未逐请求应用。'),
        ),
    )
    max_concurrent_sends: int = Field(
        default=4,
        description='并发发送上限。',
        json_schema_extra=_meta(
            '最大并发发送', 5,
            i18n=_i18n('Max concurrent sends', '最大并发发送'),
        ),
    )


class ServerConfig(PluginConfigBase):
    """本插件的 OneBot v11 服务端（给外部工具/调试直连用）。"""

    __ui_label__: ClassVar[str] = 'OneBot 服务端'
    __ui_order__: ClassVar[int] = 5

    host: str = Field(
        default='127.0.0.1',
        description='监听地址，建议保持回环。',
        json_schema_extra=_meta(
            '监听地址', 0,
            i18n=_i18n('Listen host', '监听地址'),
        ),
    )
    port: int = Field(
        default=7999,
        description='监听端口。',
        json_schema_extra=_meta(
            '监听端口', 1,
            i18n=_i18n('Listen port', '监听端口'),
        ),
    )
    token: str = Field(
        default='',
        description='鉴权 token；留空则不校验。',
        json_schema_extra=_meta(
            '鉴权 token', 2,
            i18n=_i18n('Access token', '鉴权 token',
                       'Leave empty to disable authentication.',
                       '留空表示不校验连接方。'),
        ),
    )
    enable: bool = Field(
        default=True,
        description='是否开启这个服务端（消息投递到 MaiBot 不依赖它）。',
        json_schema_extra=_meta(
            '开启 OneBot 服务端', 3,
            i18n=_i18n('Enable OneBot server', '开启 OneBot 服务端',
                       'Only needed for external OneBot clients; MaiBot uses the gateway directly.',
                       '仅外部 OneBot 客户端需要；MaiBot 走网关不受影响。'),
        ),
    )


class WechatPolicyConfig(PluginConfigBase):
    """消息过滤与唤醒策略（对齐 WeChatAgent 的 wechat 节）。"""

    __ui_label__: ClassVar[str] = '收发策略'
    __ui_order__: ClassVar[int] = 6

    whitelist_groups: str = Field(
        default='',
        description='允许收发的群（OneBot 数字 ID 或群名，逗号分隔）；留空表示全部允许。',
        json_schema_extra=_meta(
            '群白名单', 0,
            i18n=_i18n('Group whitelist', '群白名单',
                       'Comma-separated group IDs or names; empty allows all.',
                       '逗号分隔；留空表示全部允许。'),
        ),
    )
    whitelist_private: str = Field(
        default='',
        description='允许收发的私聊（wxid 或昵称，逗号分隔）；留空表示全部允许。',
        json_schema_extra=_meta(
            '私聊白名单', 1,
            i18n=_i18n('Private whitelist', '私聊白名单'),
        ),
    )
    wake_mode: str = Field(
        default='hybrid',
        description='hybrid = 自然参与；mention = 仅被 @ 或唤醒词时回复。',
        json_schema_extra=_meta(
            '唤醒模式', 2,
            i18n=_i18n('Wake mode', '唤醒模式',
                       'hybrid participates naturally; mention only replies when addressed.',
                       'hybrid 自然参与；mention 仅在 @ 或唤醒词时回复。'),
        ),
    )
    wake_words: str = Field(
        default='小柚',
        description='唤醒词，逗号分隔。',
        json_schema_extra=_meta(
            '唤醒词', 3,
            i18n=_i18n('Wake words', '唤醒词'),
        ),
    )
    max_reply_messages: int = Field(
        default=2,
        description='一轮回复最多拆成几条消息。注意：拆条由 MaiBot 宿主决定，此字段当前无插件侧消费点（保留供对齐 WeChatAgent）。',
        json_schema_extra=_meta(
            '单轮最多消息数', 4,
            i18n=_i18n('Max messages per turn', '单轮最多消息数',
                       'Splitting is decided by the MaiBot host; no plugin-side effect yet.',
                       '拆条由宿主决定，插件侧暂不生效。'),
        ),
    )
    chat_modes: str = Field(
        default='',
        description='按群覆盖唤醒模式（N2）。格式：群ID或群名=hybrid|mention，'
                    '逗号分隔；例：123456789=mention,洛克王国摸鱼群=hybrid。'
                    '未列出的群用上面的全局唤醒模式。',
        json_schema_extra=_meta(
            '分群唤醒模式', 5,
            i18n=_i18n(
                'Per-chat wake modes', '分群唤醒模式',
                'Overrides the global wake mode per group: id=name=hybrid|mention, '
                'comma separated.',
                '按群覆盖唤醒模式；未列出的群用全局模式。'),
        ),
    )
    disabled_chats: str = Field(
        default='',
        description='完全停用的群/私聊（N3 联动）。逗号分隔群ID/群名/wxid；'
                    '命中的会话既不收也不回。',
        json_schema_extra=_meta(
            '停用会话', 6,
            i18n=_i18n(
                'Disabled chats', '停用会话',
                'Chats that neither receive nor reply.',
                '命中的会话不收不回。'),
        ),
    )
    administrators: str = Field(
        default='',
        description='插件管理员的微信 ID / 群名片（逗号分隔）。留空则 /wx 管理命令任何人可用'
                    '（仅建议私人 bot 这样配）。',
        json_schema_extra=_meta(
            '插件管理员', 8,
            i18n=_i18n(
                'Plugin admins', '插件管理员',
                'Comma-separated wxids or nicknames allowed to run /wx admin commands.',
                '可执行 /wx 管理命令的微信 ID，逗号分隔。'),
        ),
    )
    backfill_count: int = Field(
        default=0,
        description='插件启动时从 reader 回填每个白名单会话最近 N 条消息到'
                    '麦麦的历史上下文（is_notify，不触发回复）。0 = 关闭。',
        json_schema_extra=_meta(
            '历史回填条数', 7,
            i18n=_i18n(
                'History backfill count', '历史回填条数',
                'On startup, feed the last N messages per whitelisted chat into '
                'history context (notify-only, no reply). 0 disables.',
                '启动时回填每个白名单会话最近 N 条进历史；0 关闭。'),
        ),
    )


class LimitsConfig(PluginConfigBase):
    """并发与节流（对齐 WeChatAgent 的 social 节）。"""

    __ui_label__: ClassVar[str] = '节流限制'
    __ui_order__: ClassVar[int] = 7

    batch_ms: int = Field(
        default=3000,
        description='消息聚批窗口（毫秒）。注意：聚批由宿主消息防抖承担，此字段当前无插件侧消费点（保留供对齐）。',
        json_schema_extra=_meta(
            '聚批窗口（毫秒）', 0,
            i18n=_i18n('Batch window (ms)', '聚批窗口（毫秒）',
                       'Batching is handled by the host debouncer; no plugin-side effect yet.',
                       '聚批由宿主防抖承担，插件侧暂不生效。'),
        ),
    )
    min_interval_ms: int = Field(
        default=15000,
        description='群聊中两次发送的最小间隔（毫秒）。',
        json_schema_extra=_meta(
            '群聊最小间隔（毫秒）', 1,
            i18n=_i18n('Min interval in groups (ms)', '群聊最小间隔（毫秒）'),
        ),
    )
    private_min_interval_ms: int = Field(
        default=1000,
        description='私聊中两次发送的最小间隔（毫秒）。',
        json_schema_extra=_meta(
            '私聊最小间隔（毫秒）', 2,
            i18n=_i18n('Min interval in private (ms)', '私聊最小间隔（毫秒）'),
        ),
    )
    max_per_minute: int = Field(
        default=20,
        description='每分钟发送上限。',
        json_schema_extra=_meta(
            '每分钟上限', 3,
            i18n=_i18n('Max per minute', '每分钟上限'),
        ),
    )
    max_per_ten_minutes: int = Field(
        default=100,
        description='每十分钟发送上限。',
        json_schema_extra=_meta(
            '每十分钟上限', 4,
            i18n=_i18n('Max per 10 minutes', '每十分钟上限'),
        ),
    )
    round_window_ms: int = Field(
        default=30000,
        description='一轮回复的最长持续时间（毫秒）。TTS 合成一条要几秒，'
                    '三段回复可能横跨 20 秒以上，所以这个值要留足。',
        json_schema_extra=_meta(
            '单轮最长时长（毫秒）', 5,
            i18n=_i18n(
                'Max turn duration (ms)', '单轮最长时长（毫秒）',
                'Upper bound on how long one reply turn may span.',
                '一轮回复最长持续多久；TTS 合成慢，要留足。',
            ),
        ),
    )
    turn_quiet_ms: int = Field(
        default=10000,
        description='与上一条间隔超过该值就视为新一轮（毫秒）。',
        json_schema_extra=_meta(
            '同轮停顿阈值（毫秒）', 6,
            i18n=_i18n(
                'Turn quiet gap (ms)', '同轮停顿阈值（毫秒）',
                'A gap longer than this starts a new turn.',
                '与上一条间隔超过该值即视为新一轮。',
            ),
        ),
    )
    round_min_gap_ms: int = Field(
        default=200,
        description='同一轮内两条消息之间的最小物理间隔（毫秒）。',
        json_schema_extra=_meta(
            '同轮最小间隔（毫秒）', 7,
            i18n=_i18n('Min gap within a turn (ms)', '同轮最小间隔（毫秒）'),
        ),
    )
    max_wait_ms: int = Field(
        default=8000,
        description='为满足发送间隔最多愿意等多久（毫秒）。超过这个值直接拒绝，'
                    '避免把发送队列堵死。',
        json_schema_extra=_meta(
            '最大等待（毫秒）', 8,
            i18n=_i18n(
                'Max throttle wait (ms)', '最大等待（毫秒）',
                'Wait at most this long to satisfy the send interval; '
                'longer waits reject instead of blocking the queue.',
                '为满足发送间隔最多等这么久，超过则直接拒绝，避免堵死队列。',
            ),
        ),
    )


class LauncherConfig(PluginConfigBase):
    """按版本拉起微信（按钮式操作）。

    MaiBot 的配置页不渲染任意按钮，所以这里用一个「动作」下拉框：
    把它切到 `open` 并保存，插件就会按下面指定的版本路径拉起微信；
    插件执行完会把该字段复位成 `idle`，不会反复触发。
    """

    __ui_label__: ClassVar[str] = '打开微信'
    __ui_order__: ClassVar[int] = 4

    action: str = Field(
        default='idle',
        description='idle = 不动作；open = 按下方路径打开对应版本微信并等待 hook 就绪。',
        json_schema_extra=_meta(
            '执行动作', 0,
            i18n=_i18n(
                'Action', '执行动作',
                'Set to open then save to launch WeChat of the configured version; '
                'the field resets to idle afterwards.',
                '选 open 并保存即拉起对应版本微信；执行完自动复位为 idle。',
            ),
        ),
    )
    exe: str = Field(
        default='state/hook/wechat-4.1.10.27/Weixin.exe',
        description='要启动的微信可执行文件（相对项目根或绝对路径）。',
        json_schema_extra=_meta(
            '微信程序路径', 1,
            i18n=_i18n('WeChat executable', '微信程序路径',
                       'Relative to the project root, or an absolute path.',
                       '相对项目根，或直接写绝对路径。'),
        ),
    )
    wait_seconds: float = Field(
        default=40.0,
        description='等待 hook 就绪的最长秒数。',
        json_schema_extra=_meta(
            '等待就绪（秒）', 2,
            i18n=_i18n('Wait for ready (s)', '等待就绪（秒）'),
        ),
    )
    allow_other_versions: bool = Field(
        default=False,
        description='允许多个微信版本同时运行（共用登录态，通常不建议）。',
        json_schema_extra=_meta(
            '允许多版本并存', 3,
            i18n=_i18n('Allow multiple versions', '允许多版本并存',
                       'Two clients sharing the same login state usually conflict.',
                       '两个版本共用登录态，通常会互相干扰。'),
        ),
    )
    last_result: str = Field(
        default='',
        description='上一次启动结果（插件写入，供排查）。',
        json_schema_extra=_meta(
            '上次结果', 9, disabled=True,
            i18n=_i18n('Last result', '上次结果'),
        ),
    )


# ---------------------------------------------------------------- 语音合成

class TtsConfig(PluginConfigBase):
    """语音合成：用 MaiBot 的豆包语音合成 2.0 插件把文本转成微信语音。

    链路：文本 → volc_tts 的 public API「synthesize」→ PCM/WAV
         → pilk 编码 SILK（\\x02#!SILK_V3）→ hook /WeChatAgent/voice-send
    """

    __ui_label__: ClassVar[str] = '语音合成'
    __ui_order__: ClassVar[int] = 8

    enabled: bool = Field(
        default=True,
        description='收到纯文本回复时，是否合成语音发出。',
        json_schema_extra=_meta(
            '启用语音合成', 0,
            i18n=_i18n('Enable TTS', '启用语音合成',
                       'Synthesize replies as WeChat voice via the Doubao TTS plugin.',
                       '用豆包语音合成插件把回复转成微信语音。'),
        ),
    )
    mode: str = Field(
        default='text_only',
        description='text_only 只发文字（语音交给模型按需调用）；'
                    'on_request 用户明确要求时才发语音；'
                    'both 每条都发文字+语音；voice_only 只发语音；'
                    'voice_if_short 短文本发语音。',
        json_schema_extra=_meta(
            '发送模式', 1,
            i18n=_i18n(
                'Send mode', '发送模式',
                'text_only = text only (voice on demand); '
                'on_request = voice only when the user asks; '
                'both = always text+voice; voice_only = always voice; '
                'voice_if_short = short replies as voice',
                'text_only 只发文字，语音由模型按需触发；'
                'on_request 用户明确要求时才发语音；'
                'both 每条都发文字和语音；voice_only 只发语音；'
                'voice_if_short 短文本发语音。'),
        ),
    )
    voice_fallback_text: bool = Field(
        default=True,
        description='语音没发成功（合成失败/被拒/限流）时，自动补发文字，'
                    '避免整条回复消失。',
        json_schema_extra=_meta(
            '语音失败补文字', 2,
            i18n=_i18n(
                'Text fallback on voice failure', '语音失败补文字',
                'Send the text when voice synthesis or delivery fails.',
                '语音没发成功时自动补发文字，避免整条回复消失。',
            ),
        ),
    )
    request_triggers: str = Field(
        default='发语音,语音说,说语音,念出来,念一下,读一下,读给我,'
                '说给我听,用声音,语音回复,发条语音,来段语音',
        description='on_request 模式下，命中这些词才把回复转成语音。逗号分隔。',
        json_schema_extra=_meta(
            '语音请求词', 3,
            i18n=_i18n(
                'Voice request triggers', '语音请求词',
                'Comma-separated keywords; matching one turns the reply into voice '
                'in on_request mode.',
                'on_request 模式下命中这些词才转语音，逗号分隔。'),
        ),
    )
    request_negative: str = Field(
        default='别发语音,不要语音,别念,不用语音,打字就行,别语音',
        description='命中这些词就不转语音（优先级高于请求词）。逗号分隔。',
        json_schema_extra=_meta(
            '语音否定词', 4,
            i18n=_i18n(
                'Voice negative keywords', '语音否定词',
                'Matching one of these suppresses voice even if a trigger matched.',
                '命中这些词就不转语音，优先级高于请求词。'),
        ),
    )
    request_window_s: float = Field(
        default=120.0,
        description='「用户要求语音」的有效期（秒）。超过这么久之前的请求不再生效。',
        json_schema_extra=_meta(
            '请求有效期（秒）', 5,
            i18n=_i18n(
                'Request validity (s)', '请求有效期（秒）',
                'How long a voice request stays in effect.',
                '用户要求语音多久内有效。'),
        ),
    )
    keep_text: bool = Field(
        default=False,
        description='on_request 模式下，发语音的同时是否也发文字。',
        json_schema_extra=_meta(
            '语音同时发文字', 6,
            i18n=_i18n('Also send text', '语音同时发文字',
                       'Send the text too when the reply goes out as voice.',
                       '转语音时是否同时发一遍文字。'),
        ),
    )
    max_chars: int = Field(
        default=120,
        description='超过这个长度不合成语音。',
        json_schema_extra=_meta(
            '最长合成字数', 8,
            i18n=_i18n('Max chars for TTS', '最长合成字数',
                       'Longer replies stay as text.', '更长的回复保持文字。'),
        ),
    )
    short_chars: int = Field(
        default=60,
        description='voice_if_short 模式下，短于该字数才发语音。',
        json_schema_extra=_meta(
            '短文本阈值', 9,
            i18n=_i18n('Short threshold', '短文本阈值'),
        ),
    )
    speaker: str = Field(
        default='',
        description='音色 ID，留空用 volc_tts 插件配置的音色。',
        json_schema_extra=_meta(
            '音色 ID', 10,
            i18n=_i18n('Speaker', '音色 ID',
                       'Empty means use the TTS plugin default voice.',
                       '留空表示用 TTS 插件自己配的音色。'),
        ),
    )
    sample_rate: int = Field(
        default=24000,
        description='PCM 采样率，需与 volc_tts 的 tts.sample_rate 一致。',
        json_schema_extra=_meta(
            '采样率', 11,
            i18n=_i18n('Sample rate', '采样率'),
        ),
    )
    retry_budget_ms: int = Field(
        default=12000,
        description='语音发送遇到 429 限流时的重试总预算（毫秒）。',
        json_schema_extra=_meta(
            '语音重试预算（毫秒）', 12,
            i18n=_i18n(
                'Voice retry budget (ms)', '语音重试预算（毫秒）',
                'Total budget for retrying voice sends that hit HTTP 429.',
                '语音发送遇到 429 限流时的重试总预算。',
            ),
        ),
    )


# ---------------------------------------------------------------- 总配置

class WxSupervisorSettings(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginOptions = Field(default_factory=PluginOptions)
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    reader: ReaderConfig = Field(default_factory=ReaderConfig)
    hook: HookConfig = Field(default_factory=HookConfig)
    launcher: LauncherConfig = Field(default_factory=LauncherConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    wechat: WechatPolicyConfig = Field(default_factory=WechatPolicyConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    tts: TtsConfig = Field(default_factory=TtsConfig)

    # 允许 config.toml 顶层直接写 config_version
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description='配置结构版本（顶层写法与 [plugin] 内等价）。',
        json_schema_extra=_meta(
            '配置版本', 98, disabled=True, hidden=True,
            i18n=_i18n('Config version', '配置版本'),
        ),
    )

    # ----------------------------------------------------- 便捷访问

    def enabled(self) -> bool:
        return bool(self.plugin.enabled)

    def whitelist(self) -> tuple[set[str], set[str]]:
        """返回 (群白名单, 私聊白名单)，空集合表示不限制。"""
        def split(raw: str) -> set[str]:
            return {x.strip() for x in str(raw or '').replace('，', ',').split(',') if x.strip()}
        return split(self.wechat.whitelist_groups), split(self.wechat.whitelist_private)

    def wake_word_list(self) -> list[str]:
        return [x.strip() for x in str(self.wechat.wake_words or '')
                .replace('，', ',').split(',') if x.strip()]

    def tts_triggers(self) -> list[str]:
        """语音请求词（中英文逗号都可作分隔）。"""
        return [x.strip() for x in str(self.tts.request_triggers or '')
                .replace('，', ',').split(',') if x.strip()]

    def tts_negatives(self) -> list[str]:
        """语音否定词：命中就绝不转语音。"""
        return [x.strip() for x in str(self.tts.request_negative or '')
                .replace('，', ',').split(',') if x.strip()]

    def chat_mode_overrides(self) -> dict[str, str]:
        """分群唤醒模式（N2）：{群ID或群名: hybrid|mention}。"""
        out: dict[str, str] = {}
        for item in str(self.wechat.chat_modes or '').replace('，', ',').split(','):
            item = item.strip()
            if not item or '=' not in item:
                continue
            chat, _, mode = item.rpartition('=')
            chat = chat.strip()
            mode = mode.strip().lower()
            if chat and mode in ('hybrid', 'mention'):
                out[chat] = mode
        return out

    def admin_set(self) -> set[str]:
        """插件管理员（N3）：空集 = 不限制。"""
        return {x.strip() for x in str(self.wechat.administrators or '')
                .replace('，', ',').split(',') if x.strip()}

    def disabled_chat_set(self) -> set[str]:
        """停用会话（N3 联动）：命中的不收不回。"""
        return {x.strip() for x in str(self.wechat.disabled_chats or '')
                .replace('，', ',').split(',') if x.strip()}

    def validate_runtime(self, logger: Any) -> bool:
        """启动前自检，返回 False 表示配置不可用。"""
        ver = self.config_version or self.plugin.config_version
        if ver != SUPPORTED_CONFIG_VERSION:
            logger.error('配置版本不兼容: 当前 %s，要求 %s',
                         ver, SUPPORTED_CONFIG_VERSION)
            return False
        if not self.project.root:
            logger.error('project.root 为空')
            return False
        if not self.project.python:
            logger.error('project.python 为空')
            return False
        if not str(self.reader.base_url).startswith('http://127.0.0.1'):
            logger.error('reader.base_url 必须是本机回环 http 地址')
            return False
        if not str(self.hook.base_url).startswith('http://127.0.0.1'):
            logger.error('hook.base_url 必须是本机回环 http 地址')
            return False
        if self.server.port < 1024 or self.server.port > 65535:
            logger.error('server.port 非法: %s', self.server.port)
            return False
        if self.wechat.wake_mode not in ('hybrid', 'mention'):
            logger.error('wake_mode 只能是 hybrid 或 mention')
            return False
        return True


__all__ = [
    'PluginOptions', 'ProjectConfig', 'ReaderConfig', 'HookConfig',
    'LauncherConfig', 'ServerConfig', 'WechatPolicyConfig', 'LimitsConfig',
    'TtsConfig',
    'WxSupervisorSettings', 'SUPPORTED_CONFIG_VERSION', 'EXPECTED_WECHAT_VERSION',
]
