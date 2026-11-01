"""文件与语音发送。

两个接口的约束都是从 hook 实现里逆出来的（`version.dll` 错误串 +
WeChatAgent 的 `file-delivery-worker.mjs` / `voice.mjs` 参考实现）。

## 文件：/SendFileMsg

    POST /SendFileMsg
    {"wxidorgid": "<群id或wxid>", "path": "<绝对路径>",
     "md5": "<32位md5>", "size": <字节数>}
    -> {"ret": 0, "retmsg": "accepted"}

硬性要求（`file-transfers.mjs` 的 `staged()`）：

1. 路径必须是 `state/files/outgoing/<sha256>/<文件名>`
   —— 这是 WeChatAgent 的 staging 布局，hook 按此校验
2. `md5` / `size` 必须与文件真实值一致

只有 `path` 而缺 `md5`/`size` 时 hook 回 `{"ret":1}` 且不带错误详情。

## 语音：/WeChatAgent/voice-send

    POST /WeChatAgent/voice-send
    {"wxidorgid": "<群id或wxid>",
     "path": "<正斜杠路径>",
     "durationMs": <整数毫秒>}
    -> {"ret":0,"retmsg":"accepted_unconfirmed", ...}

要点：

1. 端点**不是** `/SendVoiceMsg`（那是内部路由），对外是 `/WeChatAgent/voice-send`
2. 文件必须是 SILK v3：`\\x02#!SILK_V3` 开头
3. `path` 里的反斜杠要换成正斜杠（参考实现这么传）
4. 必须开总闸 `state/hook/voice-live.enable`（文件存在即可，内容为空）
5. 返回 `accepted_unconfirmed` 是正常的 —— 语音是异步投递，
   真实结果以 reader 数据库回执为准，不能当失败处理
6. 没有精确时长时按 `字节数 × 0.6118` 估算（参考实现的做法）
"""
from __future__ import annotations

from .perf import MediaLeaseRegistry

import hashlib
import logging
import pathlib
import re
import uuid
from typing import Any

log = logging.getLogger('wxsupervisor.media')

FILE_MAX_BYTES = 200 * 1024 * 1024
SILK_MAGIC = b'#!SILK_V3'
SILK_PREFIX = b'\x02'

# SILK 时长估算系数（WeChatAgent voice.mjs 用的值）
SILK_MS_PER_BYTE = 0.6118

# 文件名安全字符（参考 file-transfers.mjs 的 safeFileName 思路）
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str, default: str = 'file.bin') -> str:
    """清理文件名，去掉路径分隔符与非法字符。"""
    base = pathlib.PurePath(str(name or '')).name.strip()
    base = _BAD_NAME.sub('_', base)
    base = base.strip('. ')
    if not base or base in ('.', '..'):
        return default
    return base[:180]


# ---------------------------------------------------------------- 文件

def stage_file(project_root: str | pathlib.Path, raw: bytes,
               filename: str = '') -> tuple[pathlib.Path, str, int]:
    """把文件字节落到 hook 认可的 staging 位置。

    目录必须是 `state/files/outgoing/<sha256>/<name>`，
    返回 (路径, md5, 字节数)。
    """
    if not raw:
        raise ValueError('空文件')
    if len(raw) > FILE_MAX_BYTES:
        raise ValueError(f'文件超过 {FILE_MAX_BYTES // 1024 // 1024}MB')

    root = pathlib.Path(project_root).resolve()
    sha = hashlib.sha256(raw).hexdigest()
    md5 = hashlib.md5(raw).hexdigest()
    name = safe_filename(filename, default=f'{sha[:12]}.bin')

    folder = root / 'state' / 'files' / 'outgoing' / sha
    if folder.exists() and folder.is_symlink():
        raise ValueError('staging 目录是符号链接，拒绝写入')
    folder.mkdir(parents=True, exist_ok=True)
    if not folder.resolve().is_relative_to(root):
        raise ValueError('staging 目录越界')

    target = folder / name
    if target.is_symlink():
        raise ValueError('目标文件是符号链接')
    if target.exists():
        if target.stat().st_size != len(raw):
            raise ValueError('同名文件大小不一致')
    else:
        temp = folder / f'{name}.{uuid.uuid4().hex}.tmp'
        try:
            temp.write_bytes(raw)
            temp.replace(target)
        finally:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass

    MediaLeaseRegistry.shared().acquire(target)   # V2-8：staging 即租约
    return target, md5, len(raw)


def build_file_request(target: str, path: pathlib.Path,
                       md5: str, size: int) -> dict[str, Any]:
    """构造 /SendFileMsg 请求体。md5 与 size 缺一不可。"""
    return {
        'wxidorgid': target,
        'path': str(path),
        'md5': md5,
        'size': int(size),
    }


# ---------------------------------------------------------------- 语音

def is_silk(raw: bytes) -> bool:
    """微信语音要是 SILK v3。

    真实布局是：`0x02` + `#!SILK_V3` + 载荷。
    注意是 1 字节前缀（不是 2），`tencent=True` 编出来的文件就是这个形状。
    """
    if not raw:
        return False
    head = raw[1:] if raw[:1] == SILK_PREFIX else raw
    return head.startswith(SILK_MAGIC)


def detect_audio_format(raw: bytes) -> str:
    """按文件头判断音频格式。"""
    if not raw:
        return ''
    if is_silk(raw):
        return 'silk'
    if raw[:4] == b'RIFF' and raw[8:12] == b'WAVE':
        return 'wav'
    if raw[:3] == b'ID3' or raw[:2] in (b'\xff\xfb', b'\xff\xf3', b'\xff\xf2'):
        return 'mp3'
    if raw[:4] == b'OggS':
        return 'ogg'
    if raw[:4] == b'fLaC':
        return 'flac'
    return 'unknown'


def to_silk(raw: bytes, sample_rate: int = 24000) -> tuple[bytes, int]:
    """任意音频 -> (SILK 字节, 时长毫秒)。

    微信语音必须是 SILK，但上游给的东西五花八门：

        SILK v3      直接用
        WAV          交给 pilk
        裸 PCM       包 WAV 头再交给 pilk
        MP3/OGG/FLAC 本机没有解码器 → 明确报错，让调用方去改 TTS 输出格式

    实测 `volc_tts_speak` 工具会走 `ctx.send.custom("voice", <base64音频>)`，
    而它的 `audio_format` 默认是 mp3 —— 这就是「微信里只有 [语音]」的根因：
    收到 MP3、is_silk 判 false、整段被丢弃，只剩占位文本。

    把 volc_tts 的 `tts.audio_format` 改成 `pcm` 后这条路就通了。
    """
    from . import tts as _tts

    if not raw:
        raise ValueError('空音频数据')

    fmt = detect_audio_format(raw)
    if fmt == 'silk':
        # 已是 SILK：时长按字节估算（与 WeChatAgent 一致的系数）
        return raw, estimate_silk_duration_ms(raw)

    if fmt == 'wav':
        return _tts.encode_wav_to_silk(raw, sample_rate)

    if fmt in ('mp3', 'ogg', 'flac'):
        raise ValueError(
            f'收到 {fmt.upper()} 音频，但本机没有对应解码器'
            f'（pydub/miniaudio/soundfile/av 均未安装，ffmpeg 不在 PATH）。'
            f'请把 volc_tts 的 tts.audio_format 改成 pcm。'
        )

    # 未知格式：按裸 PCM 处理（TTS 的 pcm 输出没有文件头）
    wav = _tts.pcm_to_wav(raw, sample_rate=sample_rate)
    return _tts.encode_wav_to_silk(wav, sample_rate)


def stage_silk(project_root: str | pathlib.Path, raw: bytes,
               filename: str = '', sample_rate: int = 24000) -> pathlib.Path:
    """把语音落到 `state/voice/outgoing/` 并返回路径。

    `raw` 可以是 SILK / WAV / 裸 PCM，会先转成 SILK 再落盘。
    """
    silk, _duration = to_silk(raw, sample_rate)
    raw = silk
    if len(raw) > FILE_MAX_BYTES:
        raise ValueError('语音文件过大')

    root = pathlib.Path(project_root).resolve()
    folder = root / 'state' / 'voice' / 'outgoing'
    if folder.exists() and folder.is_symlink():
        raise ValueError('voice 目录是符号链接，拒绝写入')
    folder.mkdir(parents=True, exist_ok=True)
    if not folder.resolve().is_relative_to(root):
        raise ValueError('voice 目录越界')

    name = safe_filename(filename, default=f'{uuid.uuid4().hex}.silk')
    if not name.lower().endswith('.silk'):
        name += '.silk'

    digest = hashlib.sha256(raw).hexdigest()[:16]
    target = folder / f'{digest}-{name}'

    if target.is_symlink():
        raise ValueError('目标文件是符号链接')
    if not target.exists() or target.stat().st_size != len(raw):
        temp = folder / f'{digest}.{uuid.uuid4().hex}.tmp'
        try:
            temp.write_bytes(raw)
            temp.replace(target)
        finally:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass

    MediaLeaseRegistry.shared().acquire(target)   # V2-8：staging 即租约
    return target


def estimate_silk_duration_ms(raw: bytes) -> int:
    """没有精确时长时按字节数估算（参考实现的做法）。"""
    return max(1, int(round(len(raw) * SILK_MS_PER_BYTE)))


def ensure_voice_gate(project_root: str | pathlib.Path) -> pathlib.Path:
    """确保语音总闸文件存在。

    hook 在总闸缺失时直接拒绝，且错误码是
    `voice_live_gate_removed_or_unreadable` —— 很难从报错猜到原因，
    所以这里主动创建（空文件即可）。
    """
    root = pathlib.Path(project_root).resolve()
    gate = root / 'state' / 'hook' / 'voice-live.enable'
    if not gate.exists():
        gate.parent.mkdir(parents=True, exist_ok=True)
        gate.write_bytes(b'')
        log.info('已创建语音总闸: %s', gate)
    return gate


def build_voice_request(target: str, path: pathlib.Path,
                        duration_ms: int) -> dict[str, Any]:
    """构造 /WeChatAgent/voice-send 请求体。

    path 要用正斜杠；durationMs 必须是 int。
    """
    return {
        'wxidorgid': target,
        'path': str(path).replace('\\', '/'),
        'durationMs': int(duration_ms),
    }


def voice_confirmed(result: dict[str, Any]) -> bool:
    """判断语音是否算投递成功。

    语音是异步的：`accepted_unconfirmed` 表示 hook 已交给微信的语音通道，
    真实结果以 reader 数据库回执为准。这里只要 nativeCallAttempted 且
    ret==0 就认为"已提交"，不当失败。
    """
    if not isinstance(result, dict):
        return False
    if int(result.get('ret', -1)) != 0:
        return False
    if result.get('nativeCallAttempted') is False:
        return False
    return True
